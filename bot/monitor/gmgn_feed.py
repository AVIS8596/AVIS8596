"""GMGN 聪明钱 / KOL 交易流。

两个用途：
  1. 交易流里出现的、已在我们监听列表里的钱包 → 直接生成买卖信号
     （和链上 WebSocket 监听互为备份，同一笔交易会自动去重）
  2. 交易流里新出现的钱包 → 攒起来，每隔一段时间批量核实胜率，达标就加入监听
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..chains import norm_addr
from ..signals import Signal
from ..sources.gmgn import GmgnError
from ..utils import f

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.gmgnfeed")


class GmgnFeed:
    def __init__(self, app: "App", chain: str):
        self.app = app
        self.chain = chain
        self.watch: set[str] = set()
        self.watch_at = 0.0
        self.new_makers: dict[str, str] = {}
        self.last_eval = time.time()

    async def _refresh_watch(self) -> None:
        if time.time() - self.watch_at < 60 and not self.app.wallets_changed[self.chain].is_set():
            return
        rows = await self.app.db.fetchall(
            "SELECT address FROM wallets WHERE chain=? AND status IN ('active','manual')", (self.chain,))
        self.watch = {r["address"] for r in rows}
        self.watch_at = time.time()

    async def run(self) -> None:
        g = self.app.cfg.get("gmgn", {}) or {}
        interval = float(g.get("feed_poll_sec", 3))
        eval_every = float(g.get("candidate_eval_min", 10)) * 60
        n = 0
        while True:
            await self._refresh_watch()
            feeds = [("smart", self.app.gmgn.smartmoney)]
            if n % 3 == 0:
                feeds.append(("kol", self.app.gmgn.kol))
            n += 1
            for name, fn in feeds:
                try:
                    trades = await fn(self.chain, 200)
                except GmgnError as e:
                    log.warning("GMGN %s 交易流 %s: %s", name, self.chain, e)
                    continue
                await self._handle(trades, name)
            if time.time() - self.last_eval > eval_every and self.new_makers:
                batch = dict(list(self.new_makers.items())[:200])
                for k in batch:
                    self.new_makers.pop(k, None)
                self.last_eval = time.time()
                try:
                    added = await self.app.smart.evaluate_many(self.chain, batch)
                    if added:
                        log.info("%s 从 GMGN 交易流新增聪明钱 %d 个", self.chain, added)
                        await self.app.smart.trim(self.chain)
                        self.app.wallets_changed[self.chain].set()
                except Exception as e:  # noqa: BLE001
                    log.warning("核实交易流新钱包失败: %s", e)
            await asyncio.sleep(interval)

    async def _handle(self, trades: list[dict], source: str) -> None:
        npx = 0.0
        recent = time.time() - 600
        checked = None
        for t in trades:
            maker = t.get("maker")
            token = t.get("base_address")
            side = str(t.get("side", "")).lower()
            tx = t.get("transaction_hash")
            if not maker or not token or side not in ("buy", "sell") or not tx:
                continue
            maker = norm_addr(self.chain, maker)
            if self.chain != "solana":
                tx = tx.lower()   # 和链上监听的交易哈希格式一致，才能去重
            ts = f(t.get("timestamp"))
            if maker in self.watch:
                if ts and ts < recent:
                    continue   # 10 分钟前的旧交易不再当作信号
                if not npx:
                    npx = await self.app.native_price(self.chain)
                usd = f(t.get("amount_usd"))
                await self.app.hub.on_signal(Signal(
                    self.chain, maker, norm_addr(self.chain, token), side,
                    usd / npx if npx else 0.0, f(t.get("token_amount")), tx, ts or time.time()))
            elif maker not in self.new_makers:
                if checked is None:
                    checked = {r["address"] for r in await self.app.db.fetchall(
                        "SELECT address FROM candidates WHERE chain=? AND checked_at>?",
                        (self.chain, time.time() - 86400))}
                if maker not in checked and len(self.new_makers) < 2000:
                    self.new_makers[maker] = f"gmgn_{source}"
