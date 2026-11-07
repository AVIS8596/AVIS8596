"""信号中心：聪明钱买卖 → 入库 → 推送 → 共振检测 → 交易决策。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .chains import is_quote_token
from .utils import TTLSet

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.signals")


@dataclass
class Signal:
    chain: str
    wallet: str
    token: str
    side: str            # buy / sell
    native_amount: float
    token_amount: float  # 原始数量（未除精度），仅作记录
    tx: str
    ts: float


class SignalHub:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self._cluster_sent: dict[tuple[str, str], int] = {}
        self._pending_new: set[tuple[str, str]] = set()
        self._new_seen = TTLSet(6 * 3600)   # 同一个新币 6 小时内只处理一次（多个来源会重复发现）

    async def on_signal(self, s: Signal) -> None:
        if is_quote_token(s.chain, s.token):
            return
        try:
            rid = await self.app.db.execute_rowcount(
                "INSERT OR IGNORE INTO signals(ts,chain,wallet,token,side,native_amount,token_amount,tx) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (s.ts, s.chain, s.wallet, s.token, s.side, s.native_amount, s.token_amount, f"{s.tx}:{s.token}"))
        except Exception as e:  # noqa: BLE001
            log.warning("信号入库失败: %s", e)
            return
        if not rid:
            return   # 重复信号
        w = await self.app.db.wallet(s.chain, s.wallet)
        if not w or w["status"] not in ("active", "manual"):
            return
        if s.side == "buy":
            await self._on_buy(s, w)
        else:
            await self.app.trader.on_smart_sell(s)
            if self.cfg.get("alerts.smart_buy", True) and w["score"] >= float(self.cfg.get("alerts.smart_buy_min_score", 55)):
                await self.app.tg.smart_trade(s, w, None)

    async def _on_buy(self, s: Signal, w: dict) -> None:
        win = float(self.cfg.get("alerts.cluster_window_min", 30)) * 60
        rows = await self.app.db.fetchall(
            "SELECT DISTINCT s.wallet FROM signals s JOIN wallets w ON w.chain=s.chain AND w.address=s.wallet "
            "WHERE s.chain=? AND s.token=? AND s.side='buy' AND s.ts>=? AND w.status IN ('active','manual')",
            (s.chain, s.token, time.time() - win))
        cluster = [r["wallet"] for r in rows]
        need_cluster = int(self.cfg.get("alerts.cluster_wallets", 2))

        alert_buy = self.cfg.get("alerts.smart_buy", True) and \
            w["score"] >= float(self.cfg.get("alerts.smart_buy_min_score", 55))
        copy_ok = self.cfg.get("trading.copy_trade", False) and \
            w["score"] >= float(self.cfg.get("trading.copy_min_wallet_score", 55)) and \
            len(cluster) >= int(self.cfg.get("trading.copy_min_cluster", 1))
        is_cluster = len(cluster) >= need_cluster
        if not (alert_buy or copy_ok or is_cluster):
            return

        rep = await self.app.analyzer.analyze(s.chain, s.token, source="smart_buy")
        if alert_buy:
            await self.app.tg.smart_trade(s, w, rep)
        key = (s.chain, s.token)
        if is_cluster and len(cluster) > self._cluster_sent.get(key, 0):
            self._cluster_sent[key] = len(cluster)
            await self.app.tg.cluster(s.chain, s.token, cluster, rep)
        if len(self._cluster_sent) > 20000:
            self._cluster_sent.clear()

        if rep:
            if copy_ok:
                await self.app.trader.consider(s.chain, s.token, rep, reason="跟单", wallets=cluster)
            elif self.cfg.get("trading.auto_trade", False) and \
                    rep["score"] >= float(self.cfg.get("trading.auto_min_score", 75)) and \
                    len(cluster) >= int(self.cfg.get("trading.auto_min_cluster", 2)):
                await self.app.trader.consider(s.chain, s.token, rep, reason="聪明钱共振+高评分", wallets=cluster)

    # ---------------- 新币（没有聪明钱参与，单纯按评分） ----------------
    async def on_new_token(self, chain: str, token: str, source: str, delay: float | None = None) -> None:
        key = (chain, token)
        if key in self._pending_new or not self._new_seen.add_if_new(f"{chain}:{token}"):
            return
        self._pending_new.add(key)
        try:
            await asyncio.sleep(delay if delay is not None else float(self.cfg.get("discovery.analyze_delay_sec", 45)))
            rep = await self.app.analyzer.analyze(chain, token, source=source)
            if not rep:
                return
            if rep["liquidity"] < float(self.cfg.get("discovery.min_liquidity_usd", 5000)):
                return
            if rep["score"] >= float(self.cfg.get("alerts.min_token_score_alert", 60)):
                await self.app.tg.token_report(rep, title=f"🆕 新币（{source}）")
            if self.cfg.get("trading.auto_trade", False) and \
                    rep["score"] >= float(self.cfg.get("trading.auto_min_score", 75)) and \
                    len(rep.get("smart_buyers", [])) >= int(self.cfg.get("trading.auto_min_cluster", 2)):
                await self.app.trader.consider(chain, token, rep, reason=f"新币高评分（{source}）",
                                               wallets=rep.get("smart_buyers", []))
        finally:
            self._pending_new.discard(key)
