"""为什么买的都在亏、拦下的反而在赚（/whyloss）：把差距拆开，看是哪一环出了问题。

对每笔真实买过的单子（模拟盘、已平仓）算三个数：
  A. 实际结果；
  B. 同一笔入场（同样的时间和价格），用影子单那套方法（K 线模拟、同样的卖法）算出来的结果；
  C. 如果在机器人第一次看到这个币时就买（30 秒后第一根完整 K 线的开盘价），同样方法算出来的结果。
A 和 B 差得多 → 影子单的模拟方法本身太乐观（真实卖出更难：撤池子卖不掉、成本更高、K 线看不出的插针），
               拿影子单去比"拦下的赚、买的亏"就不公平；
B 和 C 差得多 → 买晚了：从看到到买入，价格已经涨了一截。
另外看被拦下的币：以前影子单用的是拦截时记下的价格（分析报告可能是几分钟前缓存的），
和真正 30 秒后能买到的价格差多少——差得多，说明以前的影子单把入场价算低了，收益虚高。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from ..utils import f
from .positions import active_rules
from .replay import after, candles, fair_entry, simulate, to_path
from .sampler import dead_tail

if TYPE_CHECKING:
    from ..app import App


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _med(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


async def whyloss(app: "App", days: float = 4, limit: int = 60) -> str:
    db = app.db
    now = time.time()
    exit_cfg = app.cfg.get("exit", {}) or {}
    rules = active_rules(exit_cfg)
    cost = float(exit_cfg.get("shadow_cost_pct", 2)) / 100
    pos = await db.fetchall(
        "SELECT * FROM positions WHERE status='closed' AND dry_run=? AND opened_at>? AND opened_at<? AND entry_price>0 "
        "AND cost_usd>0 ORDER BY opened_at DESC LIMIT ?", (int(bool(app.cfg.dry_run)), now - days * 86400, now - 4 * 3600, limit))
    rows = []
    for p in pos:
        t_open, entry = f(p["opened_at"]), f(p["entry_price"])
        first = await db.fetchone("SELECT ts, price FROM token_outcomes WHERE chain=? AND token=? AND ts BETWEEN ? AND ? "
                                  "ORDER BY ts LIMIT 1", (p["chain"], p["token"], t_open - 86400, t_open))
        a = f(p["realized_usd"]) / f(p["cost_usd"]) - 1
        try:
            cs = await candles(app, p["chain"], p["token"], t_open)
            await asyncio.sleep(0.3)
        except Exception:  # noqa: BLE001
            continue
        path = to_path(after(cs, t_open))
        if len(path) < 8:
            continue
        urgent = str(p["close_reason"] or "").startswith("🚨")
        # 撤池子 / 归零的：K 线在撤池子之后就断了，模拟会拿断之前的高价卖——要在断的地方补一个归零点
        path = dead_tail(path, t_open, now, urgent)
        b = simulate(entry, t_open, path, dict(rules), None, 100, cost) - 1
        c = None
        delay = runup = None
        if first and f(first["price"]) > 0:
            t1 = f(first["ts"])
            delay = (t_open - t1) / 60
            runup = entry / f(first["price"]) - 1
            try:
                cs1 = await candles(app, p["chain"], p["token"], t1) if t_open - t1 > 300 else cs
                await asyncio.sleep(0.3)
                px0, path1 = fair_entry(cs1, t1)
                path1 = dead_tail(path1, t1, now, urgent)
                if px0 > 0 and len(path1) >= 8:
                    c = simulate(px0 * 1.01, t1, path1, dict(rules), None, 100, cost) - 1
            except Exception:  # noqa: BLE001
                pass
        rows.append({"a": a, "b": b, "c": c, "delay": delay, "runup": runup, "urgent": urgent})
    L = [f"🔍 <b>为什么买的亏、拦下的赚</b>（最近 {days:g} 天 {len(rows)} 笔已平仓的真实单子，都扣过成本）"]
    if len(rows) < 5:
        return L[0] + "\n单子太少，过两天再看"
    A = [r["a"] for r in rows]
    B = [r["b"] for r in rows]
    L.append(f"A. 实际结果：每笔平均 {_avg(A) * 100:+.1f}%、中位 {_med(A) * 100:+.0f}%")
    L.append(f"B. 同一笔入场按影子单的方法模拟（卖法用现在的高手规则）：平均 {_avg(B) * 100:+.1f}%、中位 {_med(B) * 100:+.0f}%"
             f"（差 {(_avg(B) - _avg(A)) * 100:+.1f} 个点 = 卖法不同 + 模拟比真实乐观）")
    urg = [r for r in rows if r["urgent"]]
    if urg:
        L.append(f"   其中 {len(urg)} 笔是紧急卖出（撤池子 / 归零）：实际平均 {_avg([r['a'] for r in urg]) * 100:+.0f}%，"
                 f"模拟成 {_avg([r['b'] for r in urg]) * 100:+.0f}%")
    cs_ = [r for r in rows if r["c"] is not None]
    if cs_:
        L.append(f"C. 如果第一次看到就买：平均 {_avg([r['c'] for r in cs_]) * 100:+.1f}%、中位 {_med([r['c'] for r in cs_]) * 100:+.0f}%"
                 f"（同一批 {len(cs_)} 笔按 B 的方法是 {_avg([r['b'] for r in cs_]) * 100:+.1f}%，差 = 买晚了吃的亏）")
    dl = [r["delay"] for r in rows if r["delay"] is not None]
    ru = [r["runup"] for r in rows if r["runup"] is not None]
    if dl:
        L.append(f"从第一次看到到买入：中位 {_med(dl):.0f} 分钟，这段时间价格中位 {_med(ru) * 100:+.0f}%，"
                 f"{sum(1 for x in ru if x >= 0.5)}/{len(ru)} 笔已经涨了 50% 以上才买")
        hi = [r["a"] for r in rows if r["runup"] is not None and r["runup"] >= 0.5]
        lo = [r["a"] for r in rows if r["runup"] is not None and r["runup"] < 0.5]
        if hi and lo:
            L.append(f"   已涨 50% 以上才买的 {len(hi)} 笔实际平均 {_avg(hi) * 100:+.0f}% | 没怎么涨就买的 {len(lo)} 笔 {_avg(lo) * 100:+.0f}%")
    # 被拦下的币：以前记的价格 vs 30 秒后真正能买到的价格
    sk = await db.fetchall("SELECT s.ts, s.chain, s.token, s.price, k.data FROM skipped s JOIN kline_cache k "
                           "ON k.chain=s.chain AND k.token=s.token AND k.t0=CAST(s.ts AS INTEGER) "
                           "WHERE s.ts>? AND s.price>0 LIMIT 400", (now - days * 86400,))
    ratios = []
    for r in sk:
        try:
            px0, _ = fair_entry(json.loads(r["data"]), f(r["ts"]))
        except (TypeError, ValueError):
            continue
        if px0 > 0 and 0.1 < px0 / f(r["price"]) < 10:
            ratios.append(px0 / f(r["price"]) - 1)
    if len(ratios) >= 10:
        L.append(f"被拦下的 {len(ratios)} 个币：30 秒后真正能买到的价格比当时记下的价格中位 {_med(ratios) * 100:+.0f}%、"
                 f"平均 {_avg(ratios) * 100:+.0f}%，{sum(1 for x in ratios if x >= 0.2)} 个高 20% 以上"
                 "（以前影子单按记下的价格算，等于把入场价算低了；11-12 起改成按 30 秒后的价格算）")
    L.append("怎么看：B 比 A 好很多 → 影子单太乐观，拦下的'赚'有水分；C 比 B 好很多 → 主要是买晚了；"
             "都差不多 → 真是挑币挑反了（拦下的那几类确实更好）")
    return "\n".join(L)
