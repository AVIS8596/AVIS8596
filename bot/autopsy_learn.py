"""学高收益地址的买币逻辑（/autopsy 的主要部分）：它们在什么样的时机、买什么样的币？学来给机器人自己选币用。

每笔仓位（它们最近 6 天买过、已经卖完的币）在"它们买入那一刻"的样子：
  - 从 K 线还原（历史数据，每笔都有）：发币多久、入场市值、买入前 5 / 15 / 60 分钟涨了多少、
    离 1 小时内高点跌了多少、1 小时内从低点已经拉了多少、买入前 5 分钟成交额；
  - 从机器人自己的结果档案对上（机器人当时也分析过这个币才有）：持有人、前 10 持仓、捆绑、聪明钱数、流动性、
    有没有推特、代币评分等。
然后：
  1. 它们挑的币普遍长什么样（中位数）；
  2. 同样是它们挑的，赚钱的单和亏钱的单差在哪（按特征分档看胜率）；
  3. 把"赚钱的单的特征范围"变成候选规则，放到机器人最近 6 天见过的全部币上回测：
     命中规则的币之后表现是不是比全部币好——只有这一步通过的规则，才说明"学得来、自己能用"。
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .utils import f, fmt_usd

if TYPE_CHECKING:
    from .app import App

WIN = 0.3         # 它自己赚 30% 以上算"赚钱的单"

# 特征：(键, 名字, 分档边界, 显示格式)
KLINE_FEATS = [
    ("age_min", "发币多久（分钟）", [5, 30, 120, 720], "{:.0f}"),
    ("mcap", "入场市值", [30_000, 100_000, 300_000, 1_000_000], "usd"),
    ("chg5", "买前 5 分钟涨跌", [-0.1, 0, 0.1, 0.3], "pct"),
    ("chg60", "买前 1 小时涨跌", [-0.2, 0, 0.5, 2], "pct"),
    ("run_up", "1 小时内从低点已拉", [0.2, 0.5, 1, 3], "pct"),
    ("dd", "离 1 小时内高点跌了", [0.1, 0.2, 0.35, 0.5], "pct"),
    ("vol5", "买前 5 分钟成交额", [2_000, 10_000, 50_000, 200_000], "usd"),
]
SNAP_FEATS = [
    ("holders", "持有人", [100, 300, 1000, 3000], "{:.0f}"),
    ("top10_pct", "前 10 持仓%", [15, 25, 35, 50], "{:.0f}"),
    ("bundle_held_pct", "捆绑持仓%", [1, 5, 10, 20], "{:.0f}"),
    ("smart", "聪明钱数", [1, 2, 3, 5], "{:.0f}"),
    ("liquidity", "流动性", [10_000, 30_000, 100_000, 300_000], "usd"),
    ("has_x", "有推特", [1], "{:.0f}"),
    ("score", "代币评分", [40, 55, 70, 85], "{:.0f}"),
]
# 结果档案里的同名特征（回测用）：键 → 从 features 里怎么取
BASE_MAP = {"age_min": lambda ft: f(ft.get("age_hours")) * 60 if ft.get("age_hours") is not None else None,
            "mcap": lambda ft: ft.get("mcap"), "holders": lambda ft: ft.get("holders"),
            "top10_pct": lambda ft: ft.get("top10_pct"), "bundle_held_pct": lambda ft: ft.get("bundle_held_pct"),
            "smart": lambda ft: ft.get("smart"), "liquidity": lambda ft: ft.get("liquidity"),
            "has_x": lambda ft: ft.get("has_x"), "score": lambda ft: ft.get("score"),
            "chg60": lambda ft: f(ft.get("change_1h")) / 100 if ft.get("change_1h") is not None else None}
SNAP_KEYS = ("holders", "top10_pct", "bundle_held_pct", "smart", "liquidity", "has_x", "has_web", "score",
             "fresh_pct", "bot_pct", "rat_trade_pct", "cabal_pct", "dev_held_pct", "volume_1h", "change_1h")


def _med(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


def _q(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))] if s else 0.0


def _fmt(v: float, fmt: str) -> str:
    if fmt == "usd":
        return fmt_usd(v)
    if fmt == "pct":
        return f"{v * 100:+.0f}%"
    return fmt.format(v)


def _candle_t(c: dict) -> float:
    t = f(c.get("time"))
    return t / 1000 if t > 1e12 else t


def kline_features(candles: list[dict], t: float, created: float | None, supply: float | None,
                   px_buy: float) -> dict:
    """它买入那一刻（t）之前的 K 线 → 动量、离高点多远、已经拉了多少、成交额。"""
    before = sorted((c for c in candles if _candle_t(c) < t - 30), key=_candle_t)    # 买入那一分钟之前的
    out: dict[str, Any] = {"age_min": (t - created) / 60 if created and t >= created else None,
                           "mcap": px_buy * supply if supply else None}
    if not before:
        return out
    p0 = f(before[-1].get("close"))
    if p0 <= 0:
        return out

    def px_ago(sec: float) -> float:
        old = [c for c in before if _candle_t(c) <= t - sec]
        return f(old[-1].get("close")) if old else 0.0
    for key, sec in (("chg5", 300), ("chg15", 900), ("chg60", 3600)):
        a = px_ago(sec)
        out[key] = p0 / a - 1 if a > 0 else None
    hi = max((f(c.get("high")) or f(c.get("close")) for c in before), default=0.0)
    lo = min((f(c.get("low")) or f(c.get("close")) for c in before if (f(c.get("low")) or f(c.get("close"))) > 0),
             default=0.0)
    out["dd"] = 1 - p0 / hi if hi > 0 else None
    out["run_up"] = p0 / lo - 1 if lo > 0 else None
    v5 = [f(c.get("volume")) for c in before if _candle_t(c) >= t - 330]
    out["vol5"] = sum(v5) if any(v5) else None
    return out


async def snapshot(app: "App", chain: str, token: str, t: float) -> dict:
    """机器人自己当时对这个币的记录（买入前 30 分钟 ~ 后 5 分钟内最近的一次分析）。"""
    r = await app.db.fetchone(
        "SELECT features FROM token_outcomes WHERE chain=? AND token=? AND ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts - ?) LIMIT 1", (chain, token, t - 1800, t + 300, t))
    if not r:
        return {}
    try:
        ft = json.loads(r["features"] or "{}") or {}
    except (TypeError, ValueError):
        return {}
    return {k: ft.get(k) for k in SNAP_KEYS if ft.get(k) is not None}


def profile(trs: list[dict]) -> list[str]:
    """它们挑的币普遍长什么样；赚钱的单 vs 亏钱的单。"""
    win = [t for t in trs if t["own"] >= WIN]
    lose = [t for t in trs if t["own"] <= 0]
    lines = [f"  共 {len(trs)} 笔：赚 30% 以上 {len(win)} 笔、亏钱 {len(lose)} 笔（其余小赚）"]
    for key, name, _, fmt in KLINE_FEATS + SNAP_FEATS:
        allv = [f(t["x"][key]) for t in trs if t["x"].get(key) is not None]
        if len(allv) < 10:
            continue
        wv = [f(t["x"][key]) for t in win if t["x"].get(key) is not None]
        lv = [f(t["x"][key]) for t in lose if t["x"].get(key) is not None]
        s = f"  {name}：中位 {_fmt(_med(allv), fmt)}（多数在 {_fmt(_q(allv, .25), fmt)} ~ {_fmt(_q(allv, .75), fmt)}）"
        if len(wv) >= 5 and len(lv) >= 5:
            s += f" | 赚钱的单 {_fmt(_med(wv), fmt)} vs 亏钱的单 {_fmt(_med(lv), fmt)}"
        if key in dict((k, 1) for k, *_ in SNAP_FEATS):
            s += f"（{len(allv)} 笔对上了机器人的记录）"
        lines.append(s)
    return lines


def separating(trs: list[dict], top: int = 5) -> list[str]:
    """按特征分档，看每档的胜率（它自己赚 30% 以上的比例），找出最能区分好坏的特征。"""
    scored = []
    for key, name, edges, fmt in KLINE_FEATS + SNAP_FEATS:
        rows = [(f(t["x"][key]), t["own"]) for t in trs if t["x"].get(key) is not None]
        if len(rows) < 20:
            continue
        bins, lo = [], float("-inf")
        for hi in edges + [float("inf")]:
            xs = [o for v, o in rows if lo <= v < hi]
            if len(xs) >= 5:
                lab = (f"< {_fmt(hi, fmt)}" if lo == float("-inf") else f"≥ {_fmt(lo, fmt)}" if hi == float("inf")
                       else f"{_fmt(lo, fmt)}~{_fmt(hi, fmt)}")
                bins.append((lab, len(xs), sum(1 for o in xs if o >= WIN) / len(xs), _med(xs)))
            lo = hi
        if len(bins) >= 2:
            spread = max(b[2] for b in bins) - min(b[2] for b in bins)
            scored.append((spread, name, bins))
    scored.sort(key=lambda x: -x[0])
    out = []
    for spread, name, bins in scored[:top]:
        out.append(f"  {name}：" + "；".join(f"{lab} {n} 笔 胜率 {wr * 100:.0f}%、中位 {med * 100:+.0f}%"
                                         for lab, n, wr, med in bins))
    return out


def candidate_rules(trs: list[dict]) -> list[tuple[str, str, float, float]]:
    """赚钱的单在各特征上的中间一半范围（25%~75%）→ 候选规则 (键, 说明, 下限, 上限)。只用回测能验证的特征。"""
    win = [t for t in trs if t["own"] >= WIN]
    out = []
    for key, name, _, fmt in KLINE_FEATS + SNAP_FEATS:
        if key not in BASE_MAP:
            continue
        wv = [f(t["x"][key]) for t in win if t["x"].get(key) is not None]
        if len(wv) < 8:
            continue
        lo, hi = _q(wv, .25), _q(wv, .75)
        if key == "has_x":
            if lo >= 1:
                out.append((key, "有推特", 1, 1))
            continue
        out.append((key, f"{name} {_fmt(lo, fmt)}~{_fmt(hi, fmt)}", lo, hi))
    return out


async def baseline(app: "App", chain: str, days: float) -> list[dict]:
    """机器人最近几天见过的全部币（每个币取第一次分析）：特征 + 之后的结果（影子采样优先，归零按亏光）。"""
    from .brain import proxy_return
    rows = await app.db.fetchall(
        "SELECT token, price, p15, p60, p240, sim_ret, features FROM token_outcomes WHERE chain=? AND ts>? "
        "AND price>0 AND p60 IS NOT NULL AND p60<>0 ORDER BY ts", (chain, time.time() - days * 86400))
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["token"] in seen:
            continue
        seen.add(r["token"])
        sim = r["sim_ret"] if r["sim_ret"] is not None and -1 <= float(r["sim_ret"]) < 199 else None
        ret = proxy_return(f(r["price"]), f(r["p60"]), f(r["p240"]) or None, f(r["p15"]) or None, sim=sim, dead=True)
        if ret is None:
            continue
        try:
            ft = json.loads(r["features"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        x = {}
        for k, fn in BASE_MAP.items():
            try:
                v = fn(ft)
            except (TypeError, ValueError):
                v = None
            if v is not None:
                x[k] = f(v)
        out.append({"x": x, "ret": ret})
    return out


def test_rules(rules: list[tuple[str, str, float, float]], base: list[dict]) -> list[str]:
    """候选规则放到全部币上回测：命中规则的币之后每笔平均 / 中位、涨 1 倍以上的比例，对比全部币。"""
    if len(base) < 100:
        return [f"  机器人最近见过的币太少（{len(base)} 个），回测不了"]

    def stats(xs: list[float]) -> tuple[float, float, float]:
        return sum(xs) / len(xs), _med(xs), sum(1 for x in xs if x >= 1) / len(xs)
    allr = [b["ret"] for b in base]
    a0, m0, b0 = stats(allr)
    lines = [f"  全部 {len(base)} 个币：每笔平均 {a0 * 100:+.0f}%、中位 {m0 * 100:+.0f}%、翻倍 {b0 * 100:.0f}%（对照组）"]

    def hit(b: dict, r: tuple) -> bool:
        k, _, lo, hi = r
        v = b["x"].get(k)
        return v is not None and lo <= v <= hi
    res = []
    for r in rules:
        xs = [b["ret"] for b in base if hit(b, r)]
        if len(xs) >= 30:
            res.append((r[1], len(xs), *stats(xs), [r]))
    # 两两组合：单条里最好的前 4 条两两组合
    best = sorted(res, key=lambda x: -x[2])[:4]
    for i in range(len(best)):
        for j in range(i + 1, len(best)):
            rs = best[i][5] + best[j][5]
            xs = [b["ret"] for b in base if all(hit(b, r) for r in rs)]
            if len(xs) >= 30:
                res.append((best[i][0] + " 且 " + best[j][0], len(xs), *stats(xs), rs))
    res.sort(key=lambda x: -x[2])
    for name, n, a, m, big, _ in res[:8]:
        mark = "✅" if a > a0 + 0.05 and m >= m0 else "➖" if a > a0 else "❌"
        lines.append(f"  {mark} {name}：{n} 个币，平均 {a * 100:+.0f}%、中位 {m * 100:+.0f}%、翻倍 {big * 100:.0f}%")
    if not res:
        lines.append("  没有样本够多（≥ 30 个币）的候选规则")
    lines.append("  （✅ = 平均比对照组高 5 个点以上、中位不比对照组差：学得来、值得放进观察；"
                 "结果是近似值，影子采样的币按真实卖出规则模拟，其余按 1 / 4 小时价格估算）")
    return lines
