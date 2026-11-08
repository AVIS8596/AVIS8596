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
        self._confirming: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task] = set()

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

    async def _cluster(self, chain: str, token: str) -> list[str]:
        win = float(self.cfg.get("alerts.cluster_window_min", 30)) * 60
        rows = await self.app.db.fetchall(
            "SELECT DISTINCT s.wallet FROM signals s JOIN wallets w ON w.chain=s.chain AND w.address=s.wallet "
            "WHERE s.chain=? AND s.token=? AND s.side='buy' AND s.ts>=? AND w.status IN ('active','manual')",
            (chain, token, time.time() - win))
        return [r["wallet"] for r in rows]

    async def wallet_speed(self, chain: str, wallet: str) -> tuple[float, int]:
        """这个钱包最近 7 天实际的持仓时间（买入到第一次卖出），返回 (中位数秒数, 样本数)。"""
        rows = await self.app.db.fetchall(
            "SELECT token, side, ts FROM signals WHERE chain=? AND wallet=? AND ts>=? ORDER BY ts",
            (chain, wallet, time.time() - 7 * 86400))
        first: dict[str, float] = {}
        holds: list[float] = []
        for r in rows:
            if r["side"] == "buy":
                first.setdefault(r["token"], r["ts"])
            elif r["token"] in first:
                holds.append(r["ts"] - first.pop(r["token"]))
        if not holds:
            return 0.0, 0
        holds.sort()
        return holds[len(holds) // 2], len(holds)

    async def solo_ok(self, chain: str, w: dict) -> tuple[bool, str]:
        """单个聪明钱买入就跟，要求这个钱包是"稳的"：不是快进快出，跟单战绩也不差。
        不满足的钱包照样监听，只是要等到 2 个以上聪明钱一起买才跟。"""
        t = self.cfg.get("trading", {}) or {}
        # 信号质量优先：跟它买的币 1 小时后表现如何（样本 ≥ 8）
        sn, sr = int(w.get("sig_n") or 0), w.get("sig_ret")
        if sn >= 8 and sr is not None:
            hit, dump = float(w.get("sig_hit") or 0), float(w.get("sig_dump") or 0)
            if sr <= -0.1 and dump > hit:
                return False, f"跟这个钱包买的 {sn} 个币，1 小时后中位 {sr * 100:+.0f}%，跟不赚"
            if sr >= 0.05 and hit > dump:
                return True, ""   # 跟它买确实赚：即使快进快出也允许单独跟（有确认期）
        min_hold = float(t.get("solo_min_hold_sec", 600))
        gm = float(w.get("avg_hold_sec") or 0)
        if (w.get("kind") or "smart") == "kol":
            return False, "KOL 单独买不跟（常在粉丝进场后出货），要和其他钱包一起买才跟"
        if gm and gm < min_hold:
            return False, f"这个钱包平均持仓 {gm / 60:.0f} 分钟，快进快出跟不上"
        med, n = await self.wallet_speed(chain, w["address"])
        if n >= 5 and med < min_hold:
            return False, f"这个钱包最近 {n} 次买入，一般 {med / 60:.0f} 分钟就卖，跟不上"
        fw, fl = int(w.get("follow_wins") or 0), int(w.get("follow_losses") or 0)
        if fw + fl >= int(t.get("solo_bad_follow_min", 8)) and fw / (fw + fl) < float(t.get("solo_bad_follow_wr", 0.4)):
            return False, f"跟这个钱包 {fw} 胜 {fl} 负，战绩太差"
        return True, ""

    async def _on_buy(self, s: Signal, w: dict) -> None:
        cluster = await self._cluster(s.chain, s.token)
        need_cluster = int(self.cfg.get("alerts.cluster_wallets", 2))

        alert_buy = self.cfg.get("alerts.smart_buy", True) and \
            w["score"] >= float(self.cfg.get("alerts.smart_buy_min_score", 55))
        copy_ok = self.cfg.get("trading.copy_trade", False) and \
            w["score"] >= float(self.cfg.get("trading.copy_min_wallet_score", 55)) and \
            len(cluster) >= int(self.cfg.get("trading.copy_min_cluster", 1))
        is_cluster = len(cluster) >= need_cluster
        if not (alert_buy or copy_ok or is_cluster):
            return
        solo_why = ""
        min_native = float(self.cfg.get("trading.copy_min_native", 0.2))
        if copy_ok and s.native_amount and s.native_amount < min_native and len(cluster) < 2:
            copy_ok = False   # 试探性的小额买入（< 0.2 SOL）不单独跟
        if copy_ok and len(cluster) < 2:
            ok, solo_why = await self.solo_ok(s.chain, w)
            copy_ok = ok

        rep = await self.app.analyzer.analyze(s.chain, s.token, source="smart_buy")
        if alert_buy:
            await self.app.tg.smart_trade(s, w, rep)
        key = (s.chain, s.token)
        if is_cluster and len(cluster) > self._cluster_sent.get(key, 0):
            self._cluster_sent[key] = len(cluster)
            await self.app.tg.cluster(s.chain, s.token, cluster, rep)
        if len(self._cluster_sent) > 20000:
            self._cluster_sent.clear()
        if not rep:
            return
        reason = ""
        if copy_ok:
            reason = "跟单"
        elif self.cfg.get("trading.auto_trade", False) and \
                rep["score"] >= float(self.cfg.get("trading.auto_min_score", 75)) and \
                len(cluster) >= int(self.cfg.get("trading.auto_min_cluster", 2)):
            reason = "聪明钱共振+高评分"
        if reason:
            t = asyncio.create_task(self._confirm(s, rep, reason))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        elif solo_why:
            await self.app.trader.skip(s.chain, s.token, rep, "跟单", solo_why)

    async def _confirm(self, s: Signal, rep: dict, reason: str) -> None:
        await self.confirm_and_buy(s.chain, s.token, rep, reason, watch_wallet=s.wallet, since=s.ts)

    async def confirm_and_buy(self, chain: str, token: str, rep: dict, reason: str, wallets: list[str] | None = None,
                              watch_wallet: str = "", since: float = 0.0, tier: str | None = None) -> None:
        """确认期：等 30 秒再看——带单钱包是不是已经卖了、价格是不是已经拉飞或在砸。都没问题才买。"""
        key = (chain, token)
        if key in self._confirming:
            return
        self._confirming.add(key)
        try:
            t = self.cfg.get("trading", {}) or {}
            delay = float(t.get("confirm_sec", 30))
            data = self.app.data
            p0 = await data.price(chain, token) or float(rep.get("price") or 0)
            aud = None
            if watch_wallet:      # 确认期里顺便给带单钱包做体检（24 小时内做过就直接用结果）
                from .discovery.audit import audit, token_roles
                role = token_roles(rep).get(watch_wallet)
                if role:          # 带单钱包就是这个币的项目方 / 捆绑 / 老鼠仓 / 开盘狙击：跟它买就是给它接盘
                    await self.app.trader.skip(chain, token, rep, reason,
                                               f"带单钱包体检不过：它就是这个币的{role}，跟它买就是给它接盘")
                    return
                aud = asyncio.create_task(audit(self.app, chain, watch_wallet))
            if delay > 0:
                await asyncio.sleep(delay)
            if aud:
                try:
                    res = await asyncio.wait_for(aud, 15)
                except Exception as e:  # noqa: BLE001
                    log.debug("带单钱包体检失败 %s: %s", watch_wallet, e)
                    res = None
                if res and res["verdict"] == "suspect":
                    await self.app.trader.skip(chain, token, rep, reason, "带单钱包体检不过：" + res["notes"][0])
                    return
                if res and res["verdict"] == "sniper" and len(wallets or []) <= 1:
                    await self.app.trader.skip(chain, token, rep, reason, "带单钱包体检：狙击型，跟单时早已晚了")
                    return
            if watch_wallet:
                sold = await self.app.db.fetchone(
                    "SELECT 1 FROM signals WHERE chain=? AND token=? AND wallet=? AND side='sell' AND ts>=?",
                    (chain, token, watch_wallet, since))
                if sold:
                    await self.app.trader.skip(chain, token, rep, reason, f"确认期：带单钱包 {delay:.0f} 秒内就卖了")
                    return
            p1 = await data.price(chain, token)
            if p0 > 0 and p1 > 0:
                chg = (p1 / p0 - 1) * 100
                if chg > float(t.get("confirm_max_rise_pct", 25)):
                    await self.app.trader.skip(chain, token, rep, reason, f"确认期：{delay:.0f} 秒内已涨 {chg:.0f}%，不追")
                    return
                if chg < -float(t.get("confirm_max_drop_pct", 15)):
                    await self.app.trader.skip(chain, token, rep, reason, f"确认期：{delay:.0f} 秒内跌了 {-chg:.0f}%")
                    return
            if p1 > 0:
                rep = {**rep, "price": p1}
            if wallets is None:
                wallets = await self._cluster(chain, token)
            if tier:
                await self.app.trader.consider(chain, token, rep, reason=reason, wallets=wallets, tier=tier)
            else:
                await self.app.trader.consider(chain, token, rep, reason=reason, wallets=wallets)
        except Exception as e:  # noqa: BLE001
            log.exception("确认期处理失败: %s", e)
        finally:
            self._confirming.discard(key)

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
            from .trade.trader import looks_fake
            if looks_fake(rep, self.cfg.get("trading", {}) or {}):
                return   # 市值虚高（成交量和市值严重不匹配），不推送也不买
            if rep["score"] >= float(self.cfg.get("alerts.min_token_score_alert", 60)):
                await self.app.tg.token_report(rep, title=f"🆕 新币（{source}）", low=True)
            if self.cfg.get("trading.auto_trade", False) and \
                    rep["score"] >= float(self.cfg.get("trading.auto_min_score", 75)) and \
                    len(rep.get("smart_buyers", [])) >= int(self.cfg.get("trading.auto_min_cluster", 2)):
                await self.app.trader.consider(chain, token, rep, reason=f"新币高评分（{source}）",
                                               wallets=rep.get("smart_buyers", []))
        finally:
            self._pending_new.discard(key)
