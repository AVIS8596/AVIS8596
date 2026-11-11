"""EVM 聪明钱实时监听：订阅 ERC20 Transfer 事件（钱包收币=买入，转出=卖出）。
没有 WebSocket 地址的链（罗宾汉链用公共节点时）改成每几秒用 eth_getLogs 轮询。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import websockets

from ..chains import CHAINS, TRANSFER_TOPIC, is_quote_token
from ..signals import Signal
from ..sources.evm_rpc import pad_addr, topic_to_addr
from ..utils import TTLSet

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.evmmon")
PER_SUB = 100


class EvmWalletMonitor:
    def __init__(self, app: "App", chain: str):
        self.app = app
        self.chain = chain
        self.rpc = app.evm[chain]
        self.wss = app.cfg.secrets.rpc_wss[chain]
        self.seen = TTLSet(3600)
        self._tasks: set[asyncio.Task] = set()
        self.wanted = 0                     # 应该监听的钱包数
        self.ok = False                     # 现在连着（WS 订阅成功 / 最近一次轮询成功）
        self.down_since = time.time()       # 断开的开始时间（0 = 连着）
        self.last_err = ""
        self.hits: deque = deque(maxlen=20000)   # 收到聪明钱转账通知的时间（/status 看最近 1 小时收到多少）
        self.sigs: deque = deque(maxlen=20000)   # 解析出买卖信号的时间

    def status_text(self) -> str:
        """/status 用：这条链的链上监听连没连着、最近 1 小时收到多少。"""
        name = CHAINS[self.chain].name
        host = (urlparse(self.wss or getattr(self.rpc, "url", "") or "").hostname or "").split(".")
        node = f"（{host[-2] if len(host) >= 2 else '?'}）" if host != [""] else ""
        now = time.time()
        got = sum(1 for t in self.hits if now - t < 3600)
        sig = sum(1 for t in self.sigs if now - t < 3600)
        if not self.wanted:
            return f"{name} 链上监听{node}：还没有聪明钱地址"
        if not self.ok:
            h = self.health()
            return f"{name} 链上监听{node}：❌ 断开 {h['down_min']:.0f} 分钟（{self.last_err[:80]}）"
        return (f"{name} 链上监听{node}：✅ 监听 {self.wanted} 个钱包，最近 1 小时收到转账 {got} 笔、"
                f"其中买卖 {sig} 笔")

    def _up(self) -> None:
        self.ok, self.down_since, self.last_err = True, 0.0, ""

    def _down(self, why: str) -> None:
        self.ok, self.last_err = False, why
        if not self.down_since:
            self.down_since = time.time()

    def health(self) -> dict:
        return {"ok": self.ok, "wanted": self.wanted, "err": self.last_err,
                "down_min": (time.time() - self.down_since) / 60 if self.down_since and not self.ok else 0.0}

    async def run(self) -> None:
        ev = self.app.wallets_changed[self.chain]
        n = int(self.app.cfg.get("smart_money.max_wallets_per_chain", 300))
        while True:
            ev.clear()
            wallets = [w["address"].lower() for w in await self.app.db.active_wallets(self.chain, n)]
            self.wanted = len(wallets)
            if not wallets:
                log.info("%s 暂无聪明钱地址，等待发现……", self.chain)
                await ev.wait()
                continue
            log.info("%s 开始监听 %d 个钱包", self.chain, len(wallets))
            conn = asyncio.create_task(self._conn(wallets))
            waiter = asyncio.create_task(ev.wait())
            done, _ = await asyncio.wait([conn, waiter], return_when=asyncio.FIRST_COMPLETED)
            for t in (conn, waiter):
                t.cancel()
            await asyncio.gather(conn, waiter, return_exceptions=True)
            if waiter not in done:
                await asyncio.sleep(3)

    def _on_log(self, lg: dict, wset: set[str]) -> None:
        if lg.get("removed") or len(lg.get("topics", [])) < 3:
            return   # 区块重组撤销的日志 / 非标准 Transfer（如 ERC721）
        h = lg.get("transactionHash")
        frm, to = topic_to_addr(lg["topics"][1]), topic_to_addr(lg["topics"][2])
        wallet = to if to in wset else frm if frm in wset else None
        if not wallet or not h or not self.seen.add_if_new(f"{h}:{wallet}"):
            return
        self.hits.append(time.time())
        t = asyncio.create_task(self._handle(h, wallet))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _poll(self, wallets: list[str]) -> None:
        """没有 WebSocket 的链：每隔几秒用 eth_getLogs 查新区块里这些钱包的转账。"""
        wset = set(wallets)
        every = float(self.app.cfg.get("gmgn.evm_poll_sec", 4))
        last = await self.rpc.block_number()
        fails = 0
        while True:
            await asyncio.sleep(every)
            try:
                head = await self.rpc.block_number()
                if head <= last:
                    continue
                frm = max(last + 1, head - 2000)    # 断了很久只补最近 2000 个区块，避免单次查询过大
                for i in range(0, len(wallets), PER_SUB):
                    padded = ["0x" + pad_addr(w) for w in wallets[i:i + PER_SUB]]
                    for topics in ([TRANSFER_TOPIC, None, padded], [TRANSFER_TOPIC, padded]):
                        for lg in await self.rpc.logs({"fromBlock": hex(frm), "toBlock": hex(head), "topics": topics}):
                            self._on_log(lg, wset)
                last, fails = head, 0
                self._up()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                self._down(str(e)[:200] or type(e).__name__)
                if fails in (1, 10) or fails % 100 == 0:
                    log.warning("%s 轮询监听出错（第 %d 次）：%s", self.chain, fails, e)
                await asyncio.sleep(min(60, every * fails))

    async def _conn(self, wallets: list[str]) -> None:
        if not self.wss:
            return await self._poll(wallets)
        wset = set(wallets)
        backoff = 2
        while True:
            try:
                async with websockets.connect(self.wss, ping_interval=20, ping_timeout=30,
                                              max_size=10_000_000, open_timeout=15) as ws:
                    rid = 0
                    for i in range(0, len(wallets), PER_SUB):
                        padded = ["0x" + pad_addr(w) for w in wallets[i:i + PER_SUB]]
                        for topics in ([TRANSFER_TOPIC, None, padded], [TRANSFER_TOPIC, padded]):
                            rid += 1
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "eth_subscribe",
                                                      "params": ["logs", {"topics": topics}]}))
                    backoff = 2
                    self._up()
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "error" in msg:
                            log.warning("%s 订阅错误: %s", self.chain, msg["error"])
                            continue
                        if msg.get("method") != "eth_subscription":
                            continue
                        self._on_log(msg["params"]["result"], wset)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                from .solana_wallets import ws_error
                self._down(ws_error(e))
                log.warning("%s WS 断开（%s），%ds 后重连", self.chain, ws_error(e), backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle(self, h: str, wallet: str) -> None:
        try:
            tx = await self.rpc.tx(h)
            if not tx or (tx.get("from") or "").lower() != wallet:
                return   # 不是钱包自己发起的（空投、别人转入），忽略
            rc = await self.rpc.receipt(h)
            if not rc or rc.get("status") != "0x1":
                return
            for s in parse_receipt(self.chain, wallet, tx, rc):
                self.sigs.append(time.time())
                await self.app.hub.on_signal(s)
        except Exception as e:  # noqa: BLE001
            log.warning("处理 %s 交易 %s 失败: %s", self.chain, h, e)


def parse_receipt(chain: str, wallet: str, tx: dict, rc: dict) -> list[Signal]:
    wrapped = CHAINS[chain].wrapped.lower()
    delta: dict[str, int] = {}
    wrapped_out = 0
    for lg in rc.get("logs", []):
        tp = lg.get("topics", [])
        if len(tp) < 3 or tp[0] != TRANSFER_TOPIC:
            continue
        token = lg["address"].lower()
        frm, to = topic_to_addr(tp[1]), topic_to_addr(tp[2])
        amt = int(lg.get("data") or "0x0", 16) if lg.get("data") not in (None, "0x") else 0
        if to == wallet:
            delta[token] = delta.get(token, 0) + amt
        if frm == wallet:
            delta[token] = delta.get(token, 0) - amt
            if token == wrapped:
                wrapped_out += amt
    native_in = int(tx.get("value", "0x0"), 16) + wrapped_out
    quote_out = native_in > 0 or any(is_quote_token(chain, t) and d < 0 for t, d in delta.items())
    ts = time.time()
    out = []
    for token, d in delta.items():
        if d == 0 or is_quote_token(chain, token):
            continue
        if d > 0 and quote_out:
            out.append(Signal(chain, wallet, token, "buy", native_in / 1e18, float(d), tx["hash"], ts))
        elif d < 0:
            out.append(Signal(chain, wallet, token, "sell", 0.0, float(-d), tx["hash"], ts))
    return out
