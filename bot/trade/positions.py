"""持仓管理：止盈、止损、移动止损、超时、跟随聪明钱卖出，以及动态管理（健康分复查 + 观察模式对比）。"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import TYPE_CHECKING

from .executors import Fill
from .health import dyn_cfg, score_health, smart_stats, snapshot

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.positions")

URGENT = "🚨紧急卖出："

# 按买入档位区分的卖出规则（config.yaml 的 exit.tiers 可以覆盖其中任意一项）。
# 好标的拿得更久；但到了"观察期"还涨不上去就卖掉，不干耗。所有档位翻倍时都先卖一半收回本金。
DEFAULT_EXIT_TIERS: dict[str, dict] = {
    "normal": {"max_hold_hours": 6, "stall_hours": 1, "stall_min_gain_pct": 15},
    "good": {"max_hold_hours": 24, "stall_hours": 4, "stall_min_gain_pct": 20},
    "best": {"max_hold_hours": 72, "stall_hours": 12, "stall_min_gain_pct": 30,
             "trailing_start_pct": 80, "trailing_drop_pct": 35, "follow_smart_sell_pct": 50,
             "take_profit": [{"gain_pct": 100, "sell_pct": 50}, {"gain_pct": 300, "sell_pct": 30},
                             {"gain_pct": 1000, "sell_pct": 50}]},
}


# 观察模式里的第三套规则：参考向高手学到的习惯（快止盈、紧止损、拿得短），只模拟不执行。
# 学到的高手多做已站稳的币，新币波动更大，所以止损没有照搬他们的 -11%，取 -20%。config 的 exit.learned 可改。
LEARNED_DEFAULT = {
    "stop_loss_pct": -20,
    "take_profit": [{"gain_pct": 30, "sell_pct": 30}, {"gain_pct": 60, "sell_pct": 40}, {"gain_pct": 100, "sell_pct": 100}],
    "trailing_start_pct": 30, "trailing_drop_pct": 15, "max_hold_hours": 2,
    "stall_hours": 0.5, "stall_min_gain_pct": 10,
}
VARIANTS = ("v1", "v2", "v3")
VARIANT_NAME = {"v1": "当前规则", "v2": "动态管理", "v3": "高手规则"}


def learned_rules(cfg: dict) -> dict:
    base = {k: v for k, v in (cfg or {}).items() if k not in ("tiers", "dynamic", "learned")}
    return {**base, **LEARNED_DEFAULT, **((cfg or {}).get("learned") or {})}


def exit_rules(cfg: dict, tier: str | None) -> dict:
    """某个档位实际生效的卖出规则 = 全局 exit 配置 + 档位默认 + config 里的档位覆盖。
    tier 为空（分档功能上线前买的老持仓）时只用全局配置，行为和以前一样。"""
    base = {k: v for k, v in (cfg or {}).items() if k not in ("tiers", "dynamic", "learned")}
    if not tier:
        return base
    return {**base, **DEFAULT_EXIT_TIERS.get(tier, {}), **(((cfg or {}).get("tiers") or {}).get(tier) or {})}


def recover_pct(pos: dict, price: float, entry: float, buffer_pct: float = 5) -> float:
    """卖出多少比例刚好收回本金（多卖 buffer_pct% 覆盖卖出成本），限制在 10%~100%。
    有成本 / 已回收金额 / 剩余数量时按实际算（之前部分卖出过也算进去），否则按 1 / 当前倍数。"""
    buf = 1 + buffer_pct / 100
    cost, got = float(pos.get("cost_usd") or 0), float(pos.get("realized_usd") or 0)
    rem, dec = int(pos.get("remaining_raw") or 0), pos.get("decimals")
    if cost > 0 and rem > 0 and dec is not None and price > 0:
        value = rem / 10 ** int(dec) * price
        frac = max(cost - got, 0.0) * buf / value if value > 0 else 1.0
    else:
        frac = entry / price * buf if price > 0 else 1.0
    return round(min(100.0, max(10.0, frac * 100)), 1)


def decide_exit(pos: dict, price: float, cfg: dict, now: float, dyn: dict | None = None) -> tuple[float, str]:
    """返回 (卖出比例 0~100, 原因)。0 表示不动。cfg 为全局 exit 配置，按持仓的 tier 自动套用档位规则。

    dyn 为动态管理状态（None = 只用固定规则）：
      {"cfg": 动态配置, "health": 健康分或 None, "streak": 连续偏低次数, "emergency": 紧急原因,
       "reduced": 是否已因健康分卖过一半, "why": 扣分说明}
    价格 <= 0（取价失败）时绝不触发任何卖出，避免把"没取到价格"当成"归零"误卖。
    """
    cfg = exit_rules(cfg, pos.get("tier"))
    entry = float(pos.get("entry_price") or 0)
    if price <= 0 or entry <= 0:
        return 0, ""
    gain = (price / entry - 1) * 100
    high = max(float(pos.get("high_price") or entry), price)
    high_gain = (high / entry - 1) * 100
    stage = int(pos.get("tp_stage") or 0)
    d = dyn or {}
    dc = d.get("cfg") or {}
    health = d.get("health")

    if dyn and d.get("emergency"):
        return 100, URGENT + str(d["emergency"])
    if gain <= float(cfg.get("stop_loss_pct", -35)):
        return 100, f"止损（{gain:.1f}%）"
    if dyn and high_gain >= float(dc.get("breakeven_after_pct", 50)) \
            and gain <= float(dc.get("breakeven_floor_pct", 3)):
        return 100, f"保本止损（最高 +{high_gain:.0f}%，回落到 {gain:+.1f}%）"
    if dyn and health is not None and int(d.get("streak") or 0) >= int(dc.get("confirm_checks", 2)):
        why = f"（{d['why']}）" if d.get("why") else ""
        if health < float(dc.get("exit_below", 25)):
            return 100, f"健康分 {health}，数据变差全部卖出{why}"
        if health < float(dc.get("reduce_below", 40)) and not d.get("reduced"):
            return 50, f"健康分 {health}，数据变差先卖一半{why}"
    strong = bool(dyn) and health is not None and health >= float(dc.get("strong_above", 80))

    tps = cfg.get("take_profit") or []
    # 强势长拿：翻倍出本之后（证明是好币），不再按固定价位止盈，一直拿着；
    # 等它从最高点回撤（上涨疲软）再阶梯卖出，回落到保底线就全部卖掉，最长拿 7 天
    rn = cfg.get("runner") or {}
    if rn.get("enabled") and stage >= 1 and high_gain >= float(rn.get("trigger_gain_pct", 100)):
        ladder = rn.get("ladder") or [{"drop_pct": 25, "sell_pct": 33}, {"drop_pct": 40, "sell_pct": 50},
                                       {"drop_pct": 55, "sell_pct": 100}]
        drop = (1 - price / high) * 100
        k = stage - 1
        if gain <= float(rn.get("floor_gain_pct", 20)):
            return 100, f"止盈·强势币回落到保底线（最高 +{high_gain:.0f}%，现在 {gain:+.0f}%），全部卖出"
        if k < len(ladder) and drop >= float(ladder[k]["drop_pct"]):
            return float(ladder[k]["sell_pct"]), (f"止盈·强势回撤第{k + 1}档（最高 +{high_gain:.0f}%，"
                                                  f"回撤 {drop:.0f}%，卖 {float(ladder[k]['sell_pct']):.0f}%）")
        held = (now - float(pos.get("opened_at") or now)) / 3600
        if held > float(rn.get("max_hold_hours", 168)):
            return 100, f"强势长拿满 {float(rn.get('max_hold_hours', 168)):g} 小时（当前 {gain:+.0f}%）"
        return 0, ""
    if stage < len(tps) and gain >= float(tps[stage]["gain_pct"]) and not (strong and stage >= 1):
        t = tps[stage]
        if stage == 0 and float(t["gain_pct"]) >= 100 and float(t["sell_pct"]) >= 50:
            if cfg.get("recover_cost_only", True):
                # 出本只卖"刚好收回本金"的那部分：翻倍卖约一半，涨 4 倍只卖约 1/4、涨 5 倍约 1/5，剩下的都是利润
                pct = recover_pct(pos, price, entry, float(cfg.get("recover_cost_buffer_pct", 5)))
                return pct, f"止盈·翻倍出本（+{gain:.0f}%，卖 {pct:.0f}% 刚好收回本金，剩下的都是利润）"
            return float(t["sell_pct"]), f"止盈·翻倍出本（+{gain:.0f}%，卖出 {float(t['sell_pct']):.0f}% 收回本金）"
        return float(t["sell_pct"]), f"止盈第{stage + 1}档（+{gain:.0f}%）"
    if pos.get("trail_half") and gain <= 0:
        return 100, f"移动止损卖过一半后跌回成本（{gain:+.1f}%），剩下的也卖掉"
    if high_gain >= float(cfg.get("trailing_start_pct", 60)):
        drop = (1 - price / high) * 100
        if drop >= float(cfg.get("trailing_drop_pct", 30)) + (10 if strong else 0):
            if stage == 0 and not pos.get("trail_half") and gain > 5:
                # 还没止盈过：先卖一半锁定利润，剩下一半重新计算最高点，给回调后再拉升留机会
                return 50, f"移动止损先卖一半（最高 +{high_gain:.0f}%，回撤 {drop:.0f}%，剩下一半跌回成本就卖）"
            return 100, f"移动止损（最高 +{high_gain:.0f}%，回撤 {drop:.0f}%）"
    held_h = (now - float(pos.get("opened_at") or now)) / 3600
    stall_h = float(cfg.get("stall_hours") or 0) * (2 if strong else 1)
    stall_gain = float(cfg.get("stall_min_gain_pct") or 0)
    if stall_h and stage == 0 and held_h >= stall_h and gain < stall_gain:
        return 100, f"涨不上去（持有 {held_h:.1f} 小时，当前 {gain:+.1f}%，没到 +{stall_gain:.0f}%）"
    if held_h > float(cfg.get("max_hold_hours", 24)) * (1.5 if strong else 1):
        return 100, f"持仓超时（当前 {gain:+.1f}%）"
    return 0, ""


def dyn_state(p: dict, dc: dict, now: float, reduced: int | None = None) -> dict:
    """从持仓记录取出动态管理状态。复查数据太旧（超过 3 个复查周期）时不用健康分和紧急信号。"""
    fresh = now - float(p.get("health_at") or 0) <= float(dc.get("check_sec", 120)) * 3 + 30
    has = fresh and p.get("health") is not None and p.get("entry_snap")
    notes = json.loads(p.get("health_note") or "[]") if has else []
    return {
        "cfg": dc, "health": int(p["health"]) if has else None,
        "streak": int(p.get("health_streak") or 0) if has else 0,
        "emergency": (p.get("health_emergency") or "") if has else "",
        "reduced": int(p.get("health_reduced") or 0) if reduced is None else reduced,
        "why": "；".join(n.split(" ", 1)[1] for n in notes if n.startswith("-"))[:120],
    }


class PositionManager:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self._selling: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._next_try: dict[int, float] = {}   # 卖出失败后的退避时间
        self._last_reconcile = 0.0
        self._checking: set[int] = set()
        self._breach: dict = {}   # 止损/移动止损连续触发次数（防插针）
        self._shadow_px_at: dict = {}   # 已平仓持仓的虚拟持仓上次取价时间
        self._px_ok_at: dict[int, float] = {}   # 每个持仓上次取到价格的时间
        self._started = time.time()             # 程序启动时间：重启后"多久没取到价格"从这里算起
        self._check_sem = asyncio.Semaphore(2)

    def _confirmed(self, key, why: str, px: float, entry: float) -> bool:
        """止损和移动止损要连续两次检查（约 10 秒）都触发才卖，避免被瞬间插针打掉；
        跌破硬止损（默认 -50%）不等。其他卖出原因不受影响。"""
        if not why.startswith(("止损", "移动止损", "止盈·强势回撤", "止盈·强势币回落")):
            self._breach.pop(key, None)
            return True
        hard = float(self.cfg.get("exit.hard_stop_pct", -50))
        if entry > 0 and (px / entry - 1) * 100 <= hard:
            return True
        n = self._breach.get(key, 0) + 1
        self._breach[key] = n
        if n >= int(self.cfg.get("exit.confirm_ticks", 2)):
            self._breach.pop(key, None)
            return True
        return False

    @property
    def exit_cfg(self) -> dict:
        return self.cfg.get("exit", {}) or {}

    @property
    def dcfg(self) -> dict:
        return dyn_cfg(self.exit_cfg)

    async def mode(self) -> str:
        m = await self.app.db.kv_get("dyn_mode", None) or self.dcfg.get("mode", "shadow")
        return m if m in ("off", "shadow", "live") else "shadow"

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def repair_outage_zeroes(self) -> int:
        """11-08 晚上 GMGN 被限流期间，模拟盘持仓因为"取不到价格"被误按归零平仓——那不是币归零，恢复成持仓继续管理。"""
        db = self.app.db
        if await db.kv_get("repair_zero_1108", False):
            return 0
        rows = await db.fetchall(
            "SELECT * FROM positions WHERE status='closed' AND dry_run=1 AND close_reason LIKE '取不到价格%' "
            "AND closed_at BETWEEN 1794148200 AND 1794151800")
        for p in rows:
            await db.execute("UPDATE positions SET status='open', closed_at=NULL, close_reason=NULL WHERE id=?",
                             (p["id"],))
            for w in json.loads(p["source_wallets"] or "[]"):
                await db.execute("UPDATE wallets SET follow_losses=MAX(follow_losses-1, 0) WHERE chain=? AND address=?",
                                 (p["chain"], w))
        await db.kv_set("repair_zero_1108", True)
        if rows:
            log.warning("恢复了 %d 个被误按归零平仓的模拟持仓", len(rows))
            await self.app.tg.send(f"♻️ 恢复了 {len(rows)} 个模拟持仓：它们是 GMGN 限流期间取不到价格被误按归零平仓的，"
                                   f"不是币真的归零。已恢复成持仓继续按规则管理，复盘数据也已更正")
        return len(rows)

    async def backfill_bad(self) -> int:
        """一次性：把最近 7 天亏光过的单子补记进坏人记忆。"""
        db = self.app.db
        if await db.kv_get("bad_backfill_v1", False):
            return 0
        rows = await db.fetchall("SELECT * FROM positions WHERE status='closed' AND cost_usd>0 AND realized_usd<=cost_usd*0.2 "
                                 "AND closed_at>? AND (close_reason IS NULL OR close_reason NOT LIKE '取不到价格%')",
                                 (time.time() - 7 * 86400,))
        for p in rows:
            cost = float(p["cost_usd"] or 0)
            await self.remember_bad(dict(p), f"{p['symbol'] or '?'} 亏 {(1 - float(p['realized_usd'] or 0) / cost) * 100:.0f}%")
        await db.kv_set("bad_backfill_v1", True)
        return len(rows)

    async def run(self) -> None:
        try:
            await self.repair_outage_zeroes()
            await self.backfill_bad()
        except Exception as e:  # noqa: BLE001
            log.warning("恢复误平仓 / 补记坏人失败: %s", e)
        interval = float(self.cfg.get("exit.check_interval_sec", 10))
        last_fill = 0.0
        while True:
            try:
                await self.tick()
                await self.check_daily_loss()
                await self.check_loss_streak()
                if time.time() - last_fill > 300:
                    last_fill = time.time()
                    await self.fill_skipped()
                    await self.fill_outcomes()
                    await self.fill_post_max()
            except Exception as e:  # noqa: BLE001
                log.exception("持仓检查失败: %s", e)
            await asyncio.sleep(interval)

    async def reconcile(self, pos: list[dict]) -> None:
        """每 3 分钟对一次账：实盘持仓在链上已经没有币了（之前的卖出其实成功了、或你手动卖了），就把持仓关掉。"""
        if time.time() - self._last_reconcile < 180:
            return
        self._last_reconcile = time.time()
        for p in pos:
            if p.get("dry_run") or p["id"] in self._selling or time.time() - float(p["opened_at"] or 0) < 180:
                continue
            ex = self.app.trader.exec.get(p["chain"])
            if not hasattr(ex, "_token_balance"):
                continue
            try:
                bal, _ = await ex._token_balance(p["token"])
            except Exception:  # noqa: BLE001
                continue
            if bal == 0:
                await self._close(p, float(p["realized_usd"] or 0), "对账：链上已无该币（之前的卖出实际已成交或手动卖出）")
                await self.app.tg.send(f"🔄 #{p['id']} {p['symbol']} 对账发现链上已经没有这个币了，持仓已自动关闭。"
                                       f"如果之前收到过它的「卖出失败」，说明那笔卖出其实已经成交，可以到 GMGN 钱包确认收益。")

    async def tick(self) -> None:
        db = self.app.db
        pos = await db.open_positions()
        mode = await self.mode()
        shadows: list[dict] = []
        if mode != "off":
            await self._ensure_shadows(pos)
            shadows = await db.fetchall(
                "SELECT s.*, p.chain, p.token, p.symbol, p.entry_price, p.opened_at, p.tier, p.cost_usd, "
                "p.health, p.health_streak, p.health_emergency, p.health_note, p.health_at, p.entry_snap, "
                "p.status AS pstatus, p.closed_at AS pclosed FROM shadow s JOIN positions p ON p.id=s.pid "
                "WHERE s.closed=0")
        if not pos and not shadows:
            return
        if pos:
            await self.reconcile(pos)
        now = time.time()
        dc = self.dcfg

        # 真实持仓已平仓的虚拟持仓：平仓 24 小时后结束模拟；取价和复查降频，省接口额度
        live_shadows = []
        for s in shadows:
            closed_for = now - float(s["pclosed"] or now) if s["pstatus"] != "open" else 0
            if closed_for > 24 * 3600:
                if float(s["last_price"] or 0) > 0:
                    await self._shadow_apply(s, 100, "真实持仓平仓已 24 小时，按最后价格结束模拟",
                                             float(s["last_price"]), now)
                else:
                    await db.execute("UPDATE shadow SET closed=1, closed_at=? WHERE pid=? AND variant=?",
                                     (now, s["pid"], s["variant"]))
                continue
            if closed_for and now - self._shadow_px_at.get((s["pid"], s["variant"]), 0) < 60:
                continue
            self._shadow_px_at[(s["pid"], s["variant"])] = now
            live_shadows.append(s)
        shadows = live_shadows

        # ---- 健康分复查（持仓 + 平仓 6 小时内的动态管理虚拟持仓）----
        if mode != "off":
            targets = {p["id"]: (p, float(dc["check_sec"])) for p in pos}
            for s in shadows:
                if s["variant"] == "v2" and s["pid"] not in targets and now - float(s["pclosed"] or now) < 6 * 3600:
                    row = await db.fetchone("SELECT * FROM positions WHERE id=?", (s["pid"],))
                    targets[s["pid"]] = (row, float(dc["check_sec"]) * 3)
            for pid, (p, every) in targets.items():
                if p and pid not in self._checking and now - float(p.get("health_at") or 0) >= every:
                    self._checking.add(pid)
                    self._spawn(self._check_health(p, mode))

        # ---- 取价 ----
        need: dict[str, set[str]] = {}
        for p in pos:
            need.setdefault(p["chain"], set()).add(p["token"])
        for s in shadows:
            need.setdefault(s["chain"], set()).add(s["token"])
        prices: dict[tuple[str, str], float] = {}
        for chain, toks in need.items():
            got = await self.app.data.prices(chain, list(toks))
            for t in toks:
                prices[(chain, t)] = got.get(t) or got.get(t.lower()) or 0.0

        # ---- 真实持仓 ----
        missing = sum(1 for p in pos if prices.get((p["chain"], p["token"]), 0.0) <= 0)
        g = getattr(self.app, "gmgn", None)
        outage = bool(g and (g.paused_until > now or getattr(g, "consec", 0))) or (len(pos) >= 3 and missing * 2 > len(pos))
        for p in pos:
            px = prices.get((p["chain"], p["token"]), 0.0)
            if px <= 0:
                await self._no_price(p, now, outage)
                continue
            self._px_ok_at[p["id"]] = now
            if px > float(p["high_price"] or 0):
                await db.execute("UPDATE positions SET high_price=? WHERE id=?", (px, p["id"]))
                p["high_price"] = px
            dyn = dyn_state(p, dc, now) if mode == "live" else None
            pct, why = decide_exit(p, px, self.exit_cfg, now, dyn)
            if not self._confirmed(p["id"], why, px, float(p["entry_price"] or 0)):
                continue
            if pct > 0 and p["id"] not in self._selling and now >= self._next_try.get(p["id"], 0):
                self._spawn(self.sell(p, pct, why, px, urgent=why.startswith(URGENT)))

        # ---- 观察模式：旧规则 / 新规则各自虚拟卖出 ----
        for s in shadows:
            await self._sim(s, prices.get((s["chain"], s["token"]), 0.0), now, mode)
        if mode == "shadow":
            await self._shadow_report()

    async def _no_price(self, p: dict, now: float, outage: bool = False) -> None:
        """长时间取不到价格（多半已归零/下架）：持有满 1 小时且 30 分钟取不到价 → 实盘尝试清仓，模拟盘按归零平仓。
        否则这种持仓永远占着持仓名额，最后会把买入名额占满。
        数据源整体故障（GMGN 被限流、大部分持仓同时取不到价）时不算，那不是币归零。"""
        if outage:
            self._px_ok_at[p["id"]] = now       # 故障期间不计时
            return
        last = self._px_ok_at.get(p["id"]) or max(float(p["opened_at"] or now), self._started)
        if now - last < 1800 or now - float(p["opened_at"] or now) < 3600 or p["id"] in self._selling:
            return
        if p.get("dry_run"):
            await self._close(p, float(p["realized_usd"] or 0), "取不到价格超过 30 分钟（大概率已归零），按归零平仓")
            await self.app.tg.send(f"🪦 #{p['id']} {p['symbol']} 30 分钟取不到价格，模拟盘按归零平仓")
        elif now >= self._next_try.get(p["id"], 0):
            self._spawn(self.sell(p, 100, "取不到价格超过 30 分钟，尝试清仓", 0.0, urgent=True))
        self._px_ok_at[p["id"]] = now   # 30 分钟后再试

    # ================================================================ 健康分复查
    async def _check_health(self, p: dict, mode: str) -> None:
        db = self.app.db
        pid = p["id"]
        try:
            async with self._check_sem:
                rep = await self.app.analyzer.analyze(p["chain"], p["token"], source="position", force=True)
                now = time.time()
                if not rep or float(rep.get("price") or 0) <= 0:
                    await db.execute("UPDATE positions SET health_at=? WHERE id=?", (now, pid))
                    return
                cur = snapshot(rep)
                entry = json.loads(p.get("entry_snap") or "null") or cur   # 老持仓：首次复查的数据作为基准
                prev = json.loads(p.get("prev_snap") or "null")
                smart = await smart_stats(db, p)
                held_h = (now - float(p.get("opened_at") or now)) / 3600
                dc = self.dcfg
                h, notes, emerg = score_health(entry, prev, cur, smart, held_h, dc)
                low = h < float(dc["reduce_below"])
                streak = int(p.get("health_streak") or 0) + 1 if low else 0
                await db.execute(
                    "UPDATE positions SET entry_snap=?, prev_snap=?, health=?, health_note=?, health_streak=?, "
                    "health_emergency=?, health_at=? WHERE id=?",
                    (json.dumps(entry), json.dumps(cur), h, json.dumps(notes, ensure_ascii=False), streak,
                     emerg, now, pid))
                if mode == "live" and p.get("status") == "open":
                    if emerg and not p.get("health_emergency"):
                        await self.app.tg.send(f"🚨 #{pid} {p['symbol']} {emerg}，立即全部卖出")
                    elif low and streak == 1:
                        await self.app.tg.send(
                            f"⚠️ #{pid} {p['symbol']} 健康分降到 {h}：{'；'.join(notes) or '数据变差'}\n"
                            f"下次复查（约 {int(dc['check_sec']) // 60} 分钟后）仍偏低就卖出")
        except Exception as e:  # noqa: BLE001
            log.warning("复查 #%s 失败: %s", pid, e)
        finally:
            self._checking.discard(pid)

    # ================================================================ 观察模式
    async def _ensure_shadows(self, pos: list[dict]) -> None:
        """给还没有虚拟记录的持仓建两份：v1 = 旧规则，v2 = 新规则。从持仓当前状态开始模拟。"""
        db = self.app.db
        have = {(r["pid"], r["variant"]) for r in await db.fetchall("SELECT pid, variant FROM shadow")}
        for p in pos:
            if all((p["id"], v) in have for v in VARIANTS) or float(p.get("entry_price") or 0) <= 0:
                continue
            amt = int(p["amount_raw"] or 0)
            frac = int(p["remaining_raw"] or 0) / amt if amt else 1.0
            realized = float(p["realized_usd"] or 0) / float(p["cost_usd"]) if float(p["cost_usd"] or 0) else 0.0
            for v in VARIANTS:
                await db.execute(
                    "INSERT OR IGNORE INTO shadow(pid,variant,frac,tp_stage,realized,high,reduced,last_price,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (p["id"], v, frac, int(p["tp_stage"] or 0), realized, float(p["high_price"] or p["entry_price"]),
                     int(p.get("health_reduced") or 0), float(p["entry_price"]), time.time()))
        if await db.kv_get("dyn_since", None) is None:
            await db.kv_set("dyn_since", time.time())

    async def _shadow_apply(self, s: dict, pct: float, why: str, px: float, now: float) -> None:
        entry = float(s["entry_price"] or 0)
        if px <= 0 or entry <= 0 or pct <= 0:
            return
        frac = float(s["frac"])
        sell = frac if pct >= 100 or frac * (1 - pct / 100) < 0.02 else frac * pct / 100
        frac -= sell
        cost = float(self.cfg.get("exit.shadow_cost_pct", 2)) / 100   # 单边成本估算（手续费 + 滑点）
        realized = float(s["realized"]) + sell * px / entry * (1 - cost)
        stage = int(s["tp_stage"]) + (1 if why.startswith("止盈") else 0)
        reduced = 1 if (why.startswith("健康分") and pct < 100) or s["reduced"] else 0
        half = 1 if why.startswith("移动止损先卖一半") or s.get("trail_half") else 0
        high = px if why.startswith("移动止损先卖一半") else float(s.get("high") or px)
        lg = json.loads(s["log"] or "[]")
        lg.append([round(now), round(pct), why, round((px / entry - 1) * 100, 1)])
        closed = 1 if frac <= 0.001 else 0
        await self.app.db.execute(
            "UPDATE shadow SET frac=?, realized=?, tp_stage=?, reduced=?, log=?, closed=?, closed_at=?, last_price=?, "
            "trail_half=?, high=? WHERE pid=? AND variant=?",
            (max(frac, 0.0), realized, stage, reduced, json.dumps(lg[-30:], ensure_ascii=False), closed,
             now if closed else None, px, half, high, s["pid"], s["variant"]))
        s.update(frac=max(frac, 0.0), realized=realized, tp_stage=stage, reduced=reduced, log=json.dumps(lg),
                 trail_half=half, high=high)

    async def _sim(self, s: dict, px: float, now: float, mode: str) -> None:
        db = self.app.db
        if px <= 0:
            if now - float(s["opened_at"] or now) > 96 * 3600:   # 长期取不到价：按最后价格结束模拟
                await self._shadow_apply(s, 100, "取不到价格，结束模拟", float(s["last_price"] or 0), now)
            return
        if px > float(s["high"] or 0):
            s["high"] = px
        s["last_price"] = px
        await db.execute("UPDATE shadow SET high=?, last_price=? WHERE pid=? AND variant=?",
                         (float(s["high"] or px), px, s["pid"], s["variant"]))
        v = {"entry_price": s["entry_price"], "high_price": s["high"], "tp_stage": s["tp_stage"],
             "opened_at": s["opened_at"], "tier": s["tier"], "trail_half": s.get("trail_half")}
        dyn = dyn_state(s, self.dcfg, now, reduced=int(s["reduced"] or 0)) if s["variant"] == "v2" else None
        if s["variant"] == "v3":
            pct, why = decide_exit({**v, "tier": None}, px, learned_rules(self.exit_cfg), now, None)
        else:
            pct, why = decide_exit(v, px, self.exit_cfg, now, dyn)
        if not self._confirmed((s["pid"], s["variant"]), why, px, float(s["entry_price"] or 0)):
            pct, why = 0, ""
        if pct <= 0 and now - float(s["opened_at"] or now) > 96 * 3600:
            pct, why = 100, "模拟超过 4 天，按当前价结束"
        if pct > 0:
            await self._shadow_apply(s, pct, why, px, now)
            # 只提示新规则独有的卖点（止损、止盈这些两套规则一样，真实持仓本来就会卖）
            if s["variant"] == "v2" and mode == "shadow" and s["pstatus"] == "open" \
                    and why.startswith((URGENT, "保本", "健康分")):
                await self.app.tg.send(f"👀 观察模式：新规则会在这里卖出 #{s['pid']} {s['symbol']} {pct:.0f}%"
                                       f"（{why}）。现在只记录，不执行。")

    async def shadow_on_manual(self, pid: int, pct: float, why: str, px: float) -> None:
        """不属于卖出规则的卖出同步到虚拟持仓：手动卖出同步给全部三套；
        跟卖聪明钱只同步给旧规则（动态管理靠健康分里的聪明钱扣分、高手规则靠自己的止盈止损），
        这样对比结果能回答"跟卖聪明钱到底有没有用"。"""
        follow = why.startswith("带单聪明钱")
        for s in await self.app.db.fetchall(
                "SELECT s.*, p.entry_price FROM shadow s JOIN positions p ON p.id=s.pid "
                "WHERE s.pid=? AND s.closed=0", (pid,)):
            if follow and s["variant"] != "v1":
                continue
            await self._shadow_apply(s, pct, why, px or float(s["last_price"] or 0), time.time())

    async def _shadow_report(self) -> None:
        db = self.app.db
        since = float(await db.kv_get("dyn_since", 0) or 0)
        hours = float(self.dcfg.get("shadow_report_hours", 24))
        if not since or time.time() - since < hours * 3600 or await db.kv_get("dyn_reported", False):
            return
        await db.kv_set("dyn_reported", True)
        from .review import shadow_compare
        await self.app.tg.send(f"📊 <b>动态管理观察满 {hours:.0f} 小时</b>\n{await shadow_compare(self.app)}\n\n"
                               f"想让新规则接管卖出，发 /dynamic on；继续观察不用操作。")

    # ================================================================ 卖出
    async def sell(self, p: dict, pct: float, reason: str, price_hint: float = 0.0, urgent: bool = False) -> None:
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
            if urgent:
                slip = max(slip, int(self.dcfg.get("urgent_slippage_bps", 3000)))
            ex = self.app.trader.exec.get(chain)
            if not ex:
                return
            try:
                fill: Fill = await ex.sell(p["token"], amt, int(p["decimals"] or 0), slip, urgent=urgent)
            except Exception as e:  # noqa: BLE001
                fill = Fill(False, error=str(e)[:200])
            npx = await self.app.native_price(chain)
            usd = fill.native * npx
            await self.app.db.execute(
                "INSERT INTO trades(ts,chain,token,side,position_id,native_amount,token_raw,price_usd,usd_value,tx,"
                "dry_run,ok,error,quote_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), chain, p["token"], "sell", pid, fill.native, str(fill.token_raw), price_hint, usd,
                 fill.tx, int(self.cfg.dry_run), int(fill.ok), fill.error, price_hint or fill.quote or None))
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
                    self._next_try[pid] = time.time() + (5 if urgent else 20 * attempts)
                    await self.app.db.execute("UPDATE positions SET sell_attempts=? WHERE id=?", (attempts, pid))
                    await self.app.tg.trade_failed(chain, {"symbol": p["symbol"], "address": p["token"]}, "卖出", fill)
                return
            new_remaining = max(remaining - fill.token_raw, 0)
            realized = float(p["realized_usd"] or 0) + usd
            stage = int(p["tp_stage"] or 0) + (1 if reason.startswith("止盈") else 0)
            reduced = 1 if (reason.startswith("健康分") and pct < 100) or p.get("health_reduced") else 0
            await self.app.db.execute(
                "UPDATE positions SET remaining_raw=?, realized_usd=?, tp_stage=?, sell_attempts=0, health_reduced=? "
                "WHERE id=?", (str(new_remaining), realized, stage, reduced, pid))
            if reason.startswith("移动止损先卖一半"):   # 剩下一半从当前价重新算最高点
                await self.app.db.execute("UPDATE positions SET trail_half=1, high_price=? WHERE id=?",
                                          (price_hint or float(p["high_price"] or 0), pid))
            if reason.startswith(("手动", "带单聪明钱")):
                ui = fill.token_raw / 10 ** int(p["decimals"] or 0) if fill.token_raw else 0
                px_fill = price_hint or (usd / ui if ui > 0 else 0)
                await self.shadow_on_manual(pid, pct, reason, px_fill)
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
        # 亏光（亏 80% 以上，比如撤池子 / 项目方砸盘）：记住项目方和币名，之后不再碰（数据源故障导致的"取不到价格"不算）
        cost = float(p["cost_usd"] or 0)
        if cost > 0 and realized <= cost * 0.2 and not str(reason).startswith("取不到价格"):
            try:
                await self.remember_bad(p, f"{p.get('symbol') or '?'} 亏 {(1 - realized / cost) * 100:.0f}%")
            except Exception as e:  # noqa: BLE001
                log.debug("记录坏人失败: %s", e)
        # 更新带单钱包的跟单战绩（用于修正钱包评分）
        win = realized > float(p["cost_usd"] or 0)
        for w in json.loads(p["source_wallets"] or "[]"):
            col = "follow_wins" if win else "follow_losses"
            await self.app.db.execute(f"UPDATE wallets SET {col}={col}+1 WHERE chain=? AND address=?",
                                      (p["chain"], w))

    async def remember_bad(self, p: dict, why: str) -> None:
        from .trader import norm_symbol
        db = self.app.db
        now = time.time()
        tok = await db.fetchone("SELECT creator FROM tokens WHERE chain=? AND address=?", (p["chain"], p["token"]))
        creator = (tok["creator"] if tok else "") or ""
        if creator:
            await db.execute("INSERT OR REPLACE INTO bad_actors(chain,kind,key,ts,token,reason) VALUES(?,?,?,?,?,?)",
                             (p["chain"], "creator", creator, now, p["token"], why))
        sym = norm_symbol(p.get("symbol"))
        if sym:
            await db.execute("INSERT OR REPLACE INTO bad_actors(chain,kind,key,ts,token,reason) VALUES(?,?,?,?,?,?)",
                             ("*", "symbol", sym, now, p["token"], why))
        # 带我们买进亏光币的钱包：7 天内第 2 次就移出监听（多半是项目方自己的号或老鼠仓，"胜率高"是靠割人）
        for w in json.loads(p.get("source_wallets") or "[]"):
            await db.execute("INSERT OR REPLACE INTO bad_actors(chain,kind,key,ts,token,reason) VALUES(?,?,?,?,?,?)",
                             (p["chain"], "wallet_rug", f"{w}:{p['token']}", now, p["token"], why))
            n = await db.fetchone("SELECT COUNT(*) n FROM bad_actors WHERE chain=? AND kind='wallet_rug' AND key LIKE ? "
                                  "AND ts>?", (p["chain"], f"{w}:%", now - 7 * 86400))
            if n and n["n"] >= 2:
                await db.execute("UPDATE wallets SET status='inactive' WHERE chain=? AND address=? AND status='active'",
                                 (p["chain"], w))
                log.warning("钱包 %s 7 天内带我们买进 %d 个亏光的币，移出监听", w, n["n"])
                tg = getattr(self.app, "tg", None)
                if tg:
                    await tg.send(f"🚫 钱包 {w[:6]}…{w[-4:]} 7 天内第 {n['n']} 次带机器人买进亏光的币（最近：{why}），"
                                  f"已移出监听——多半是项目方自己的号或老鼠仓")
        log.info("记住坏人：%s 项目方 %s，币名 %s", why, creator[:8] or "?", sym)

    async def check_loss_streak(self) -> None:
        """连亏熔断：连续 N 笔亏损（亏超 3%）平仓，暂停买入一段时间。通常说明行情转冷或带单钱包状态不好。"""
        t = self.cfg.get("trading", {}) or {}
        # 模拟盘默认不熔断（loss_streak_dry: 0）：模拟盘的目的是积累数据，熔断会少掉一段时间的样本
        n = int(t.get("loss_streak_dry", 0) if self.cfg.dry_run else t.get("loss_streak", 4)) or 0
        if n <= 0:
            return
        db = self.app.db
        since = float(await db.kv_get("streak_reset_at", 0) or 0)
        rows = await db.fetchall(
            "SELECT realized_usd, cost_usd, closed_at FROM positions WHERE status='closed' AND dry_run=? "
            "AND closed_at>? ORDER BY closed_at DESC LIMIT ?", (int(self.cfg.dry_run), since, n))
        if len(rows) < n or not all(float(r["realized_usd"] or 0) < float(r["cost_usd"] or 0) * 0.97 for r in rows):
            return
        mins = float(t.get("loss_streak_pause_min", 60))
        until = time.time() + mins * 60
        await db.kv_set("streak_reset_at", time.time())
        await db.kv_set("streak_pause_until", until)
        loss = sum(float(r["cost_usd"] or 0) - float(r["realized_usd"] or 0) for r in rows)
        await self.app.tg.send(f"🧊 连续 {n} 笔亏损（共 ${loss:.1f}），暂停买入 {mins:.0f} 分钟到 "
                               f"{self.app.fmt_time(until)}，持仓照常管理。想马上恢复发 /resume")

    async def fill_skipped(self) -> None:
        """被过滤的信号满 1 小时后回看价格（取不到记 -1，统计时跳过）。"""
        db = self.app.db
        rows = await db.fetchall("SELECT id, chain, token FROM skipped WHERE px_1h IS NULL AND ts<? LIMIT 40",
                                 (time.time() - 3600,))
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(r["chain"], []).append(r)
        for chain, lst in by.items():
            px = await self.app.data.prices(chain, list({r["token"] for r in lst}))
            for r in lst:
                await db.execute("UPDATE skipped SET px_1h=? WHERE id=?", (px.get(r["token"]) or -1, r["id"]))

    async def fill_post_max(self) -> None:
        """平仓满 4 小时的持仓：查平仓后 4 小时内的最高价，和平均卖出价比，看有没有卖飞。"""
        db = self.app.db
        now = time.time()
        rows = await db.fetchall(
            "SELECT id, chain, token, symbol, closed_at, realized_usd, cost_usd, amount_raw, decimals, close_reason, "
            "entry_snap FROM positions WHERE status='closed' AND post_max IS NULL AND closed_at<? AND closed_at>? LIMIT 10",
            (now - 4 * 3600, now - 3 * 86400))
        for r in rows:
            ui = int(r["amount_raw"] or 0) / 10 ** int(r["decimals"] or 0)
            exit_px = float(r["realized_usd"] or 0) / ui if ui > 0 else 0
            val = -1.0
            if exit_px > 0:
                hi = await self.app.data.recent_high_between(r["chain"], r["token"], float(r["closed_at"]),
                                                             float(r["closed_at"]) + 4 * 3600)
                val = hi / exit_px if hi > 0 else -1.0
            # 每笔复盘：打教训标签（自我学习汇总用）
            from ..brain import LESSON_TIPS, lesson
            tags = lesson({**dict(r), "post_max": val})
            await db.execute("UPDATE positions SET post_max=?, lesson=? WHERE id=?",
                             (val, json.dumps(tags, ensure_ascii=False), r["id"]))
            bad = [t for t in tags if t in ("买在高位", "卖早了", "止损后又涨回来")]
            if bad and self.app.tg:
                await self.app.tg.send(f"📚 复盘 #{r['id']} {html.escape(str(r['symbol'] or '?'))}："
                                       + "；".join(f"{t}（{LESSON_TIPS[t]}）" for t in tags), low=True)

    async def fill_outcomes(self) -> None:
        """结果档案：满 15 分钟 / 1 小时 / 4 小时后回看价格（取不到记 -1，学习时跳过）。"""
        db = self.app.db
        now = time.time()
        for col, mins in (("p15", 15), ("p60", 60), ("p240", 240)):
            rows = await db.fetchall(
                f"SELECT id, chain, token FROM token_outcomes WHERE {col} IS NULL AND ts<? AND ts>? LIMIT 60",
                (now - mins * 60, now - mins * 60 - 6 * 3600))
            by: dict[str, list[dict]] = {}
            for r in rows:
                by.setdefault(r["chain"], []).append(r)
            for chain, lst in by.items():
                px = await self.app.data.prices(chain, list({r["token"] for r in lst}))
                await db.executemany(f"UPDATE token_outcomes SET {col}=? WHERE id=?",
                                     [(px.get(r["token"]) or -1, r["id"]) for r in lst])

    async def check_daily_loss(self) -> None:
        key = "trading.max_daily_loss_usd_dry" if self.cfg.dry_run else "trading.max_daily_loss_usd"
        limit = float(self.cfg.get(key, 0) or 0)   # 模拟盘默认不设上限，方便积累数据
        if limit <= 0:
            return
        pnl = await self.app.db.realized_today_usd(self.app.day_start(), self.app.cfg.dry_run)
        if pnl <= -limit and float(await self.app.db.kv_get("pause_until", 0) or 0) < time.time():
            until = self.app.day_start() + 86400
            await self.app.db.kv_set("pause_until", until)
            await self.app.tg.send(f"⛔ 今日已实现亏损 ${-pnl:.0f}，超过上限 ${limit:.0f}，"
                                   f"自动暂停买入到 {self.app.fmt_time(until)}。持仓的止盈止损照常运行。")
