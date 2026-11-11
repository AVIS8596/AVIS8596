"""市场冷热：最近几小时机器人分析过的币，1 小时后整体是涨是跌。

冷：大部分币买入 1 小时后都在跌 → 仓位减半（可配置成暂停买入）
热：涨的多、跌的少 → 正常仓位
样本不够（< 30 个）时按"正常"处理。用的是已经发生的真实结果，比看 SOL 涨跌更直接。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import App

NAME = {"hot": "🔥热", "normal": "🌤正常", "cold": "🧊冷", "unknown": "❔样本不够"}
_cache: dict = {"ts": 0.0, "val": None}


def classify(rets: list[float]) -> dict:
    if len(rets) < 30:
        return {"state": "unknown", "n": len(rets), "med": 0.0, "hit": 0.0, "dump": 0.0}
    rs = sorted(rets)
    med = rs[len(rs) // 2]
    hit = sum(1 for r in rs if r >= 0.3) / len(rs)
    dump = sum(1 for r in rs if r <= -0.3) / len(rs)
    if med <= -0.25 or hit - dump <= -0.3:
        state = "cold"
    elif med >= 0 and hit >= dump:
        state = "hot"
    else:
        state = "normal"
    return {"state": state, "n": len(rs), "med": med, "hit": hit, "dump": dump}


async def regime(app: "App") -> dict:
    """带 10 分钟缓存。返回 {state, n, med, hit, dump, mult, pause}。"""
    if _cache["val"] and time.time() - _cache["ts"] < 600:
        return _cache["val"]
    rows = await app.db.fetchall(
        "SELECT price, p60 FROM token_outcomes WHERE ts BETWEEN ? AND ? AND price>0 AND p60>0",
        (time.time() - 7 * 3600, time.time() - 3600))
    res = classify([r["p60"] / r["price"] - 1 for r in rows])
    cfg = app.cfg.get("trading.regime", {}) or {}
    res["mult"] = float(cfg.get(res["state"], 0.5 if res["state"] == "cold" else 1))
    res["pause"] = res["state"] == "cold" and bool(cfg.get("cold_pause", False))
    _cache.update(ts=time.time(), val=res)
    return res


def text(r: dict) -> str:
    if r["state"] == "unknown":
        return f"市场冷热：{NAME['unknown']}（{r['n']} 个，至少 30 个）"
    act = "暂停买入" if r.get("pause") else (f"仓位 ×{r['mult']:g}" if r.get("mult", 1) != 1 else "正常仓位")
    return (f"市场冷热：{NAME[r['state']]}（最近 6 小时 {r['n']} 个币，1 小时后中位 {r['med'] * 100:+.0f}%，"
            f"涨超30% {r['hit'] * 100:.0f}% / 跌超30% {r['dump'] * 100:.0f}%）→ {act}")
