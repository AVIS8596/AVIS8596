"""每套打法给每个币打的分，统一记在 lane_scores（结果档案 id → 打法 → 分数），两天后合成"总打法"用。

打法：学来的打法（买入判断的分数）、钻石手打法（会不会翻倍的分数）、买卖力量打法（1 = 会买）、选币模型。
分数 -1 = 不在这套打法的出手范围 / 没过它的硬性排雷。只记数据，不影响买卖。
"""
from __future__ import annotations

import time


async def lane_score(db, oid: int | None, lane: str, score: float) -> None:
    if not oid:
        return
    try:
        await db.execute("INSERT OR REPLACE INTO lane_scores(oid,lane,score,ts) VALUES(?,?,?,?)",
                         (int(oid), lane, float(score), time.time()))
    except Exception:  # noqa: BLE001
        pass


async def lane_report(app, days: float = 4) -> list[str]:
    """各套打法的分数高低 vs 之后的结果（影子采样：同样卖法模拟到底）。"""
    db = app.db
    try:
        rows = await db.fetchall(
            "SELECT l.lane, l.score, o.sim_ret FROM lane_scores l JOIN token_outcomes o ON o.id=l.oid "
            "WHERE l.ts>? AND o.sim_ret BETWEEN -1 AND 199", (time.time() - days * 86400,))
    except Exception:  # noqa: BLE001
        return []
    by: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        by.setdefault(r["lane"], []).append((float(r["score"]), float(r["sim_ret"])))
    if not by:
        return ["<b>各打法打分 vs 结果</b>：还没有数据（币要满 4 小时、被影子采样到才有结果）"]
    L = ["<b>各打法打分 vs 结果</b>（同一批币、同样卖法；合成总打法的原料）"]
    for lane, xs in sorted(by.items()):
        inr = sorted((s, r) for s, r in xs if s >= 0)
        out = [r for s, r in xs if s < 0]
        if len(inr) < 10:
            L.append(f"  {lane}：范围内 {len(inr)} 个（太少）")
            continue
        top = [r for _, r in inr[len(inr) * 2 // 3:]]           # 分数最高的三分之一
        low = [r for _, r in inr[:len(inr) // 3]]
        avg = lambda v: sum(v) / len(v) * 100 if v else 0.0     # noqa: E731
        L.append(f"  {lane}：分数最高的 1/3 平均 {avg(top):+.1f}% vs 最低的 1/3 {avg(low):+.1f}%"
                 + (f"，范围外 {avg(out):+.1f}%（{len(out)} 个）" if out else "") + f"（共 {len(xs)} 个）")
    return L
