"""买卖力量打法（/flow）：早期建仓，只排硬性的坑；买盘强就一直拿，卖盘强就获利了结离场。

买入：机器人分析的每个币，发币 2 小时内（flow.max_age_hours），通过硬性排雷就买（¼ 仓位、每天最多 20 笔）：
  不是貔貅 / 能增发 / 高税（致命风险）、池子已烧或还在发射平台曲线上、捆绑 < 30%、没有阴谋地址 / 团伙、
  项目方没批量发币也没在卖、流动性 ≥ $5000。不看评分、持有人多少、涨了多少（见 trader.check_lite）。
卖出（不设固定止盈）：每分钟看最近 5 分钟的买卖——
  - 买入金额 / 卖出金额（GMGN 给的话用金额，没有就用笔数；都没有就用 1 分钟 K 线：阳线成交额 / 阴线成交额）；
  - 卖盘明显占优（买卖比 < 0.7）而且价格从最高点回落 15% 以上，连续 2 次就卖：赚 20% 以上先卖一半，否则全卖；
    买盘占优（买卖比 > 1）就把计数清零，接着拿；
  - 5 分钟成交不到 $300、拿了 30 分钟还没赚 10%：没人玩了，卖；
  - 兜底：亏 35% 止损、撤池子立即卖（持仓管理那边照常）、翻倍后从最高点回撤 40% 卖、最长拿 48 小时。
对照：所有持仓（不只是这条线的）每 2 分钟记一次买卖力量（平仓后再看 6 小时），/compare 里把"按买卖力量卖"
放到真实的单子上重放，和实际的卖法比。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .lanes import lane_score
from .utils import dig, f

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.flow")

PREFIX = "买卖力量打法"
DEFAULTS = {"enabled": True, "max_age_hours": 2, "max_per_day": 20, "max_bundle_pct": 30, "min_liquidity_usd": 5000,
            "sell_ratio": 0.7, "buy_ratio": 1.0, "drop_from_high_pct": 15, "strikes": 2, "half_above_pct": 20,
            "dead_volume_usd": 300, "dead_after_min": 30, "dead_min_gain_pct": 10}
EXIT = {"stop_loss_pct": -35, "take_profit": [], "trailing_start_pct": 100, "trailing_drop_pct": 40,
        "max_hold_hours": 48, "stall_hours": 0, "runner": {"enabled": False}, "recover_cost_only": False}


def flow_from_info(d: dict) -> dict | None:
    """GMGN 代币信息里最近 5 分钟的买卖：优先用金额，没有就用笔数。取不到返回 None。"""
    bv, sv = f(dig(d, "price.buy_volume_5m", default=0)), f(dig(d, "price.sell_volume_5m", default=0))
    b, s = f(dig(d, "price.buys_5m", default=0)), f(dig(d, "price.sells_5m", default=0))
    vol = f(dig(d, "price.volume_5m", default=0)) or bv + sv
    if bv + sv > 0:
        return {"ratio": (bv + 1) / (sv + 1), "vol": vol, "src": "金额"}
    if b + s > 0:
        return {"ratio": (b + 0.5) / (s + 0.5), "vol": vol, "src": "笔数"}
    return None


def flow_from_kline(candles: list[dict], now: float) -> dict | None:
    """1 分钟 K 线的近似：最近 5 分钟阳线成交额 / 阴线成交额。"""
    up = down = 0.0
    for c in candles:
        t = f(c.get("time"))
        t = t / 1000 if t > 1e12 else t
        if now - t > 330:
            continue
        v = f(c.get("volume"))
        if f(c.get("close")) >= f(c.get("open")):
            up += v
        else:
            down += v
    if up + down <= 0:
        return {"ratio": 1.0, "vol": 0.0, "src": "K 线"}
    return {"ratio": (up + 1) / (down + 1), "vol": up + down, "src": "K 线"}


def decide(st: dict, fl: dict, gain: float, high_gain: float, drop: float, held_min: float, c: dict) -> tuple[float, str]:
    """一次检查：st 是这笔仓位的状态（strikes 连续卖盘占优次数、half 是否已经卖过一半），会被改写。返回 (卖出比例, 原因)。"""
    if fl["vol"] < float(c["dead_volume_usd"]) and held_min >= float(c["dead_after_min"]) \
            and gain < float(c["dead_min_gain_pct"]):
        return 100, f"买卖力量·没人交易了（5 分钟成交 ${fl['vol']:,.0f}，当前 {gain:+.0f}%）"
    if fl["ratio"] < float(c["sell_ratio"]) and drop >= float(c["drop_from_high_pct"]):
        st["strikes"] = st.get("strikes", 0) + 1
    elif fl["ratio"] > float(c["buy_ratio"]):
        st["strikes"] = 0
    if st.get("strikes", 0) >= int(c["strikes"]):
        st["strikes"] = 0
        if gain >= float(c["half_above_pct"]) and not st.get("half"):
            st["half"] = True
            return 50, (f"买卖力量·卖盘占优先卖一半（买卖比 {fl['ratio']:.2f}，最高 +{high_gain:.0f}%，"
                        f"回落 {drop:.0f}%，当前 {gain:+.0f}%）")
        return 100, f"买卖力量·卖盘占优离场（买卖比 {fl['ratio']:.2f}，回落 {drop:.0f}%，当前 {gain:+.0f}%）"
    return 0, ""


def replay(entry: float, t0: float, obs: list[tuple[float, float, float, float]], c: dict, cost: float = 0.02) -> float | None:
    """把"按买卖力量卖"放到一笔真实入场上重放。obs：(时间, 价格, 买卖比, 5 分钟成交额)。返回收益（扣成本）。"""
    obs = sorted(o for o in obs if o[0] >= t0 and o[1] > 0)
    if entry <= 0 or len(obs) < 3:
        return None
    st: dict = {}
    frac, got, high = 1.0, 0.0, entry
    for t, px, ratio, vol in obs:
        high = max(high, px)
        gain = (px / entry - 1) * 100
        hg = (high / entry - 1) * 100
        drop = (1 - px / high) * 100
        pct, _ = (100.0, "") if gain <= EXIT["stop_loss_pct"] else \
            ((100.0, "") if hg >= EXIT["trailing_start_pct"] and drop >= EXIT["trailing_drop_pct"] else
             decide(st, {"ratio": ratio, "vol": vol}, gain, hg, drop, (t - t0) / 60, c))
        if pct > 0:
            sell = frac if pct >= 100 else frac * pct / 100
            got += sell * px / entry
            frac -= sell
            if frac <= 1e-6:
                break
        if t - t0 > EXIT["max_hold_hours"] * 3600:
            break
    got += frac * obs[-1][1] / entry if frac > 1e-6 else 0
    return got * (1 - cost) - 1


class FlowLane:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self._seen: dict[str, float] = {}
        self._state: dict[int, dict] = {}
        self._last: dict[int, float] = {}
        self._logged = False

    def c(self) -> dict:
        return {**DEFAULTS, **(self.cfg.get("flow", {}) or {})}

    def exit_rules(self, chain: str, base: dict) -> dict:
        b = {k: v for k, v in (base or {}).items() if k not in ("tiers", "dynamic", "learned")}
        return {**b, **EXIT}

    async def flow(self, chain: str, token: str) -> dict | None:
        data = self.app.data
        d = await data._gm_info(chain, token, max_age=30) if getattr(data, "use_gmgn", False) else {}
        if d and not self._logged:
            self._logged = True
            log.info("GMGN 代币价格字段：%s", sorted((d.get("price") or {}).keys())[:60])
        fl = flow_from_info(d or {})
        if fl:
            return fl
        g = getattr(self.app, "gmgn", None)
        if not g:
            return None
        now = time.time()
        try:
            cs = await g.kline(chain, token, "1m", int(now - 420), int(now))
        except Exception:  # noqa: BLE001
            return None
        return flow_from_kline(cs or [], now)

    # ---------------- 买入
    async def on_analysis(self, chain: str, token: str, rep: dict) -> None:
        c = self.c()
        if not c.get("enabled"):
            return
        age = f(rep.get("age_hours"))
        if rep.get("risks") or rep.get("age_hours") is None or age > float(c["max_age_hours"]) \
                or float(rep.get("bundle_held_pct") or 0) >= float(c["max_bundle_pct"]):
            await lane_score(self.app.db, rep.get("_oid"), PREFIX, -1)
            return
        await lane_score(self.app.db, rep.get("_oid"), PREFIX, 1)
        now = time.time()
        if now - self._seen.get(f"{chain}:{token}", 0) < 1800:
            return
        self._seen[f"{chain}:{token}"] = now
        if len(self._seen) > 20000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 1800}
        n = await self.app.db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?",
                                       (PREFIX + "%", self.app.day_start()))
        if n and n["n"] >= int(c["max_per_day"]):
            return
        reason = f"{PREFIX}（实验仓，发币 {age * 60:.0f} 分钟）"
        hub = self.app.hub
        t = asyncio.create_task(hub.confirm_and_buy(chain, token, rep, reason, wallets=rep.get("smart_buyers") or [],
                                                    tier="normal"))
        hub._tasks.add(t)
        t.add_done_callback(hub._tasks.discard)

    # ---------------- 卖出（持仓管理每 10 秒调一次，这里每分钟真正看一次）
    async def exit_signal(self, p: dict, px: float, now: float) -> tuple[float, str]:
        pid = int(p["id"])
        if now - self._last.get(pid, 0) < 60:
            return 0, ""
        self._last[pid] = now
        entry = f(p.get("entry_price"))
        if entry <= 0 or px <= 0:
            return 0, ""
        fl = await self.flow(p["chain"], p["token"])
        if not fl:
            return 0, ""
        high = max(f(p.get("high_price")) or entry, px)
        st = self._state.setdefault(pid, {"half": bool(p.get("trail_half"))})
        pct, why = decide(st, fl, (px / entry - 1) * 100, (high / entry - 1) * 100, (1 - px / high) * 100,
                          (now - f(p.get("opened_at"))) / 60, self.c())
        if pct >= 100:
            self._state.pop(pid, None)
        return pct, why

    # ---------------- 对照：所有持仓记买卖力量（平仓后再看 6 小时），给 /compare 重放
    async def run(self) -> None:
        db = self.app.db
        await db.execute("CREATE TABLE IF NOT EXISTS flow_obs (pos_id INTEGER, ts REAL, price REAL, ratio REAL, vol REAL)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_flow_obs ON flow_obs(pos_id, ts)")
        await asyncio.sleep(60)
        while True:
            try:
                now = time.time()
                rows = await db.fetchall(
                    "SELECT id, chain, token FROM positions WHERE status='open' OR closed_at>? LIMIT 80", (now - 6 * 3600,))
                for r in rows:
                    fl = await self.flow(r["chain"], r["token"])
                    if not fl:
                        continue
                    px = (await self.app.data.prices(r["chain"], [r["token"]])).get(r["token"]) or 0.0
                    if px > 0:
                        await db.execute("INSERT INTO flow_obs(pos_id,ts,price,ratio,vol) VALUES(?,?,?,?,?)",
                                         (r["id"], now, px, fl["ratio"], fl["vol"]))
                await db.execute("DELETE FROM flow_obs WHERE ts<?", (now - 10 * 86400,))
            except Exception as e:  # noqa: BLE001
                log.debug("记买卖力量失败: %s", e)
            await asyncio.sleep(120)


async def replay_report(app: "App", days: float = 4) -> list[str]:
    """/compare 用：真实单子按"买卖力量卖法"重放 vs 实际卖法。"""
    db = app.db
    try:
        rows = await db.fetchall(
            "SELECT p.id, p.entry_price, p.opened_at, p.cost_usd, p.realized_usd FROM positions p WHERE p.status='closed' "
            "AND p.opened_at>? AND p.dry_run=?", (time.time() - days * 86400, int(bool(app.cfg.dry_run))))
    except Exception:  # noqa: BLE001
        return []
    c = {**DEFAULTS, **(app.cfg.get("flow", {}) or {})}
    real, new = [], []
    for r in rows:
        try:
            obs = await db.fetchall("SELECT ts, price, ratio, vol FROM flow_obs WHERE pos_id=? ORDER BY ts", (r["id"],))
        except Exception:  # noqa: BLE001
            return []
        if len(obs) < 3 or f(r["cost_usd"]) <= 0:
            continue
        ret = replay(f(r["entry_price"]), f(r["opened_at"]),
                     [(f(o["ts"]), f(o["price"]), f(o["ratio"]), f(o["vol"])) for o in obs], c)
        if ret is None:
            continue
        real.append(f(r["realized_usd"]) / f(r["cost_usd"]) - 1)
        new.append(ret)
    if len(real) < 5:
        return [f"<b>卖法对比</b>：按买卖力量卖的重放还只有 {len(real)} 笔（每笔要记满买卖力量才能重放），过两天再看"]
    better = sum(1 for a, b in zip(real, new) if b > a + 1e-9)
    return ["<b>卖法对比</b>（同一批真实入场，记下的买卖力量重放）",
            f"  实际的卖法：{len(real)} 笔，每笔平均 {sum(real) / len(real) * 100:+.1f}%",
            f"  按买卖力量卖：每笔平均 {sum(new) / len(new) * 100:+.1f}%（更好 {better} 笔 / 更差 "
            f"{sum(1 for a, b in zip(real, new) if b < a - 1e-9)} 笔）"]
