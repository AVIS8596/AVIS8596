"""自我学习：让每一笔模拟盘交易、每一个看过的币都变成经验。

三件事：
1. 每笔复盘：平仓 4 小时后给这笔交易打"教训"标签（买在高位 / 卖早了 / 止损后又涨回来 / 止损及时 / 拿住大赢家），
   每日复盘汇总"最大的毛病"。
2. 漏掉的好币：机器人看过、后来涨了 3 倍以上却没买的币，查它当时卡在哪条规则，统计哪条规则漏掉的好币最多。
3. 自动调参：用结果档案（每个看过的币之后 1 小时 / 4 小时的涨跌）和"早期观察样本"（快毕业的币每 2~3 分钟一份快照），
   对一组参数逐个试"放宽一档 / 收紧一档"，按"止损 -35% + 拿 4 小时"近似计算收益。
   只有前 60% 和后 40% 两段数据都更好、改变的币足够多、去掉最赚的一个依然更好，才算学到了。
   模拟盘自动生效（每次最多改一个参数，同一参数 24 小时内不重复改，都有上下限），实盘只发建议，发 /tune apply 才生效。

这是统计意义上的学习：样本越多越准；它学不会"理解"新闻和叙事。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from .utils import f, fmt_usd

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.brain")

STOP = -0.35          # 近似收益：1 小时内跌破 -35% 视为止损
COST = 0.04           # 买卖合计成本
CAP = 5.0             # 单个币的收益最多按 +500% 算，防止一个百倍币把结论带偏


def proxy_return(price: float, p60: float | None, p240: float | None, p15: float | None = None) -> float | None:
    """近似"止损 -35% + 拿 4 小时"的收益。数据不全返回 None。"""
    if not price or price <= 0:
        return None
    lows = [p for p in (p15, p60) if p and p > 0]
    if lows and min(lows) / price <= 1 + STOP:
        return STOP - COST
    end = p240 if p240 and p240 > 0 else (p60 if p60 and p60 > 0 else None)
    if end is None:
        return None
    return min(end / price - 1, CAP) - COST


# ============================================================ 1. 每笔复盘
LESSON_TIPS = {
    "买在高位": "入场时已经涨了很多（发币价 8 倍以上或 1 小时涨超 300%）",
    "卖早了": "卖出后 4 小时内又涨到卖出价 2 倍以上",
    "止损后又涨回来": "止损后 4 小时内又涨回成本以上",
    "止损及时": "止损后没再涨回来，止损是对的",
    "拿住大赢家": "这笔赚了 1 倍以上",
}


def lesson(p: dict) -> list[str]:
    cost = f(p.get("cost_usd"))
    ret = (f(p.get("realized_usd")) - cost) / cost if cost > 0 else 0.0
    try:
        snap = json.loads(p.get("entry_snap") or "{}") or {}
    except (TypeError, ValueError):
        snap = {}
    pm = f(p.get("post_max"))
    cr = str(p.get("close_reason") or "")
    tags = []
    if f(snap.get("launch_x")) >= 8 or f(snap.get("change_1h")) >= 300:
        tags.append("买在高位")
    if ret >= 1:
        tags.append("拿住大赢家")
    if cr.startswith("止损"):
        if pm >= 1.5:
            tags.append("止损后又涨回来")
        elif 0 < pm < 1.15:
            tags.append("止损及时")
    elif pm >= 2:
        tags.append("卖早了")
    return tags


async def lessons_text(app: "App", days: float = 7) -> str:
    rows = await app.db.fetchall(
        "SELECT lesson, cost_usd, realized_usd FROM positions WHERE status='closed' AND closed_at>? AND dry_run=? "
        "AND lesson IS NOT NULL", (time.time() - days * 86400, int(app.cfg.dry_run)))
    if not rows:
        return ""
    g: dict[str, list[float]] = {}
    for r in rows:
        pnl = f(r["realized_usd"]) - f(r["cost_usd"])
        for t in json.loads(r["lesson"] or "[]"):
            g.setdefault(t, []).append(pnl)
    if not g:
        return ""
    lines = [f"<b>📚 经验教训</b>（最近 {days:g} 天复盘过的 {len(rows)} 笔）"]
    for t, v in sorted(g.items(), key=lambda kv: sum(kv[1])):
        lines.append(f"  {t}：{len(v)} 笔，合计 {fmt_usd(sum(v))}（{LESSON_TIPS.get(t, '')}）")
    worst = min(g.items(), key=lambda kv: sum(kv[1]))
    if sum(worst[1]) < 0 and worst[0] in ("买在高位", "止损后又涨回来", "卖早了"):
        lines.append(f"  👉 最大的毛病：{worst[0]}，自动调参会优先在这方面找改进")
    return "\n".join(lines)


# ============================================================ 2. 漏掉的好币
async def block_reason(app: "App", chain: str, token: str, t0: float, ft: dict) -> str:
    db = app.db
    sk = await db.fetchall("SELECT cat FROM skipped WHERE chain=? AND token=? AND ts BETWEEN ? AND ?",
                           (chain, token, t0 - 60, t0 + 4 * 3600))
    if sk:
        return "风控：" + Counter(r["cat"] for r in sk).most_common(1)[0][0]
    t = app.cfg.get("trading", {}) or {}
    smin, cmin = float(t.get("auto_min_score", 75)), int(t.get("auto_min_cluster", 2))
    notes = await db.fetchall("SELECT msg FROM strat_log WHERE chain=? AND token=? AND ts BETWEEN ? AND ? "
                              "ORDER BY ts DESC LIMIT 1", (chain, token, t0 - 3 * 3600, t0 + 4 * 3600))
    if notes:
        from .strategies import status_key
        m = str(notes[0]["msg"])
        head, _, rest = m.partition("：")
        return f"{head}：{status_key(rest) if rest else m}"
    if f(ft.get("score")) < smin:
        return f"新币评分不够（要 ≥ {smin:g}）"
    if int(f(ft.get("smart"))) < cmin:
        return f"聪明钱不到 {cmin} 个"
    return "没触发任何买入条件"


async def missed_winners(app: "App", days: float = 2, min_x: float = 3.0) -> dict:
    db = app.db
    now = time.time()
    rows = await db.fetchall(
        "SELECT chain, token, symbol, ts, price, p60, p240, features, bought FROM token_outcomes "
        "WHERE ts>? AND ts<? AND price>0 ORDER BY ts", (now - days * 86400, now - 4 * 3600))
    toks: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["chain"], r["token"])
        d = toks.setdefault(k, {"first": dict(r), "peak": 0.0, "bought": 0})
        d["bought"] |= int(r["bought"] or 0)
        for p in (r["p60"], r["p240"]):
            if p and p > 0:
                d["peak"] = max(d["peak"], f(p))
    bought = {(r["chain"], r["token"]) for r in await db.fetchall(
        "SELECT chain, token FROM positions WHERE opened_at>?", (now - (days + 1) * 86400,))}
    seen = len(toks)
    winners, reasons = [], Counter()
    for k, d in toks.items():
        x = d["peak"] / f(d["first"]["price"])
        if x < min_x:
            continue
        if d["bought"] or k in bought:
            continue
        ft = json.loads(d["first"]["features"] or "{}")
        why = await block_reason(app, k[0], k[1], f(d["first"]["ts"]), ft)
        reasons[why] += 1
        winners.append((x, d["first"]["symbol"] or "?", why))
    n_x = sum(1 for d in toks.values() if d["peak"] / f(d["first"]["price"]) >= min_x)
    return {"seen": seen, "n_x": n_x, "missed": sorted(winners, reverse=True), "reasons": reasons}


async def missed_text(app: "App", days: float = 2) -> str:
    m = await missed_winners(app, days)
    if not m["seen"]:
        return ""
    lines = [f"<b>🎯 漏掉的好币</b>（最近 {days:g} 天看过 {m['seen']} 个币，{m['n_x']} 个之后涨了 3 倍以上，"
             f"其中没买的 {len(m['missed'])} 个）"]
    if m["reasons"]:
        lines.append("  卡在：" + html.escape("；".join(f"{k} ×{n}" for k, n in m["reasons"].most_common(4))))
        lines.append("  例子：" + html.escape("、".join(f"{s} {x:.0f}倍（{w}）" for x, s, w in m["missed"][:3])))
    return "\n".join(lines)


# ============================================================ 3. 自动调参
@dataclass(frozen=True)
class Param:
    key: str
    grid: tuple[float, ...]
    default: float
    label: str


ENTRY_PARAMS = (
    Param("trading.auto_min_score", (55, 60, 65, 70, 75, 80, 85), 75, "新币买入的评分门槛"),
    Param("trading.auto_min_cluster", (1, 2, 3), 2, "新币买入要几个聪明钱"),
    Param("trading.max_change_1h_pct", (150, 200, 300, 500, 800), 300, "1 小时涨幅上限（不追高）"),
    Param("trading.min_holders", (50, 80, 120, 200), 80, "最少持有人"),
    Param("trading.max_bundle_pct", (15, 20, 25, 30, 40), 25, "捆绑地址持仓上限"),
    Param("trading.max_insider_pct", (10, 15, 20, 30), 20, "埋伏地址持仓上限"),
    Param("trading.max_top10_pct", (35, 40, 45, 55), 45, "前10持仓上限"),
)
EARLY_PARAMS = (
    Param("strategies.early.min_holders", (50, 70, 100, 150, 200), 100, "早期建仓：最少持有人"),
    Param("strategies.early.min_holder_growth_pct", (6, 8, 10, 15, 20, 30), 15, "早期建仓：3 分钟持有人增长"),
    Param("strategies.early.min_volume", (5000, 10000, 15000, 25000, 40000), 15000, "早期建仓：最少成交额"),
    Param("strategies.early.max_insider_rate", (0.1, 0.15, 0.2, 0.25), 0.15, "早期建仓：老鼠仓上限"),
    Param("strategies.early.max_bundler_rate", (0.1, 0.15, 0.2, 0.3), 0.15, "早期建仓：捆绑上限"),
    Param("strategies.early.max_top10_rate", (0.25, 0.3, 0.35, 0.4), 0.3, "早期建仓：前10持仓上限"),
)
NEWTOKEN_KEYS = {"trading.auto_min_score", "trading.auto_min_cluster"}
STOPS = (-25, -30, -35, -45, -55)
LADDERS = ((20, 35, 50), (25, 40, 55), (30, 45, 60), (35, 50, 65))
FLOORS = (10, 20, 35)


def entry_pass(ft: dict, v: dict, min_liq: float = 8000) -> bool:
    """按参数 v，这个币当时会不会买（新币高评分这条路 + 通用风控过滤的近似）。"""
    if int(f(ft.get("risks"))) or f(ft.get("liquidity")) < min_liq:
        return False
    if f(ft.get("bundle_held_pct")) > v["trading.max_bundle_pct"] or \
            f(ft.get("insider_held_pct")) > v["trading.max_insider_pct"] or \
            f(ft.get("top10_pct")) > v["trading.max_top10_pct"]:
        return False
    if 0 < f(ft.get("holders")) < v["trading.min_holders"]:
        return False
    if f(ft.get("age_hours")) >= 1 and f(ft.get("change_1h")) > v["trading.max_change_1h_pct"]:
        return False
    return f(ft.get("score")) >= v["trading.auto_min_score"] and int(f(ft.get("smart"))) >= v["trading.auto_min_cluster"]


def filter_pass(ft: dict, v: dict, min_liq: float = 8000) -> bool:
    """只看通用风控过滤（跟单、主动建仓都要过）。"""
    return entry_pass(ft, {**v, "trading.auto_min_score": -1, "trading.auto_min_cluster": 0}, min_liq)


def early_entry(samples: list[dict], v: dict, base: dict) -> int | None:
    """按参数 v 模拟早期建仓：返回第几份快照时会触发（不触发返回 None）。和 strategies.early_status 同一套条件。"""
    cfg = {**base, **{k.split(".")[-1]: val for k, val in v.items()}}
    hist: deque = deque(maxlen=40)
    win = float(cfg.get("growth_window_min", 3)) * 60
    for i, s in enumerate(samples):
        now, holders = f(s["ts"]), int(f(s["holders"]))
        ok = True
        if f(s.get("created")) and now - f(s["created"]) > float(cfg.get("max_age_min", 180)) * 60:
            ok = False
        elif holders < cfg["min_holders"] or f(s["volume"]) < cfg["min_volume"]:
            ok = False
        elif s.get("wash") or f(s["rug"]) > float(cfg.get("max_rug_ratio", 0.2)) or f(s["top10"]) > cfg["max_top10_rate"] \
                or f(s["bundler"]) > cfg["max_bundler_rate"] or f(s["insider"]) > cfg["max_insider_rate"] \
                or f(s["creator"]) > float(cfg.get("max_creator_rate", 0.05)):
            ok = False
        if ok:
            old = [(t, h) for t, h in hist if now - t <= win * 2]
            if old and now - old[0][0] >= win * 0.6 and old[0][1] > 0:
                growth = (holders / old[0][1] - 1) * 100
                need = cfg["min_holder_growth_pct"] * ((now - old[0][0]) / win)
                if growth >= max(need, cfg["min_holder_growth_pct"] * 0.6):
                    return i
        hist.append((now, holders))
    return None


@dataclass
class Case:
    ts: float
    ret_fn: Callable[[dict], float | None]   # 参数 → 收益（不买返回 None）


def _sum(rs: list[float | None]) -> tuple[float, int]:
    xs = [r for r in rs if r is not None]
    return sum(xs), len(xs)


def tune(cases: list[Case], params: tuple[Param, ...], cur: dict, min_flip: int = 15,
         skip_keys: set[str] | None = None) -> dict | None:
    """逐个参数试相邻一档，返回最好的一处改动（没有可信的改进返回 None）。"""
    if len(cases) < 4 * min_flip:
        return None
    cases = sorted(cases, key=lambda c: c.ts)
    cut = int(len(cases) * 0.6)
    base = [c.ret_fn(cur) for c in cases]
    b_tr, b_va, b_all = _sum(base[:cut]), _sum(base[cut:]), _sum(base)
    best = None
    for p in params:
        if skip_keys and p.key in skip_keys:
            continue
        g = list(p.grid)
        i = min(range(len(g)), key=lambda j: abs(g[j] - cur[p.key]))
        for j in (i - 1, i + 1):
            if not 0 <= j < len(g) or g[j] == cur[p.key]:
                continue
            v = {**cur, p.key: g[j]}
            new = [c.ret_fn(v) for c in cases]
            added = [b for a, b in zip(base, new) if a != b and b is not None]
            removed = [a for a, b in zip(base, new) if a != b and a is not None]
            flips = max(len(added), len(removed))
            if flips < min_flip:
                continue
            g_tr = _sum(new[:cut])[0] - b_tr[0]
            g_va = _sum(new[cut:])[0] - b_va[0]
            if g_tr <= 0 or g_va <= 0:          # 前 60% 和后 40% 两段都要更好
                continue
            delta = sum(added) - sum(removed)
            if delta - max(added + [0.0]) <= 0:      # 去掉最赚的一个就不成立 = 靠运气
                continue
            all_v = _sum(new)
            gain = g_tr + g_va
            if not best or gain > best["gain"]:
                best = {"key": p.key, "label": p.label, "old": cur[p.key], "new": g[j], "gain": gain,
                        "flips": flips, "n_old": b_all[1], "n_new": all_v[1],
                        "exp_old": b_all[0] / b_all[1] if b_all[1] else 0.0,
                        "exp_new": all_v[0] / all_v[1] if all_v[1] else 0.0,
                        "sum_old": b_all[0], "sum_new": all_v[0], "cases": len(cases)}
    return best


def set_cfg(raw: dict, dotted: str, value: Any) -> None:
    cur = raw
    parts = dotted.split(".")
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return "/".join(str(x.get("drop_pct", x)) if isinstance(x, dict) else str(x) for x in v) + "%"
    return str(v)


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else float(v)


class Brain:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.last_tune = 0.0
        self.last_exit = 0.0

    # ---------------- 参数覆盖（学到的参数存在数据库里，重启后自动加载）
    async def load(self) -> None:
        tuned = await self.app.db.kv_get("tuned", {}) or {}
        for k, v in tuned.items():
            set_cfg(self.cfg.raw, k, v)
        if tuned:
            log.info("已加载学到的参数：%s", tuned)

    async def mode(self) -> str:
        m = await self.app.db.kv_get("tune_mode", None) or str(self.cfg.get("learning.auto_tune", "auto"))
        return m if m in ("auto", "suggest", "off") else "auto"

    async def apply(self, key: str, value: float, why: str) -> None:
        db = self.app.db
        tuned = await db.kv_get("tuned", {}) or {}
        base = await db.kv_get("tune_base", {}) or {}
        old = self.cfg.get(key)
        if key not in base:
            base[key] = old
        val = _num(value) if isinstance(value, (int, float)) else value
        tuned[key] = val
        set_cfg(self.cfg.raw, key, val)
        hist = (await db.kv_get("tune_log", []) or [])[-49:]
        hist.append({"ts": time.time(), "key": key, "old": old, "new": val, "why": why})
        await db.kv_set("tuned", tuned)
        await db.kv_set("tune_base", base)
        await db.kv_set("tune_log", hist)

    async def reset(self) -> int:
        db = self.app.db
        base = await db.kv_get("tune_base", {}) or {}
        for k, v in base.items():
            set_cfg(self.cfg.raw, k, v)
        await db.kv_set("tuned", {})
        await db.kv_set("tune_base", {})
        await db.kv_set("tune_pending", None)
        return len(base)

    def current(self, params: tuple[Param, ...]) -> dict:
        return {p.key: float(self.cfg.get(p.key, p.default) if self.cfg.get(p.key) is not None else p.default)
                for p in params}

    # ---------------- 数据
    async def entry_cases(self, days: float = 4) -> tuple[list[Case], list[Case]]:
        rows = await self.app.db.fetchall(
            "SELECT ts, price, p15, p60, p240, features, source FROM token_outcomes WHERE ts>? AND price>0 "
            "AND p60 IS NOT NULL AND p60>0", (time.time() - days * 86400,))
        min_liq = float(self.cfg.get("trading.min_liquidity_usd", 8000))
        newtok, filt = [], []
        for r in rows:
            if str(r["source"] or "") == "whynot":
                continue
            ret = proxy_return(f(r["price"]), f(r["p60"]), f(r["p240"]) or None, f(r["p15"]) or None)
            if ret is None:
                continue
            ft = json.loads(r["features"] or "{}")
            newtok.append(Case(f(r["ts"]), lambda v, ft=ft, ret=ret: ret if entry_pass(ft, v, min_liq) else None))
            # 通用过滤只在"有人可能会买"的币上评估：有聪明钱买入，或评分不低
            if int(f(ft.get("smart"))) >= 1 or f(ft.get("score")) >= 60:
                filt.append(Case(f(r["ts"]), lambda v, ft=ft, ret=ret: ret if filter_pass(ft, v, min_liq) else None))
        return newtok, filt

    async def early_cases(self, days: float = 4) -> list[Case]:
        db = self.app.db
        toks = await db.fetchall("SELECT chain, token, first_ts, p60, p240 FROM early_tok WHERE first_ts>? "
                                 "AND p240 IS NOT NULL AND p240>0", (time.time() - days * 86400,))
        if not toks:
            return []
        from .strategies import EARLY_DEFAULTS
        base = {**EARLY_DEFAULTS, **(self.cfg.get("strategies.early", {}) or {})}
        obs: dict[tuple[str, str], list[dict]] = {}
        for r in await db.fetchall("SELECT * FROM early_obs WHERE ts>? ORDER BY ts", (time.time() - days * 86400 - 6 * 3600,)):
            obs.setdefault((r["chain"], r["token"]), []).append(dict(r))
        cases = []
        for t in toks:
            ss = obs.get((t["chain"], t["token"])) or []
            if len(ss) < 2:
                continue

            def ret_fn(v: dict, ss=ss, t=dict(t)) -> float | None:
                i = early_entry(ss, v, base)
                if i is None:
                    return None
                entry = f(ss[i]["price"]) * 1.02          # 买入滑点
                if entry <= 0:
                    return None
                # 结果价是"第一次看到后 1 小时 / 4 小时"，入场晚于第一次看到时这是近似
                return proxy_return(entry, f(t["p60"]) or None, f(t["p240"]) or None)
            cases.append(Case(f(t["first_ts"]), ret_fn))
        return cases

    async def fill_early(self) -> None:
        """早期观察样本的结果：第一次看到后 1 小时 / 4 小时的价格。"""
        db = self.app.db
        now = time.time()
        for col, mins in (("p60", 60), ("p240", 240)):
            rows = await db.fetchall(f"SELECT chain, token FROM early_tok WHERE {col} IS NULL AND first_ts<? "
                                     "AND first_ts>? LIMIT 80", (now - mins * 60, now - mins * 60 - 6 * 3600))
            by: dict[str, list[str]] = {}
            for r in rows:
                by.setdefault(r["chain"], []).append(r["token"])
            for chain, lst in by.items():
                px = await self.app.data.prices(chain, lst)
                await db.executemany(f"UPDATE early_tok SET {col}=? WHERE chain=? AND token=?",
                                     [(px.get(tk) or -1, chain, tk) for tk in lst])

    # ---------------- 调参
    async def tune_once(self, force: bool = False) -> list[str]:
        """跑一轮调参，返回给 TG 的说明。"""
        mode = await self.mode()
        if mode == "off" and not force:
            return []
        db = self.app.db
        recent = {h["key"] for h in (await db.kv_get("tune_log", []) or []) if time.time() - h["ts"] < 86400}
        newtok, filt = await self.entry_cases()
        early = await self.early_cases()
        out: list[str] = []
        found = []
        cur_e = self.current(ENTRY_PARAMS)
        cur_x = self.current(EARLY_PARAMS)
        # 纯计算，放到线程里跑，不卡住止盈止损
        a = await asyncio.to_thread(tune, newtok, tuple(p for p in ENTRY_PARAMS if p.key in NEWTOKEN_KEYS), cur_e,
                                    15, recent)
        b = await asyncio.to_thread(tune, filt, tuple(p for p in ENTRY_PARAMS if p.key not in NEWTOKEN_KEYS), cur_e,
                                    15, recent)
        c = await asyncio.to_thread(tune, early, EARLY_PARAMS, cur_x, 10, recent)
        for name, r in (("新币买入", a), ("风控过滤", b), ("早期建仓", c)):
            if r:
                found.append((name, r))
        if mode != "off" or force:
            try:
                out += await self.tune_exits(force)
            except Exception as e:  # noqa: BLE001
                log.warning("卖出规则调整失败: %s", e)
        if force:
            out.append(f"数据：结果档案 {len(newtok)} 个币（其中 {len(filt)} 个用于风控过滤），早期观察 {len(early)} 个币")
        if not found:
            if force:
                out.append("暂时没有可信的改进（要两段数据都更好、改变的币够多、去掉最赚的一个依然成立）")
            return out
        # 每轮只改一个：选改进最大的
        name, r = max(found, key=lambda x: x[1]["gain"])
        loosen = (r["new"] < r["old"]) == (".min_" in r["key"] or r["key"].endswith(("score", "cluster")))
        why = (f"{name}：{r['label']} {_num(r['old'])} → {_num(r['new'])}（{'放宽' if loosen else '收紧'}）。"
               f"最近 {r['cases']} 个币里，会买的从 {r['n_old']} 个变成 {r['n_new']} 个，"
               f"近似每笔收益 {r['exp_old'] * 100:+.1f}% → {r['exp_new'] * 100:+.1f}%，"
               f"合计 {r['sum_old'] * 100:+.0f}% → {r['sum_new'] * 100:+.0f}%（按每笔同样金额）")
        if mode == "auto" and self.cfg.dry_run:
            await self.apply(r["key"], r["new"], why)
            out.append("🧠 <b>自我调参</b>（模拟盘自动生效）\n" + html.escape(why)
                       + "\n不满意可以发 /tune reset 恢复配置文件里的值")
        else:
            await db.kv_set("tune_pending", {"key": r["key"], "new": r["new"], "why": why, "ts": time.time()})
            out.append("🧠 <b>调参建议</b>（" + ("实盘" if not self.cfg.dry_run else "建议模式") + "不自动改）\n"
                       + html.escape(why) + "\n同意就发 /tune apply")
        return out

    # ---------------- 卖出规则自动调整（用真实入场回放 K 线）
    def exit_candidates(self, exit_cfg: dict, skip: set[str]) -> list[tuple[str, Any, str, dict]]:
        out: list[tuple[str, Any, str, dict]] = []
        stop = float(exit_cfg.get("stop_loss_pct", -35))
        if "exit.stop_loss_pct" not in skip:
            i = min(range(len(STOPS)), key=lambda j: abs(STOPS[j] - stop))
            for j in (i - 1, i + 1):
                if 0 <= j < len(STOPS) and STOPS[j] != stop:
                    out.append(("exit.stop_loss_pct", STOPS[j], f"止损 {stop:g}% → {STOPS[j]}%",
                                {"stop_loss_pct": STOPS[j]}))
        rn = dict(exit_cfg.get("runner") or {})
        if rn.get("enabled"):
            lad = rn.get("ladder") or [{"drop_pct": 25, "sell_pct": 33}, {"drop_pct": 40, "sell_pct": 50},
                                       {"drop_pct": 55, "sell_pct": 100}]
            sells = [x.get("sell_pct", 100) for x in lad][:3] + [100] * (3 - min(3, len(lad)))
            first = float(lad[0].get("drop_pct", 25))
            if "exit.runner.ladder" not in skip:
                i = min(range(len(LADDERS)), key=lambda j: abs(LADDERS[j][0] - first))
                for j in (i - 1, i + 1):
                    if 0 <= j < len(LADDERS) and LADDERS[j][0] != first:
                        new = [{"drop_pct": d, "sell_pct": s} for d, s in zip(LADDERS[j], sells)]
                        out.append(("exit.runner.ladder", new,
                                    "强势长拿回撤档位 " + "/".join(f"{x.get('drop_pct')}" for x in lad) + "% → "
                                    + "/".join(str(d) for d in LADDERS[j]) + "%", {"runner": {**rn, "ladder": new}}))
            fl = float(rn.get("floor_gain_pct", 20))
            if "exit.runner.floor_gain_pct" not in skip:
                i = min(range(len(FLOORS)), key=lambda j: abs(FLOORS[j] - fl))
                for j in (i - 1, i + 1):
                    if 0 <= j < len(FLOORS) and FLOORS[j] != fl:
                        out.append(("exit.runner.floor_gain_pct", FLOORS[j], f"强势长拿保底线 +{fl:g}% → +{FLOORS[j]}%",
                                    {"runner": {**rn, "floor_gain_pct": FLOORS[j]}}))
        return out

    async def tune_exits(self, force: bool = False) -> list[str]:
        if not force and time.time() - self.last_exit < 24 * 3600:
            return []
        self.last_exit = time.time()
        from .trade.replay import collect, replay_one
        trades, _ = await collect(self.app, float(self.cfg.get("learning.exit_days", 4)))
        min_n = int(self.cfg.get("learning.exit_min_trades", 30))
        if len(trades) < min_n:
            return [f"卖出规则：可回放的单子 {len(trades)} 笔（要 ≥ {min_n}），暂不调整"] if force else []
        exit_cfg = self.cfg.get("exit", {}) or {}
        cost = float(exit_cfg.get("shadow_cost_pct", 2)) / 100
        follow = bool(exit_cfg.get("follow_smart_sell", True))
        recent = {h["key"] for h in (await self.app.db.kv_get("tune_log", []) or []) if time.time() - h["ts"] < 86400}
        cands = self.exit_candidates(exit_cfg, recent)
        trades.sort(key=lambda t: t["opened_at"])

        def evaluate() -> dict | None:
            base = [replay_one(t, exit_cfg, None, follow, cost) for t in trades]
            cut = int(len(base) * 0.6)
            best = None
            for key, val, label, ov in cands:
                new = [replay_one(t, exit_cfg, ov, follow, cost) for t in trades]
                d = [n - b for n, b in zip(new, base)]
                if sum(d[:cut]) <= 0 or sum(d[cut:]) <= 0 or sum(d) - max(d) <= 0:
                    continue
                if not best or sum(d) > best["gain"]:
                    best = {"key": key, "new": val, "label": label, "gain": sum(d), "n": len(d),
                            "old_avg": sum(base) / len(base) - 1, "new_avg": sum(new) / len(new) - 1,
                            "better": sum(1 for x in d if x > 1e-9), "worse": sum(1 for x in d if x < -1e-9)}
            return best
        r = await asyncio.to_thread(evaluate)
        if not r:
            return ["卖出规则：回放了 " + str(len(trades)) + " 笔，相邻的几种改法都没有可信的改进"] if force else []
        why = (f"卖出规则：{r['label']}。用最近 {r['n']} 笔真实入场回放 K 线，平均每笔 {r['old_avg'] * 100:+.1f}% → "
               f"{r['new_avg'] * 100:+.1f}%（更好 {r['better']} 笔 / 更差 {r['worse']} 笔，前后两段都更好）")
        if await self.mode() == "auto" and self.cfg.dry_run:
            await self.apply(r["key"], r["new"], why)
            return ["🧠 <b>自我调参</b>（模拟盘自动生效）\n" + html.escape(why) + "\n不满意可以发 /tune reset"]
        await self.app.db.kv_set("tune_pending", {"key": r["key"], "new": r["new"], "why": why, "ts": time.time()})
        return ["🧠 <b>调参建议</b>（不自动改）\n" + html.escape(why) + "\n同意就发 /tune apply"]

    async def status_text(self) -> str:
        db = self.app.db
        mode = await self.mode()
        names = {"auto": "自动（模拟盘自动生效，实盘只建议）", "suggest": "只建议", "off": "关闭"}
        lines = [f"<b>🧠 自我学习</b>：{names[mode]}，每 6 小时学一次"]
        tuned = await db.kv_get("tuned", {}) or {}
        base = await db.kv_get("tune_base", {}) or {}
        labels = {p.key: p.label for p in ENTRY_PARAMS + EARLY_PARAMS}
        labels.update({"exit.stop_loss_pct": "止损线", "exit.runner.ladder": "强势长拿回撤档位",
                       "exit.runner.floor_gain_pct": "强势长拿保底线"})
        if tuned:
            lines.append("学到的参数（覆盖配置文件）：")
            lines += [f"  {labels.get(k, k)}：{_fmt(base.get(k))} → {_fmt(v)}" for k, v in tuned.items()]
        else:
            lines.append("还没有改过参数（数据够多、结论可信才会改）")
        pend = await db.kv_get("tune_pending", None)
        if pend:
            lines.append("待确认的建议：" + html.escape(pend["why"]) + "（/tune apply 采纳）")
        for h in (await db.kv_get("tune_log", []) or [])[-5:][::-1]:
            lines.append(f"  {time.strftime('%m-%d %H:%M', time.localtime(h['ts']))} "
                         f"{labels.get(h['key'], h['key'])} {_fmt(h['old'])} → {_fmt(h['new'])}")
        n1 = await db.fetchone("SELECT COUNT(*) n FROM token_outcomes WHERE p60 IS NOT NULL AND p60>0 AND ts>?",
                               (time.time() - 4 * 86400,))
        n2 = await db.fetchone("SELECT COUNT(*) n FROM early_tok WHERE p240 IS NOT NULL AND p240>0 AND first_ts>?",
                               (time.time() - 4 * 86400,))
        lines.append(f"学习样本（最近 4 天）：结果档案 {n1['n']} 个，早期观察 {n2['n']} 个")
        lines.append("命令：/tune now 立即学一次｜/tune auto / suggest / off｜/tune apply 采纳建议｜/tune reset 全部恢复")
        return "\n".join(lines)

    async def run(self) -> None:
        await asyncio.sleep(300)
        every = float(self.cfg.get("learning.tune_hours", 6)) * 3600
        while True:
            try:
                await self.fill_early()
                if time.time() - self.last_tune > every:
                    self.last_tune = time.time()
                    for msg in await self.tune_once():
                        await self.app.tg.send(msg)
            except Exception as e:  # noqa: BLE001
                log.exception("自我学习出错: %s", e)
            await asyncio.sleep(300)
