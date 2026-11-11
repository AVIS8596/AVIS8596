"""回放回测：用机器人真实买过的单子（同样的入场时间和价格），拉之后 24 小时的 5 分钟 K 线，
按不同的卖出规则重新模拟一遍，几分钟就知道"如果当初用这套规则会赚多少"。

- 每根 K 线拆成 4 个价格点（阳线 开→低→高→收，阴线 开→高→低→收，对止损偏保守），逐点套用和实盘一样的 decide_exit
- 跟卖聪明钱：用信号表里带单钱包的卖出时间还原（过半数卖出那一刻跟卖）
- 每次卖出扣估算成本（默认 2%），买入成本已经包含在真实入场价里
- K 线缓存在数据库里，同一笔重复回测不再花接口额度
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from ..utils import f, fmt_usd
from .positions import DEFAULT_EXIT_TIERS, decide_exit, exit_rules, learned_rules

if TYPE_CHECKING:
    from ..app import App


OLD_RULES = {"trailing_start_pct": 60, "trailing_drop_pct": 30}   # 11-08 之前的规则（配合 DEFAULT_EXIT_TIERS）


def variants(exit_cfg: dict) -> dict[str, tuple[dict | None, bool]]:
    """名称 → (覆盖的卖出规则，None 表示按档位的当前规则；是否跟卖聪明钱)。
    当前规则的"跟卖"开关跟着 config 的 exit.follow_smart_sell 走。"""
    follow = bool(exit_cfg.get("follow_smart_sell", True))
    return {
        "当前规则": (None, follow),
        "旧规则（跟卖+观察期+移动止损）": ({"_old": True}, True),
        "当前 + 跟卖": (None, True),
        "不用强势长拿（固定止盈）": ({"runner": {"enabled": False}}, follow),
        "出本固定卖一半（不按回本比例）": ({"recover_cost_only": False}, follow),
        "强势长拿·回撤更宽（35/50/65%）": ({"runner": {**(exit_cfg.get("runner") or {}), "enabled": True, "ladder": [
            {"drop_pct": 35, "sell_pct": 33}, {"drop_pct": 50, "sell_pct": 50}, {"drop_pct": 65, "sell_pct": 100}]}}, follow),
        "当前 + 移动止损（+60% 后回撤 30%）": ({"trailing_start_pct": 60, "trailing_drop_pct": 30}, follow),
        "当前 + 移动止损（+100% 后回撤 40%）": ({"trailing_start_pct": 100, "trailing_drop_pct": 40}, follow),
        "当前 + 观察期 1 小时": ({"stall_hours": 1, "stall_min_gain_pct": 15}, follow),
        "止损 -25%": ({"stop_loss_pct": -25}, follow),
        "止损 -50%": ({"stop_loss_pct": -50}, follow),
        "早止盈（+50% 卖 30%）": ({"take_profit": [{"gain_pct": 50, "sell_pct": 30}, {"gain_pct": 100, "sell_pct": 50},
                                                {"gain_pct": 300, "sell_pct": 50}]}, follow),
        "高手规则": (learned_rules(exit_cfg), False),
    }


def to_path(candles: list[dict]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for c in sorted(candles, key=lambda c: f(c.get("time"))):
        t = f(c.get("time"))
        t = t / 1000 if t > 1e12 else t
        o, h, lo, cl = f(c.get("open")), f(c.get("high")), f(c.get("low")), f(c.get("close"))
        if cl <= 0:
            continue
        seq = (o, lo, h, cl) if cl >= o else (o, h, lo, cl)
        for i, px in enumerate(seq):
            if px > 0:
                pts.append((t + i * 60, px))
    return pts


def simulate(entry: float, t0: float, path: list[tuple[float, float]], rules: dict, follow_t: float | None,
             follow_pct: float, cost: float) -> float:
    """返回收益倍数（1.0 = 不赚不亏）。"""
    pos = {"entry_price": entry, "high_price": entry, "tp_stage": 0, "opened_at": t0, "tier": None, "trail_half": 0}
    frac, realized = 1.0, 0.0
    add = float(rules.pop("_trail_add", 0)) if "_trail_add" in rules else 0.0
    if add:
        rules["trailing_drop_pct"] = float(rules.get("trailing_drop_pct", 30)) + add
    followed = False
    last = entry
    for t, px in path:
        if t < t0:
            continue
        last = px
        pos["high_price"] = max(pos["high_price"], px)
        pct, why = 0.0, ""
        if follow_t and not followed and t >= follow_t:
            followed, pct, why = True, follow_pct, "跟卖"
        if not pct:
            pct, why = decide_exit(pos, px, rules, t, None)
        if pct > 0:
            sell = frac if pct >= 100 or frac * (1 - pct / 100) < 0.02 else frac * pct / 100
            realized += sell * px / entry * (1 - cost)
            frac -= sell
            if why.startswith("止盈"):
                pos["tp_stage"] += 1
            if why.startswith("移动止损先卖一半"):
                pos["trail_half"], pos["high_price"] = 1, px
            if frac <= 0.001:
                return realized
        if t - t0 > 24 * 3600:
            break
    return realized + frac * last / entry * (1 - cost)


def follow_time(rows: list[dict], sources: list[str], since: float) -> float | None:
    """过半数带单钱包（单个钱包时就是它）卖出的时刻。"""
    if not sources:
        return None
    last: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        if r["wallet"] in sources and r["ts"] >= since - 3600:
            last[r["wallet"]] = r["side"]
            sold = sum(1 for w in sources if last.get(w) == "sell")
            if r["side"] == "sell" and r["ts"] >= since and (len(sources) == 1 or sold * 2 > len(sources)):
                return float(r["ts"])
    return None


async def candles(app: "App", chain: str, token: str, t0: float) -> list[dict]:
    db = app.db
    row = await db.fetchone("SELECT data FROM kline_cache WHERE chain=? AND token=? AND t0=?", (chain, token, int(t0)))
    if row:
        return json.loads(row["data"])
    t1 = min(t0 + 24 * 3600, time.time())
    rows = await app.gmgn.kline(chain, token, "5m", int(t0) - 300, int(t1))
    if t1 - t0 >= 4 * 3600:   # 至少 4 小时的完整数据才缓存
        await db.execute("INSERT OR REPLACE INTO kline_cache(chain,token,t0,data) VALUES(?,?,?,?)",
                         (chain, token, int(t0), json.dumps(rows)))
    return rows


async def collect(app: "App", days: float = 3) -> tuple[list[dict], int]:
    """当前模式下最近 days 天、开仓满 4 小时的已平仓单子，连同 K 线路径和跟卖时间。返回 (单子列表, 候选总数)。"""
    db = app.db
    now = time.time()
    dry = int(app.cfg.dry_run)   # 只用当前模式的单子：实盘小额和模拟盘大额混在一起会把百分比和金额搅乱
    pos = await db.fetchall(
        "SELECT * FROM positions WHERE status='closed' AND opened_at>? AND opened_at<? AND entry_price>0 "
        "AND dry_run=? ORDER BY opened_at DESC LIMIT 150", (now - days * 86400, now - 4 * 3600, dry))
    exit_cfg = app.cfg.get("exit", {}) or {}
    out = []
    for p in pos:
        try:
            path = to_path(await candles(app, p["chain"], p["token"], float(p["opened_at"])))
        except Exception:  # noqa: BLE001
            continue
        if len(path) < 8:
            continue
        sources = json.loads(p["source_wallets"] or "[]")
        sig = await db.fetchall("SELECT wallet, side, ts FROM signals WHERE chain=? AND token=? AND ts>=?",
                                (p["chain"], p["token"], float(p["opened_at"]) - 3600))
        tier_rules = exit_rules(exit_cfg, p.get("tier"))
        cost_usd = float(p["cost_usd"] or 0)
        out.append({"p": dict(p), "path": path, "ft": follow_time([dict(r) for r in sig], sources, float(p["opened_at"])),
                    "tier_rules": tier_rules, "fpct": float(tier_rules.get("follow_smart_sell_pct", 100)),
                    "cost_usd": cost_usd, "symbol": p["symbol"] or "?", "opened_at": float(p["opened_at"]),
                    "actual": float(p["realized_usd"] or 0) / cost_usd if cost_usd else 1.0})
    return out, len(pos)


def replay_one(t: dict, exit_cfg: dict, override: dict | None, follow: bool, cost: float) -> float:
    p = t["p"]
    if override and override.get("_old"):
        base = {k: v for k, v in exit_cfg.items() if k not in ("tiers", "dynamic", "learned")}
        rules = {**base, **OLD_RULES, **DEFAULT_EXIT_TIERS.get(p.get("tier") or "", {})}
    elif override and "take_profit" in override and "stop_loss_pct" in override:
        rules = dict(override)
    else:
        rules = {**t["tier_rules"], **(override or {})}
    return simulate(float(p["entry_price"]), t["opened_at"], t["path"], rules, t["ft"] if follow else None,
                    t["fpct"], cost)


async def run(app: "App", days: float = 3) -> str:
    trades, n_pos = await collect(app, days)
    dry = int(app.cfg.dry_run)
    if n_pos < 5:
        return f"最近 {days:g} 天开仓满 4 小时的已平仓单子只有 {n_pos} 笔，至少 5 笔才能回测"
    exit_cfg = app.cfg.get("exit", {}) or {}
    cost = float(exit_cfg.get("shadow_cost_pct", 2)) / 100
    vs = variants(exit_cfg)
    res: dict[str, list[tuple[float, float, str]]] = {k: [] for k in vs}
    actual = [(t["actual"], t["cost_usd"], t["symbol"]) for t in trades]
    used = len(trades)
    for t in trades:
        for name, (override, follow) in vs.items():
            res[name].append((replay_one(t, exit_cfg, override, follow, cost), t["cost_usd"], t["symbol"]))

    def summ(xs: list[tuple[float, float, str]]) -> dict:
        rs = sorted(m - 1 for m, _, _ in xs)
        usds = sorted((c * (m - 1), sym) for m, c, sym in xs)
        total = sum(u for u, _ in usds)
        return {"exp": sum(rs) / len(rs), "med": rs[len(rs) // 2], "win": sum(1 for r in rs if r > 0) / len(rs),
                "usd": total, "best": usds[-1], "ex_best": total - usds[-1][0]}

    if not used:
        return "取不到这些单子的 K 线，稍后再试"
    a = summ(actual)
    lines = [f"<b>回放回测</b>（最近 {days:g} 天 {used} 笔{'模拟盘' if dry else '实盘'}真实入场，每次卖出扣 {cost * 100:.0f}% 成本）",
             f"实际结果：每笔期望 {a['exp'] * 100:+.1f}%，中位 {a['med'] * 100:+.0f}%，胜率 {a['win'] * 100:.0f}%，"
             f"合计 {fmt_usd(a['usd'])}", ""]
    ranked = sorted(((summ(v), k) for k, v in res.items() if v), key=lambda x: -x[0]["usd"])
    for st, name in ranked:
        lines.append(f"{'🏆 ' if name == ranked[0][1] else ''}<b>{name}</b>：合计 {fmt_usd(st['usd'])}，"
                     f"每笔期望 {st['exp'] * 100:+.1f}%，中位 {st['med'] * 100:+.0f}%，胜率 {st['win'] * 100:.0f}%"
                     f"｜去掉最赚的 {st['best'][1]}（{fmt_usd(st['best'][0])}）后 {fmt_usd(st['ex_best'])}")
    best = ranked[0]
    cur = next(x for x in ranked if x[1] == "当前规则")
    lines.append("")
    robust = best[0]["ex_best"] > cur[0]["ex_best"]
    if best[1] != "当前规则" and best[0]["usd"] > cur[0]["usd"]:
        lines.append(f"「{best[1]}」合计比当前规则多 {fmt_usd(best[0]['usd'] - cur[0]['usd'])}；"
                     + ("去掉最赚的一笔后依然更好，结论比较稳。" if robust else
                        "但去掉最赚的一笔后就不如当前规则了，说明主要靠一个大赢家，结论不稳。")
                     + ("样本还偏少，建议再积累一些再改" if used < 50 else ""))
    else:
        lines.append("当前规则已经是最好或差不多，暂时不用改")
    lines.append("（注：11-08 起卖出规则改成了止损 + 分批止盈拿满，旧规则作为对照一直保留在回测里）")
    lines.append("（回测用 5 分钟 K 线近似，真实成交会有偏差；看的是相对好坏）")
    return "\n".join(lines)
