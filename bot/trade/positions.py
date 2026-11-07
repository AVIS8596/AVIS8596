"""持仓管理：止盈、止损、移动止损、超时、跟随聪明钱卖出。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .executors import Fill

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.positions")


def decide_exit(pos: dict, price: float, cfg: dict, now: float) -> tuple[float, str]:
    """返回 (卖出比例 0~100, 原因)。0 表示不动。

    价格 <= 0（取价失败）时绝不触发止损，避免把"没取到价格"当成"归零"误卖。
    """
    entry = float(pos.get("entry_price") or 0)
    if price <= 0 or entry <= 0:
        return 0, ""
    gain = (price / entry - 1) * 100
    high = max(float(pos.get("high_price") or entry), price)
    high_gain = (high / entry - 1) * 100

    if gain <= float(cfg.get("stop_loss_pct", -35)):
        return 100, f"止损（{gain:.1f}%）"
    tps = cfg.get("take_profit") or []
    stage = int(pos.get("tp_stage") or 0)
    if stage < len(tps) and gain >= float(tps[stage]["gain_pct"]):
        return float(tps[stage]["sell_pct"]), f"止盈第{stage + 1}档（+{gain:.0f}%）"
    if high_gain >= float(cfg.get("trailing_start_pct", 60)):
        drop = (1 - price / high) * 100
        if drop >= float(cfg.get("trailing_drop_pct", 30)):
            return 100, f"移动止损（最高 +{high_gain:.0f}%，回撤 {drop:.0f}%）"
    if now - float(pos.get("opened_at") or now) > float(cfg.get("max_hold_hours", 24)) * 3600:
        return 100, f"持仓超时（当前 {gain:+.1f}%）"
    return 0, ""


class PositionManager:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self._selling: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._next_try: dict[int, float] = {}   # 卖出失败后的退避时间

    async def run(self) -> None:
        interval = float(self.cfg.get("exit.check_interval_sec", 10))
        while True:
            try:
                await self.tick()
                await self.check_daily_loss()
            except Exception as e:  # noqa: BLE001
                log.exception("持仓检查失败: %s", e)
            await asyncio.sleep(interval)

    async def tick(self) -> None:
        pos = await self.app.db.open_positions()
        if not pos:
            return
        by_chain: dict[str, list[dict]] = {}
        for p in pos:
            by_chain.setdefault(p["chain"], []).append(p)
        exit_cfg = self.cfg.get("exit", {}) or {}
        for chain, lst in by_chain.items():
            prices = await self.app.data.prices(chain, [p["token"] for p in lst])
            for p in lst:
                px = prices.get(p["token"]) or prices.get(p["token"].lower()) or 0.0
                if px <= 0:
                    continue
                if px > float(p["high_price"] or 0):
                    await self.app.db.execute("UPDATE positions SET high_price=? WHERE id=?", (px, p["id"]))
                    p["high_price"] = px
                pct, why = decide_exit(p, px, exit_cfg, time.time())
                if pct > 0 and p["id"] not in self._selling and time.time() >= self._next_try.get(p["id"], 0):
                    t = asyncio.create_task(self.sell(p, pct, why, px))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)

    async def sell(self, p: dict, pct: float, reason: str, price_hint: float = 0.0) -> None:
        pid = p["id"]
        if pid in self._selling:
            return
        self._selling.add(pid)
        try:
            p = await self.app.db.fetchone("SELECT * FROM positions WHERE id=?", (pid,))
            if not p or p["status"] != "open":
                return
            remaining = int(p["remaining_raw"] or 0)
            original = int(p["amount_raw"] or 0)
            amt = remaining if pct >= 100 else int(remaining * pct / 100)
            if remaining - amt < original * 0.02:   # 剩余太少，一起卖掉
                amt = remaining
            if amt <= 0:
                return
            chain = p["chain"]
            slip = int(self.cfg.per_chain("trading.slippage_bps", chain, 1000))
            ex = self.app.trader.exec.get(chain)
            if not ex:
                return
            try:
                fill: Fill = await ex.sell(p["token"], amt, int(p["decimals"] or 0), slip)
            except Exception as e:  # noqa: BLE001
                fill = Fill(False, error=str(e)[:200])
            npx = await self.app.native_price(chain)
            usd = fill.native * npx
            await self.app.db.execute(
                "INSERT INTO trades(ts,chain,token,side,position_id,native_amount,token_raw,price_usd,usd_value,tx,"
                "dry_run,ok,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), chain, p["token"], "sell", pid, fill.native, str(fill.token_raw), price_hint, usd,
                 fill.tx, int(self.cfg.dry_run), int(fill.ok), fill.error))
            if not fill.ok:
                attempts = int(p["sell_attempts"] or 0) + 1
                max_att = int(self.cfg.get("exit.max_sell_attempts", 5))
                if "没有这个币" in fill.error:
                    await self._close(p, 0, "钱包里已没有该币（可能已手动卖出）")
                    return
                if attempts >= max_att:
                    await self.app.db.execute("UPDATE positions SET status='stuck', sell_attempts=? WHERE id=?",
                                              (attempts, pid))
                    await self.app.tg.send(f"🚨 #{pid} {p['symbol']} 连续 {attempts} 次卖出失败，已标记为无法卖出，"
                                           f"请手动处理。最后错误：{fill.error}")
                else:
                    self._next_try[pid] = time.time() + 20 * attempts
                    await self.app.db.execute("UPDATE positions SET sell_attempts=? WHERE id=?", (attempts, pid))
                    await self.app.tg.trade_failed(chain, {"symbol": p["symbol"], "address": p["token"]}, "卖出", fill)
                return
            new_remaining = max(remaining - fill.token_raw, 0)
            realized = float(p["realized_usd"] or 0) + usd
            stage = int(p["tp_stage"] or 0) + (1 if reason.startswith("止盈") else 0)
            await self.app.db.execute(
                "UPDATE positions SET remaining_raw=?, realized_usd=?, tp_stage=?, sell_attempts=0 WHERE id=?",
                (str(new_remaining), realized, stage, pid))
            closed = new_remaining <= original * 0.02
            if closed:
                await self._close(p, realized, reason)
            await self.app.tg.trade_sell(p, fill, usd, reason, closed, realized)
        finally:
            self._selling.discard(pid)

    async def _close(self, p: dict, realized: float, reason: str) -> None:
        await self.app.db.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=?, realized_usd=? WHERE id=?",
            (time.time(), reason, realized, p["id"]))
        # 更新带单钱包的跟单战绩（用于修正钱包评分）
        import json
        win = realized > float(p["cost_usd"] or 0)
        for w in json.loads(p["source_wallets"] or "[]"):
            col = "follow_wins" if win else "follow_losses"
            await self.app.db.execute(f"UPDATE wallets SET {col}={col}+1 WHERE chain=? AND address=?",
                                      (p["chain"], w))

    async def check_daily_loss(self) -> None:
        limit = float(self.cfg.get("trading.max_daily_loss_usd", 0) or 0)
        if limit <= 0:
            return
        pnl = await self.app.db.realized_today_usd(self.app.day_start())
        if pnl <= -limit and float(await self.app.db.kv_get("pause_until", 0) or 0) < time.time():
            until = self.app.day_start() + 86400
            await self.app.db.kv_set("pause_until", until)
            await self.app.tg.send(f"⛔ 今日已实现亏损 ${-pnl:.0f}，超过上限 ${limit:.0f}，"
                                   f"自动暂停买入到 {self.app.fmt_time(until)}。持仓的止盈止损照常运行。")
