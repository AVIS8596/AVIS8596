"""Solana 聪明钱实时监听：Helius WebSocket logsSubscribe（每个钱包一个订阅）。

连接尽量少、尽量稳：
- Helius 免费版最多 5 条同时连接、每条 1000 个订阅 → 默认 500 个钱包一条连接（300 个钱包只用 1 条）；
- 钱包名单变了不立刻全部断开重连，最多 10 分钟换一次（频繁断开重连会被节点限流，期间 GMGN 交易流照常出信号）；
- 被拒绝（HTTP 429）时把节点返回的原因记下来，退避到最长 5 分钟，不猛敲；
- 记录连接状态，/status 能看到、断线 10 分钟以上会在 Telegram 报警（见 watchdog.py）；
- 节点有多个（Helius + SOLANA_RPC_URLS）：当前节点拒绝连接就换下一个。

省节点额度（免费版每月几百万次，取一笔交易就扣一次）：
- 通知里带着交易日志：日志里只有转账 / 建账户 / 设手续费这类程序（没碰任何交易所、聚合器）的，直接跳过不去取交易；
- 平均每天买 50 个以上不同币的扫盘型钱包不监听（和找聪明钱时"每天 50 个币基本是扫盘机器人"同一个标准）；
- 只是被别人的交易提到、自己很少买卖的钱包（比如收返佣 / 手续费的地址）不监听：每条通知都要花一次额度去取交易，
  取回来发现不是它自己发起的，白花；收到 40 条以上通知、其中自己发起的不到 10% 就停 24 小时；
- 取交易前等 1 秒（节点刚收到的交易常常还查不到，查不到再查又要花一次额度）；
- 取交易和后面的分析分开排队：分析慢不会让新通知被丢掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import socket
import time
from collections import deque
from typing import TYPE_CHECKING

import websockets

from ..chains import is_quote_token
from ..signals import Signal
from ..sources.solana_rpc import signer, wallet_deltas
from ..utils import TTLSet

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.solmon")
PER_CONN = 500
# Solana 官方公共节点：一条连接上订阅请求发到约 100 个就被踢（1013 "Too many subscriptions attempted. Please open a
# new connection."，11-13 实测每 33 秒一次）。所以公共节点上拆成多条连接、每条只订 40 个，全部连接合起来每秒发 3 个订阅。
PUBLIC = "Solana 公共节点"
PUBLIC_PER_CONN = 40
PUBLIC_SUB_GAP = 0.34
REBUILD_MIN_SEC = 600
FETCH_DELAY = 1.0
SUB_GAP = 0.12          # 订阅请求之间隔多久：每秒约 8 个（Chainstack 免费版每秒 25 次，还要留给取交易）
SUB_RETRY = 3.0         # 订阅被节点拒绝（超频）后多久重发
MENTION_MIN, MENTION_OWN = 40, 0.1
MENTION_SEC = 86400
BUSY_PER_H = 60         # 一个钱包 1 小时 60 笔以上成功的买卖：交易机器人，暂停监听（smart_money.sol_busy_per_hour）
BUSY_OUT_SEC = 6 * 3600  # 暂停 6 小时后重新给机会
# 11-12：以前数的是全部通知（失败的交易、别人转来的垃圾币、普通转账都算），门槛 30、停 24 小时，
# 10 小时后 517 个聪明钱里 388 个被当成"交易太频繁"停掉，只剩 139 个在监听。这些通知本来就不花额度去取，不该算。
INVOKE_RE = re.compile(r"^Program (\w{32,44}) invoke \[\d+\]")
# 这些程序本身不做买卖：一笔交易只碰了它们 = 转账 / 建关账户 / 设手续费 / 备注 / 投票质押，没必要去取交易内容
NON_SWAP = {
    "11111111111111111111111111111111",                 # System（转 SOL、Jito 小费）
    "ComputeBudget111111111111111111111111111111",      # 设优先费
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",      # Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",      # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",     # 建代币账户
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",      # Memo
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95",     # Lighthouse（钱包的交易保护断言）
    "Vote111111111111111111111111111111111111111",
    "Stake11111111111111111111111111111111111111",
    "AddressLookupTab1e1111111111111111111111111",
}


def worth_fetching(logs: list | None) -> bool:
    """看日志判断这笔交易值不值得去取（可能是买卖）。看不出来的一律取，宁可多花额度也不漏信号。"""
    if not logs:
        return True
    progs = set()
    for line in logs:
        line = str(line)
        if "truncated" in line.lower():
            return True
        m = INVOKE_RE.match(line)
        if m:
            progs.add(m.group(1))
    return not progs or not progs <= NON_SWAP


def ws_error(e: BaseException) -> str:
    """连接出错的原因。被节点拒绝时把它返回的说明也带上（用来区分"额度用完"和"连接数 / 频率超限"）。"""
    msg = str(e) or type(e).__name__
    resp = getattr(e, "response", None)
    body = getattr(resp, "body", b"") if resp is not None else b""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", "replace")
    body = " ".join(str(body or "").split())[:200]
    return f"{msg}：{body}" if body else msg


class MoveNode(Exception):
    """主动换监听节点（不算断线）。"""


class SolanaWalletMonitor:
    def __init__(self, app: "App"):
        self.app = app
        self.seen = TTLSet(3600)
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0
        self.live: dict[int, int] = {}      # 连接编号 → 这条连接订阅了多少个钱包
        self.capped = 0                      # 公共节点上只订阅了排名前多少个（0 = 没限）
        self._pub_slot = 0.0                 # 公共节点下一个订阅最早什么时候能发
        self.wanted = 0                      # 应该监听的钱包数
        self.down_since = time.time()        # 一条连接都没有的开始时间（0 = 有连接）
        self.last_err = ""
        self.fails = 0                       # 连续连接失败次数
        self.notes = 0                       # 收到并去处理的通知数
        self.failed = 0                      # 处理失败（取不到交易）的通知数
        self.sub_errors = 0                  # 订阅被节点拒绝的次数（会自动重发）
        self.skipped = 0                     # 看日志就知道不是买卖、没去取的通知数
        self.noisy = 0                       # 扫盘型、不监听的钱包数
        self.wstat: dict[str, list[int]] = {}   # 钱包 → [收到的通知数, 其中它自己发起的]
        self.mention_out: dict[str, float] = {}  # 只是被提到的钱包 → 停止监听的时间
        self.busy_out: dict[str, float] = {}     # 通知太频繁的钱包（交易机器人）→ 停止监听的时间
        self.wrate: dict[str, deque] = {}        # 钱包 → 最近成功买卖通知的时间
        cfg = getattr(app, "cfg", None)
        self.busy_per_h = int((cfg.get("smart_money.sol_busy_per_hour", BUSY_PER_H) if cfg else BUSY_PER_H) or BUSY_PER_H)
        self._sig_tasks: set[asyncio.Task] = set()
        self._sig_sem = asyncio.Semaphore(8)
        self.sig_dropped = 0
        self.node = ""                       # 现在连的是哪个节点
        self.last_tx_err = ""

    def health(self) -> dict:
        now = time.time()
        return {"ok": bool(self.live), "wallets": sum(self.live.values()), "wanted": self.wanted, "conns": len(self.live),
                "down_min": (now - self.down_since) / 60 if self.down_since and not self.live else 0.0,
                "err": self.last_err, "fails": self.fails, "dropped": self.dropped, "notes": self.notes,
                "failed": self.failed}

    def status_text(self) -> str:
        h = self.health()
        if not h["wanted"]:
            return "Solana 链上监听：还没有要监听的钱包"
        if h["ok"]:
            tail = f"，丢弃了 {h['dropped']} 条通知（处理不过来）" if h["dropped"] else ""
            bad = (f"，{h['failed']}/{h['notes']} 条取交易失败" if h["notes"] >= 20 and h["failed"] * 5 > h["notes"] else "")
            node = f"（{self.node}）" if self.node else ""
            save = ""
            if self.skipped + self.notes:
                save = f"，看日志跳过非买卖通知 {self.skipped / (self.skipped + self.notes) * 100:.0f}%"
            noisy = f"，{self.noisy} 个扫盘型钱包不监听" if self.noisy else ""
            if self.mention_out:
                noisy += f"，{len(self.mention_out)} 个只是被别人交易提到的钱包不监听"
            if self.busy_out:
                noisy += f"，{len(self.busy_out)} 个交易太频繁（1 小时买卖 {self.busy_per_h} 笔以上）的钱包暂停监听"
            capn = f"（公共节点只订排名前 {self.capped} 个）" if self.capped else ""
            return f"Solana 链上监听{node}：✅ 已订阅 {h['wallets']}/{h['wanted']} 个钱包{capn}{bad}{save}{noisy}{tail}"
        return (f"Solana 链上监听：❌ 连不上，已断 {h['down_min']:.0f} 分钟（{h['err'] or '还在连接'}）"
                f"——这期间 Solana 只剩 GMGN 交易流那一点信号")

    async def run(self) -> None:
        ev = self.app.wallets_changed["solana"]
        n = int(self.app.cfg.get("smart_money.max_wallets_per_chain", 300))
        current: set[str] = set()
        conns: list[asyncio.Task] = []
        built_at = 0.0
        while True:
            ev.clear()
            wallets = await self.pick_wallets(n)
            self.wanted = len(wallets)
            if not wallets:
                for t in conns:
                    t.cancel()
                await asyncio.gather(*conns, return_exceptions=True)
                conns, current = [], set()
                log.info("Solana 暂无聪明钱地址，等待发现……")
                await ev.wait()
                continue
            if set(wallets) != current or not conns or any(t.done() for t in conns):
                for t in conns:
                    t.cancel()
                await asyncio.gather(*conns, return_exceptions=True)
                if conns:
                    await asyncio.sleep(3)          # 给节点一点时间释放旧连接
                current = set(wallets)
                pub = self._public_now()
                pmax = int(self.app.cfg.get("smart_money.public_ws_max", 160))
                use = wallets[:pmax] if pub else wallets      # 钱包已按跟单战绩排好，公共节点上只订排名靠前的
                size = PUBLIC_PER_CONN if pub else PER_CONN
                self.capped = len(use) if len(use) < len(wallets) else 0
                chunks = [use[i:i + size] for i in range(0, len(use), size)]
                log.info("Solana 开始监听 %d 个钱包（%d 条连接%s）", len(use), len(chunks), "，公共节点" if pub else "")
                conns = [asyncio.create_task(self._conn(i, c)) for i, c in enumerate(chunks)]
                built_at = time.time()
            waiter = asyncio.create_task(ev.wait())
            done, _ = await asyncio.wait(conns + [waiter], return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            if waiter in done:
                # 名单可能变了：离上次建连接不到 10 分钟就先等等再换，不频繁断开重连
                left = float(self.app.cfg.get("smart_money.resubscribe_min_sec", REBUILD_MIN_SEC)) - (time.time() - built_at)
                if left > 0:
                    await asyncio.sleep(left)
            else:
                await asyncio.sleep(3)              # 连接任务意外退出，稍等重建

    def _public_now(self) -> bool:
        """现在监听会落到公共节点上吗（别的节点都拒绝 / 额度用完时）。"""
        try:
            return self.app.sol.ws_pick().name == PUBLIC
        except Exception:  # noqa: BLE001
            return False

    async def _pub_wait(self) -> None:
        """公共节点：所有连接合起来每秒最多发 3 个订阅（同一个方法每 10 秒 40 次的上限）。"""
        now = time.time()
        t = max(now, self._pub_slot)
        self._pub_slot = t + PUBLIC_SUB_GAP
        if t > now:
            await asyncio.sleep(t - now)

    async def pick_wallets(self, n: int) -> list[str]:
        """要监听的钱包：按评分取前 n 个，跳过（手动加的不跳过）：
        - 扫盘型：最近 7 天里有信号的日子，平均每天买 50 个以上不同的币；
        - 只是被别人交易提到的（见 _handle），停 24 小时。"""
        cap = float(self.app.cfg.get("smart_money.sol_max_daily_tokens", 50) or 0)
        now = time.time()
        for w, t in list(self.mention_out.items()):
            if now - t >= MENTION_SEC:          # 24 小时到了：重新给它一次机会
                self.mention_out.pop(w, None)
                self.wstat.pop(w, None)
        for w, t in list(self.busy_out.items()):
            if now - t >= BUSY_OUT_SEC:
                self.busy_out.pop(w, None)
                self.wrate.pop(w, None)
        rows = await self.app.db.active_wallets("solana", n * 3)
        noisy: set[str] = set()
        if cap:
            for r in await self.app.db.fetchall(
                    "SELECT wallet, COUNT(DISTINCT token) n, COUNT(DISTINCT CAST(ts / 86400 AS INTEGER)) d FROM signals "
                    "WHERE chain='solana' AND side='buy' AND ts>=? GROUP BY wallet HAVING n>?",
                    (now - 7 * 86400, cap)):
                if r["n"] / max(r["d"], 1) > cap:
                    noisy.add(r["wallet"])
        skip = noisy | set(self.mention_out) | set(self.busy_out)
        keep = [w["address"] for w in rows if w.get("status") == "manual" or w["address"] not in skip][:n]
        self.noisy = sum(1 for w in rows[:n] if w["address"] in noisy and w.get("status") != "manual")
        return keep

    def _up(self, cid: int, n: int) -> None:
        self.live[cid] = n
        self.down_since, self.fails, self.last_err = 0.0, 0, ""

    def _down(self, cid: int, why: str) -> None:
        self.live.pop(cid, None)
        self.fails += 1
        self.last_err = why
        if not self.live and not self.down_since:
            self.down_since = time.time()

    async def _conn(self, cid: int, wallets: list[str]) -> None:
        if cid:
            await asyncio.sleep(2.0 * cid)          # 多条连接错开建立，不同时去敲节点
        backoff = 2.0
        sol = self.app.sol
        try:
            while True:
                ep = sol.ws_pick() if hasattr(sol, "ws_pick") else None
                public = bool(ep and ep.name == PUBLIC)
                if public and len(wallets) > PUBLIC_PER_CONN:
                    # 这条连接是按别的节点分的（一条几百个钱包），现在只能用公共节点：退出，让 run() 按每条 40 个重新分
                    log.info("Solana 监听落到公共节点：按每条连接 %d 个钱包重新分", PUBLIC_PER_CONN)
                    return
                try:
                    # 只走 IPv4：服务器同时有 IPv4 和 IPv6 时，节点会看到两个 IP（Shyft 免费版限制同一个密钥的 IP 个数）
                    async with websockets.connect(ep.wss if ep else sol.wss, ping_interval=20, ping_timeout=30,
                                                  max_size=10_000_000, open_timeout=15, family=socket.AF_INET) as ws:
                        pending: dict[int, str] = {}
                        subs: dict[int, str] = {}
                        queue: list[str] = list(wallets)
                        rid_box = [0]
                        sending = [False]
                        # 公共节点一条连接发到约 100 个订阅请求就被踢：重发也算，最多发 len + 20 个，剩下的不订
                        budget = [len(queue) + 20 if public else 10 ** 9]

                        async def sender() -> None:
                            # 订阅请求慢慢发（每秒约 8 个）：一口气灌过去会超出节点每秒请求数，大部分订阅被拒，
                            # 那些钱包其实没在监听（11-11 Chainstack：1 小时 500 次 "exceeded the RPS limit"）
                            if sending[0]:
                                return
                            sending[0] = True
                            try:
                                while queue and budget[0] > 0:
                                    if public:
                                        await self._pub_wait()
                                        if not queue:
                                            break
                                    w = queue.pop(0)
                                    budget[0] -= 1
                                    rid_box[0] += 1
                                    pending[rid_box[0]] = w
                                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid_box[0], "method": "logsSubscribe",
                                                              "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                                    if not public:
                                        await asyncio.sleep(SUB_GAP)
                            finally:
                                sending[0] = False

                        async def resend() -> None:
                            await asyncio.sleep(SUB_RETRY)
                            await sender()

                        helpers: set[asyncio.Task] = set()

                        def spawn(coro) -> None:
                            t = asyncio.create_task(coro)
                            helpers.add(t)
                            t.add_done_callback(helpers.discard)

                        spawn(sender())
                        nmsg = 0
                        self._up(cid, 0)
                        if ep:
                            sol.ws_ok(ep)
                            self.node = ep.name
                        backoff = 2.0
                        try:
                            async for raw in ws:
                                msg = json.loads(raw)
                                if ep:
                                    sol.ws_note(ep)
                                    nmsg += 1
                                    if nmsg % 500 == 0 and sol.ws_should_move(ep):
                                        log.info("Solana 监听：%s 今天的额度快用超了，换到 %s", ep.name, sol.ws_pick().name)
                                        raise MoveNode()
                                if "id" in msg and msg.get("id") in pending:
                                    w = pending.pop(msg["id"])
                                    if "result" in msg:
                                        subs[msg["result"]] = w
                                        self.live[cid] = len(subs)
                                    else:                        # 被拒（多半是超频）：过几秒重发
                                        self.sub_errors += 1
                                        if self.sub_errors % 200 == 1:
                                            log.info("Solana 订阅被节点拒绝 %d 次（%s），会自动重发",
                                                     self.sub_errors, str(msg.get("error"))[:120])
                                        queue.append(w)
                                        if not sending[0]:
                                            spawn(resend())
                                    continue
                                if "error" in msg:
                                    log.warning("Solana 订阅错误: %s", msg["error"])
                                    continue
                                if msg.get("method") != "logsNotification":
                                    continue
                                p = msg["params"]
                                wallet = subs.get(p.get("subscription"))
                                val = (p.get("result") or {}).get("value") or {}
                                sig = val.get("signature")
                                if not wallet or not sig:
                                    continue
                                if wallet in self.busy_out:     # 交易机器人：暂停中，不取
                                    continue
                                if val.get("err") is not None:
                                    continue
                                if not worth_fetching(val.get("logs")):
                                    self.skipped += 1            # 只是转账 / 建账户之类：不花额度去取
                                    continue
                                if self._busy(wallet):          # 只数成功的买卖：太频繁 = 交易机器人
                                    continue
                                if not self.seen.add_if_new(f"{sig}:{wallet}"):
                                    continue
                                if len(self._tasks) >= 15:
                                    # 节点额度跟不上时丢弃，GMGN 交易流那一路还能补上；排队等下去信号也过时了
                                    self.dropped += 1
                                    if self.dropped % 100 == 1:
                                        log.warning("Solana 监听积压，已丢弃 %d 条通知（Helius 额度不够，考虑升级）", self.dropped)
                                    continue
                                self.notes += 1
                                t = asyncio.create_task(self._handle(sig, wallet))
                                self._tasks.add(t)
                                t.add_done_callback(self._tasks.discard)
                        finally:
                            for t in list(helpers):
                                t.cancel()
                    raise ConnectionError("连接被节点关闭")
                except asyncio.CancelledError:
                    raise
                except MoveNode:
                    await asyncio.sleep(1)
                    continue
                except Exception as e:  # noqa: BLE001
                    why = ws_error(e)
                    if ep:
                        why = f"{ep.name}：{why}"
                    self._down(cid, why)
                    other = sol.ws_failed(ep, why) if ep else False
                    if other:                    # 换一个节点：马上试
                        log.info("Solana WS 断开（%s），马上换节点 / 重连", why[:160])
                        await asyncio.sleep(2)
                        continue
                    wait = backoff * (0.7 + 0.6 * random.random())
                    log.warning("Solana WS 断开（%s），%.0fs 后重连", why, wait)
                    await asyncio.sleep(wait)
                    # 被拒绝（429 = 限流 / 额度用完）时退避到最长 5 分钟，不猛敲；其他断线最长 1 分钟
                    backoff = min(backoff * 2, 300.0 if "429" in why or "402" in why else 60.0)
        finally:
            self.live.pop(cid, None)
            if not self.live and not self.down_since:
                self.down_since = time.time()

    def _busy(self, wallet: str, now: float | None = None) -> bool:
        """这个钱包是不是交易机器人：1 小时 busy_per_h 笔以上成功的买卖。是的话暂停监听 6 小时。
        11-12：订阅真正生效后 1 小时 4 万多条通知处理不过来，大头是少数交易机器人——数通知不花额度，取交易才花。"""
        if wallet in self.busy_out:
            return True
        now = now or time.time()
        lim = self.busy_per_h
        q = self.wrate.setdefault(wallet, deque(maxlen=lim))
        q.append(now)
        if len(q) >= lim and now - q[0] < 3600:
            self.busy_out[wallet] = now
            log.info("Solana 钱包 %s 1 小时内买卖 %d 笔以上（交易机器人），暂停监听 6 小时", wallet, lim)
            ev = getattr(self.app, "wallets_changed", {}).get("solana")
            if ev:
                ev.set()
            return True
        return False

    def _seen_tx(self, wallet: str, own: bool) -> None:
        """记一笔：这条通知是不是钱包自己发起的。老是被别人的交易提到、自己很少发起的钱包，停止监听 24 小时。"""
        st = self.wstat.setdefault(wallet, [0, 0])
        st[0] += 1
        st[1] += int(own)
        if st[0] >= MENTION_MIN and st[1] < st[0] * MENTION_OWN and wallet not in self.mention_out:
            self.mention_out[wallet] = time.time()
            log.info("Solana 钱包 %s 收到 %d 条通知，只有 %d 条是它自己发起的（多半是收返佣 / 手续费的地址），停止监听 24 小时",
                     wallet, st[0], st[1])
            ev = getattr(self.app, "wallets_changed", {}).get("solana")
            if ev:
                ev.set()

    async def _dispatch(self, s: Signal) -> None:
        async with self._sig_sem:
            try:
                await self.app.hub.on_signal(s)
            except Exception as e:  # noqa: BLE001
                log.warning("处理 Solana 信号 %s 失败: %s", s.tx, e)

    async def _handle(self, sig: str, wallet: str) -> None:
        try:
            await asyncio.sleep(FETCH_DELAY)    # 节点刚收到的交易常常还查不到：等一下再查，省掉"查不到再查"的额度
            tx = await self.app.sol.get_transaction(sig)
            if not tx:
                return
            own = signer(tx) == wallet
            self._seen_tx(wallet, own)
            if not own:                          # 只认钱包自己发起的交易，过滤空投/别人转账/被提到
                return
            for s in parse_swaps(tx, wallet, sig):
                if len(self._sig_tasks) >= 300:  # 分析排队太长：新信号先不分析（已经过时了）
                    self.sig_dropped += 1
                    continue
                t = asyncio.create_task(self._dispatch(s))
                self._sig_tasks.add(t)
                t.add_done_callback(self._sig_tasks.discard)
        except Exception as e:  # noqa: BLE001
            self.failed += 1
            self.last_tx_err = str(e)[:200]
            if self.failed % 50 == 1:           # 节点出问题时每笔都会失败，不刷屏
                log.warning("处理 Solana 交易 %s 失败（累计 %d 次）: %s", sig, self.failed, e)


def parse_swaps(tx: dict, wallet: str, sig: str) -> list[Signal]:
    sol_d, toks = wallet_deltas(tx, wallet)
    quote_out = sol_d < -0.0005 or any(is_quote_token("solana", m) and d < 0 for m, (d, _) in toks.items())
    quote_in = sol_d > 0.0005 or any(is_quote_token("solana", m) and d > 0 for m, (d, _) in toks.items())
    ts = float(tx.get("blockTime") or time.time())
    out = []
    for mint, (d, _dec) in toks.items():
        if is_quote_token("solana", mint):
            continue
        if d > 0 and quote_out:
            out.append(Signal("solana", wallet, mint, "buy", max(-sol_d, 0.0), float(d), sig, ts))
        elif d < 0 and quote_in:
            out.append(Signal("solana", wallet, mint, "sell", max(sol_d, 0.0), float(-d), sig, ts))
    return out
