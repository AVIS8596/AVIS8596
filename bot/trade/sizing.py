"""仓位按证据分配：每种买入来源（跟单 / 新币高评分 / 早期建仓 / 强叙事 / 模型买入）和每个档位，
按自己最近 7 天的真实期望值自动调仓位——赚钱的加、亏钱的减；再加一个总敞口上限，防止一群 meme 一起崩。

| 最近 7 天（≥ 15 笔） | 仓位 |
|---|---|
| 每笔期望 ≤ -10% | ×0.25 |
| -10% ~ 0 | ×0.5 |
| 0 ~ +8% | ×1 |
| +8% ~ +20%（去掉最赚的一笔仍为正） | ×1.25 |
| ≥ +20%（去掉最赚的一笔仍为正） | ×1.5 |
来源和档位的倍数相乘，最终限制在 ×0.25 ~ ×1.5。样本不够 15 笔按 ×1。
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from ..utils import f

if TYPE_CHECKING:
    from ..app import App

_cache: dict = {"t": 0.0, "dry": None, "v": {}}


def source_of(reason: str | None) -> str:
    if "试探仓" in (reason or ""):
        return "试探仓"
    return re.split(r"[（(|、]", reason or "其他")[0].strip() or "其他"


def mult(rets: list[float], n_min: int = 15) -> float:
    if len(rets) < n_min:
        return 1.0
    e = sum(rets) / len(rets)
    ex_best = (sum(rets) - max(rets)) / (len(rets) - 1)
    if e <= -0.10:
        return 0.25
    if e < 0:
        return 0.5
    if e >= 0.20 and ex_best > 0:
        return 1.5
    if e >= 0.08 and ex_best > 0:
        return 1.25
    return 1.0


async def evidence(app: "App") -> dict:
    dry = int(app.cfg.dry_run)
    if time.time() - _cache["t"] < 600 and _cache["dry"] == dry:
        return _cache["v"]
    rows = await app.db.fetchall(
        "SELECT reason, tier, cost_usd, realized_usd FROM positions WHERE status='closed' AND dry_run=? AND closed_at>? "
        "AND cost_usd>0", (dry, time.time() - 7 * 86400))
    src: dict[str, list[float]] = {}
    tier: dict[str, list[float]] = {}
    for r in rows:
        ret = f(r["realized_usd"]) / f(r["cost_usd"]) - 1
        src.setdefault(source_of(r["reason"]), []).append(ret)
        tier.setdefault(r["tier"] or "?", []).append(ret)
    v = {"src": src, "tier": tier}
    _cache.update(t=time.time(), dry=dry, v=v)
    return v


async def size_mult(app: "App", reason: str, tier: str) -> tuple[float, str]:
    if not bool(app.cfg.get("trading.evidence_sizing", True)):
        return 1.0, ""
    n_min = int(app.cfg.get("trading.evidence_min_trades", 15))
    ev = await evidence(app)
    s = source_of(reason)
    rs, rt = ev["src"].get(s, []), ev["tier"].get(tier, [])
    ms, mt = mult(rs, n_min), mult(rt, n_min)
    m = max(0.25, min(1.5, ms * mt))
    if m == 1.0:
        return 1.0, ""
    parts = []
    if ms != 1:
        parts.append(f"{s}近 7 天 {len(rs)} 笔每笔 {sum(rs) / len(rs) * 100:+.0f}%")
    if mt != 1:
        parts.append(f"该档 {len(rt)} 笔每笔 {sum(rt) / len(rt) * 100:+.0f}%")
    return m, f"按战绩仓位 ×{m:g}（" + "，".join(parts) + "）"


async def exposure_ok(app: "App", add_usd: float) -> tuple[bool, str]:
    key = "trading.max_exposure_usd_dry" if app.cfg.dry_run else "trading.max_exposure_usd"
    cap = float(app.cfg.get(key, 0) or 0)
    if cap <= 0:
        return True, ""
    rows = await app.db.fetchall("SELECT cost_usd, amount_raw, remaining_raw FROM positions WHERE status='open' "
                                 "AND dry_run=?", (int(app.cfg.dry_run),))
    used = 0.0
    for r in rows:
        a = int(r["amount_raw"] or 0)
        used += f(r["cost_usd"]) * (int(r["remaining_raw"] or 0) / a if a > 0 else 1)
    if used + add_usd > cap:
        return False, f"持仓总金额 ${used:,.0f} + 这笔 ${add_usd:,.0f} 超过上限 ${cap:,.0f}"
    return True, ""
