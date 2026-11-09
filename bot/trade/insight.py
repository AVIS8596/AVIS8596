"""从结果档案里学习：每个分析过的币当时的数据 vs 之后的涨跌，找出哪些特征真的能预测上涨。

这是"自主学习"的第一步：先让数据说话（每一项特征分档，看各档后来的涨跌），
样本够多以后再用这些规律自动给新币打"上涨概率"、自动提出参数调整建议。
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from ..utils import f, sane_ratio

if TYPE_CHECKING:
    from ..app import App

H_NAME = {"p15": "15 分钟", "p60": "1 小时", "p240": "4 小时"}

# (特征, 显示名, 分档边界)
BUCKETS = [
    ("score", "代币评分", [40, 55, 70]),
    ("smart", "聪明钱数量", [1, 2, 3]),
    ("bundle_held_pct", "捆绑持有%", [5, 15, 25]),
    ("holders", "持有人", [150, 500, 1500]),
    ("age_hours", "上线小时", [1, 6, 24]),
    ("liquidity", "流动性$", [10_000, 30_000, 100_000]),
    ("change_1h", "1h涨幅%", [0, 100, 300]),
    ("buy_sell", "买卖笔数比", [1, 1.5, 2.5]),
    ("top10_pct", "前10持仓%", [15, 25, 40]),
    ("cabal_pct", "阴谋地址持仓%", [0.01, 3, 10]),
    ("early_finder_pct", "早期发现者持仓%", [0.01, 1, 5]),
]


def _label(edges: list[float], i: int) -> str:
    def k(x: float) -> str:
        return f"{x / 1000:g}K" if abs(x) >= 1000 else f"{x:g}"
    if i == 0:
        return f"＜{k(edges[0])}"   # 全角，避免 Telegram HTML 把 < 当标签
    if i == len(edges):
        return f"≥{k(edges[-1])}"
    return f"{k(edges[i - 1])}~{k(edges[i])}"


def _stats(rs: list[float]) -> dict:
    rs = sorted(rs)
    capped = [min(r, 3.0) for r in rs]   # 单个百倍币不能把平均值带飞：收益按最多 +300% 计
    return {"n": len(rs), "med": rs[len(rs) // 2], "mean": sum(capped) / len(capped),
            "hit": sum(1 for r in rs if r >= 0.3) / len(rs), "dump": sum(1 for r in rs if r <= -0.3) / len(rs)}


def analyze_rows(rows: list[dict], horizon: str) -> dict:
    """纯计算：返回总体统计、各特征分档统计、买过的 vs 全部。"""
    data = []
    for r in rows:
        p0, p1 = f(r.get("price")), f(r.get(horizon))
        if p0 <= 0 or p1 <= 0 or not sane_ratio(p1 / p0):
            continue
        feats = json.loads(r.get("features") or "{}")
        b, s = f(feats.get("buys_1h")), f(feats.get("sells_1h"))
        feats["buy_sell"] = b / s if s > 0 else (3.0 if b > 0 else 0.0)
        data.append((feats, p1 / p0 - 1, int(r.get("bought") or 0), str(r.get("source") or "")))
    if not data:
        return {"n": 0}
    out = {"n": len(data), "all": _stats([d[1] for d in data]), "features": {}}
    for key, name, edges in BUCKETS:
        groups: dict[int, list[float]] = {}
        for feats, ret, _, _ in data:
            v = feats.get(key)
            if v is None:
                continue
            i = sum(1 for e in edges if f(v) >= e)
            groups.setdefault(i, []).append(ret)
        out["features"][name] = [(_label(edges, i), _stats(g)) for i, g in sorted(groups.items()) if len(g) >= 10]
    bought = [d[1] for d in data if d[2]]
    if bought:
        out["bought"] = _stats(bought)
    return out


async def feature_report(app: "App", horizon: str = "p60", days: float = 3) -> str:
    rows = await app.db.fetchall(
        f"SELECT price, features, bought, source, {horizon} FROM token_outcomes WHERE ts>? AND {horizon} IS NOT NULL",
        (time.time() - days * 86400,))
    res = analyze_rows([dict(r) for r in rows], horizon)
    total = await app.db.fetchone("SELECT COUNT(*) n FROM token_outcomes")
    if res["n"] < 50:
        return (f"结果档案共 {total['n']} 条，其中有{H_NAME[horizon]}后价格的只有 {res['n']} 条，"
                f"至少 50 条才有统计意义。机器人每天会分析几百个币，过几个小时再看。")
    a = res["all"]
    lines = [f"<b>从结果里学习</b>（最近 {days:g} 天 {res['n']} 个币，看{H_NAME[horizon]}后的涨跌）",
             f"全部：中位 {a['med'] * 100:+.0f}%，平均 {a['mean'] * 100:+.0f}%，"
             f"涨超 30% 的占 {a['hit'] * 100:.0f}%，跌超 30% 的占 {a['dump'] * 100:.0f}%"]
    if "bought" in res:
        b = res["bought"]
        lines.append(f"机器人买过的 {b['n']} 个：中位 {b['med'] * 100:+.0f}%，平均 {b['mean'] * 100:+.0f}%，"
                     f"涨超 30% 占 {b['hit'] * 100:.0f}%（比全部好说明选币有效）")
    best, worst = [], []
    for name, groups in res["features"].items():
        if len(groups) < 2:
            continue
        lines.append(f"\n<b>{name}</b>")
        for lab, st in groups:
            lines.append(f"  {lab}：{st['n']} 个，中位 {st['med'] * 100:+.0f}%，涨超30% {st['hit'] * 100:.0f}%，"
                         f"跌超30% {st['dump'] * 100:.0f}%")
            edge = st["hit"] - st["dump"]
            best.append((edge, f"{name} {lab}"))
            worst.append((edge, f"{name} {lab}"))
    best.sort(reverse=True)
    worst.sort()
    lines.append("\n<b>发现</b>（涨超30%的比例 − 跌超30%的比例）")
    lines.append("最好的几档：" + "、".join(f"{n}（{e * 100:+.0f}）" for e, n in best[:3]))
    lines.append("最差的几档：" + "、".join(f"{n}（{e * 100:+.0f}）" for e, n in worst[:3]))
    lines.append("样本越多越可靠；规律稳定后会用来自动调整选币条件（先观察验证）")
    return "\n".join(lines)
