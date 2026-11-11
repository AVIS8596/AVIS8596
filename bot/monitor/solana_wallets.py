"""Solana 聪明钱实时监听：Helius WebSocket logsSubscribe（每个钱包一个订阅）。

连接尽量少、尽量稳：
- Helius 免费版最多 5 条同时连接、每条 1000 个订阅 → 默认 500 个钱包一条连接（300 个钱包只用 1 条）；
- 钱包名单变了不立刻全部断开重连，最多 10 分钟换一次（频繁断开重连会被节点限流，期间 GMGN 交易流照常出信号）；
- 被拒绝（HTTP 429）时把节点返回的原因记下来，退避到最长 5 分钟，不猛敲；
- 记录连接状态，/status 能看到、断线 10 分钟以上会在 Telegram 报警（见 watchdog.py）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
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
REBUILD_MIN_SEC = 600


def ws_error(e: BaseException) -> str:
    """连接出错的原因。被节点拒绝时把它返回的说明也带上（用来区分"额度用完"和"连接数 / 频率超限"）。"""
    msg = str(e) or type(e).__name__
    resp = getattr(e, "response", None)
    body = getattr(resp, "body", b"") if resp is not None else b""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", "replace")
    body = " ".join(str(body or "").split())[:200]
    return f"{msg}：{body}" if body else msg


class SolanaWalletMonitor:
    def __init__(self, app: "App"):
        self.app = app
        self.seen = TTLSet(3600)
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0
        self.live: dict[int, int] = {}      # 连接编号 → 这条连接订阅了多少个钱包
        self.wanted = 0                      # 应该监听的钱包数
        self.down_since = time.time()        # 一条连接都没有的开始时间（0 = 有连接）
        self.last_err = ""
        self.fails = 0                       # 连续连接失败次数
        self.notes = 0                       # 收到并去处理的通知数
        self.failed = 0                      # 处理失败（取不到交易）的通知数
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
            tail = f"，丢弃了 {h['dropped']} 条通知（节点额度跟不上）" if h["dropped"] else ""
            bad = (f"，{h['failed']}/{h['notes']} 条取交易失败" if h["notes"] >= 20 and h["failed"] * 5 > h["notes"] else "")
            return f"Solana 链上监听：✅ 已订阅 {h['wallets']}/{h['wanted']} 个钱包{bad}{tail}"
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
            wallets = [w["address"] for w in await self.app.db.active_wallets("solana", n)]
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
                chunks = [wallets[i:i + PER_CONN] for i in range(0, len(wallets), PER_CONN)]
                log.info("Solana 开始监听 %d 个钱包（%d 条连接）", len(wallets), len(chunks))
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
        try:
            while True:
                try:
                    async with websockets.connect(self.app.sol.wss, ping_interval=20, ping_timeout=30,
                                                  max_size=10_000_000, open_timeout=15) as ws:
                        pending: dict[int, str] = {}
                        for i, w in enumerate(wallets):
                            rid = i + 1
                            pending[rid] = w
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                                                      "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                            if rid % 50 == 0:
                                await asyncio.sleep(0.3)   # 订阅请求分批发，不一口气灌给节点
                        subs: dict[int, str] = {}
                        self._up(cid, len(wallets))
                        backoff = 2.0
                        async for raw in ws:
                            msg = json.loads(raw)
                            if "id" in msg and "result" in msg and msg["id"] in pending:
                                subs[msg["result"]] = pending.pop(msg["id"])
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
                            if not wallet or not sig or val.get("err") is not None:
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
                    raise ConnectionError("连接被节点关闭")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    why = ws_error(e)
                    self._down(cid, why)
                    wait = backoff * (0.7 + 0.6 * random.random())
                    log.warning("Solana WS 断开（%s），%.0fs 后重连", why, wait)
                    await asyncio.sleep(wait)
                    # 被拒绝（429 = 限流 / 额度用完）时退避到最长 5 分钟，不猛敲；其他断线最长 1 分钟
                    backoff = min(backoff * 2, 300.0 if "429" in why else 60.0)
        finally:
            self.live.pop(cid, None)
            if not self.live and not self.down_since:
                self.down_since = time.time()

    async def _handle(self, sig: str, wallet: str) -> None:
        try:
            tx = await self.app.sol.get_transaction(sig)
            if not tx or signer(tx) != wallet:   # 只认钱包自己发起的交易，过滤空投/别人转账
                return
            for s in parse_swaps(tx, wallet, sig):
                await self.app.hub.on_signal(s)
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
