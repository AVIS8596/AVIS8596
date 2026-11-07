"""Solana 聪明钱实时监听：Helius WebSocket logsSubscribe（每个钱包一个订阅，分多条连接）。"""
from __future__ import annotations

import asyncio
import json
import logging
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
PER_CONN = 100


class SolanaWalletMonitor:
    def __init__(self, app: "App"):
        self.app = app
        self.seen = TTLSet(3600)
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0

    async def run(self) -> None:
        ev = self.app.wallets_changed["solana"]
        n = int(self.app.cfg.get("smart_money.max_wallets_per_chain", 300))
        while True:
            ev.clear()
            wallets = [w["address"] for w in await self.app.db.active_wallets("solana", n)]
            if not wallets:
                log.info("Solana 暂无聪明钱地址，等待发现……")
                await ev.wait()
                continue
            log.info("Solana 开始监听 %d 个钱包", len(wallets))
            conns = [asyncio.create_task(self._conn(wallets[i:i + PER_CONN]))
                     for i in range(0, len(wallets), PER_CONN)]
            waiter = asyncio.create_task(ev.wait())
            done, _ = await asyncio.wait(conns + [waiter], return_when=asyncio.FIRST_COMPLETED)
            for t in conns + [waiter]:
                t.cancel()
            await asyncio.gather(*conns, waiter, return_exceptions=True)
            if waiter not in done:
                await asyncio.sleep(3)   # 连接异常退出，稍等重连

    async def _conn(self, wallets: list[str]) -> None:
        backoff = 2
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
                    subs: dict[int, str] = {}
                    backoff = 2
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
                        t = asyncio.create_task(self._handle(sig, wallet))
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Solana WS 断开（%s），%ds 后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle(self, sig: str, wallet: str) -> None:
        try:
            tx = await self.app.sol.get_transaction(sig)
            if not tx or signer(tx) != wallet:   # 只认钱包自己发起的交易，过滤空投/别人转账
                return
            for s in parse_swaps(tx, wallet, sig):
                await self.app.hub.on_signal(s)
        except Exception as e:  # noqa: BLE001
            log.warning("处理 Solana 交易 %s 失败: %s", sig, e)


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
