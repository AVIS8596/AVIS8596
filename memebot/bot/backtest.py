"""信号回测：如果过去 N 天每个聪明钱的买入信号都跟了，结果会怎样？

用法：python main.py backtest --days 7 [--limit 300] [--apply]
  --apply：把"跟单信号表现很差"的钱包自动停用
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .utils import short

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.backtest")


def evaluate_curve(points: list[tuple[float, float]], t0: float) -> dict | None:
    """points: [(时间, 价格)]。返回 入场价、24h内最高涨幅、6h收益、24h收益。"""
    pts = sorted(p for p in points if p[1] > 0)
    after = [p for p in pts if p[0] >= t0 - 900]
    if len(after) < 2:
        return None
    entry = after[0][1]
    win24 = [p for p in after if p[0] <= t0 + 86400]
    max_gain = (max(p[1] for p in win24) / entry - 1) * 100
    min_gain = (min(p[1] for p in win24) / entry - 1) * 100

    def at(h: float) -> float:
        cand = [p for p in after if p[0] <= t0 + h * 3600]
        return (cand[-1][1] / entry - 1) * 100 if cand else 0.0

    return {"entry": entry, "max": max_gain, "min": min_gain, "r6h": at(6), "r24h": at(24)}


async def run_backtest(app: "App", days: float = 7, limit: int = 300, apply: bool = False) -> str:
    since = time.time() - days * 86400
    rows = await app.db.fetchall(
        "SELECT chain, wallet, token, MIN(ts) ts FROM signals WHERE side='buy' AND ts>=? AND ts<=? "
        "GROUP BY chain, wallet, token ORDER BY ts DESC LIMIT ?", (since, time.time() - 6 * 3600, limit))
    if not rows:
        return "回测：这段时间没有足够的买入信号（信号需要至少 6 小时前的）"
    stats: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        pts = await app.data.closes(r["chain"], r["token"], int(r["ts"]) - 900, int(r["ts"]) + 86400)
        res = evaluate_curve(pts, r["ts"])
        if res:
            stats.setdefault((r["chain"], r["wallet"]), []).append(res)

    lines = [f"<b>回测结果（近 {days:g} 天，{len(rows)} 条信号）</b>",
             "钱包 | 信号数 | 24h内涨超50%比例 | 平均6h收益 | 平均24h收益"]
    total = [x for v in stats.values() for x in v]
    disabled = 0
    ranking = []
    for (chain, w), lst in stats.items():
        n = len(lst)
        hit = sum(1 for x in lst if x["max"] >= 50) / n
        r6 = sum(x["r6h"] for x in lst) / n
        r24 = sum(x["r24h"] for x in lst) / n
        ranking.append((hit, r6, chain, w, n, r24))
        if apply and n >= 5 and hit < 0.2 and r24 < -30:
            await app.db.execute("UPDATE wallets SET status='inactive' WHERE chain=? AND address=? AND status='active'",
                                 (chain, w))
            disabled += 1
            app.wallets_changed[chain].set()
    ranking.sort(reverse=True)
    for hit, r6, chain, w, n, r24 in ranking[:25]:
        lines.append(f"{chain}:{short(w)} | {n} | {hit * 100:.0f}% | {r6:+.0f}% | {r24:+.0f}%")
    if total:
        hit_all = sum(1 for x in total if x["max"] >= 50) / len(total)
        lines.append(f"\n整体：{len(total)} 条可回测，{hit_all * 100:.0f}% 在24小时内涨超50%，"
                     f"平均24h收益 {sum(x['r24h'] for x in total) / len(total):+.1f}%")
    if apply:
        lines.append(f"已停用表现差的钱包 {disabled} 个")
    return "\n".join(lines)
