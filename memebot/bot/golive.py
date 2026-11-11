"""上实盘的标准：机器人自己判断、自己报告。全部达标才建议上实盘，并给出分阶段加钱计划；
上了实盘之后持续对比"实盘 vs 模拟盘"，对不上就提醒退回模拟盘。

标准（只看规则改版后的模拟盘单子，成本按实盘实测校准）：
1. 至少跑满 7 天
2. 至少 150 笔已平仓
3. 每笔期望为正
4. 利润因子 ≥ 1.3（总盈利 ÷ 总亏损）
5. 去掉最赚的一笔后合计依然赚钱
6. 一半以上的天数赚钱，且最差的一天亏损不超过总盈利
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from .utils import f, fmt_usd

if TYPE_CHECKING:
    from .app import App

RULES_SINCE = 1794126900.0      # 2026-11-08 15:35（UTC+7）卖出规则改版，之前的单子不算


def extra_cost(app: "App") -> float:
    """模拟盘公式成本和实盘实测之间的差（一买一卖合计），用于校准成本校准上线之前的老单子。"""
    from .trade.costs import measured
    c = getattr(app, "live_costs", None)
    b, s = measured(c, "all", "buy"), measured(c, "all", "sell")
    model = 0.02           # 公式成本里手续费 + 小额冲击的典型值（单边）
    return max(0.0, (b or model) - model) + max(0.0, (s or model) - model)


async def evaluate(app: "App") -> dict:
    db = app.db
    since = float(await db.kv_get("golive_since", RULES_SINCE) or RULES_SINCE)
    cal_at = float(await db.kv_get("cost_applied_at", 0) or 0)
    rows = await db.fetchall("SELECT opened_at, closed_at, cost_usd, realized_usd FROM positions WHERE status='closed' "
                             "AND dry_run=1 AND opened_at>=? AND cost_usd>0 ORDER BY closed_at", (since,))
    pen = extra_cost(app)
    rets, usds, days = [], [], {}
    for r in rows:
        ret = f(r["realized_usd"]) / f(r["cost_usd"]) - 1
        if not cal_at or f(r["opened_at"]) < cal_at:
            ret -= pen                     # 成本校准生效之前的单子补扣差价
        usd = ret * f(r["cost_usd"])
        rets.append(ret)
        usds.append(usd)
        d = datetime.fromtimestamp(f(r["closed_at"]), app.tz).strftime("%m-%d")
        days[d] = days.get(d, 0.0) + usd
    n = len(rets)
    running = (time.time() - since) / 86400
    win = sum(u for u in usds if u > 0)
    loss = -sum(u for u in usds if u < 0)
    total = sum(usds)
    exp = sum(rets) / n if n else 0.0
    pf = win / loss if loss > 0 else (float("inf") if win > 0 else 0.0)
    ex_best = total - max(usds) if usds else 0.0
    pos_days = sum(1 for v in days.values() if v > 0)
    worst = min(days.values()) if days else 0.0
    checks = [
        (running >= 7, f"跑满 7 天：已跑 {running:.1f} 天"),
        (n >= 150, f"至少 150 笔：已平仓 {n} 笔"),
        (n > 0 and exp > 0, f"每笔期望为正：{exp * 100:+.1f}%"),
        (pf >= 1.3, f"利润因子 ≥ 1.3：{pf:.2f}" if pf != float("inf") else "利润因子 ≥ 1.3：没有亏损单"),
        (n > 0 and ex_best > 0, f"去掉最赚的一笔仍赚钱：{fmt_usd(ex_best)}"),
        (bool(days) and pos_days * 2 >= len(days) and (total <= 0 or -worst <= total),
         f"赚钱的天数过半且最差一天不超过总盈利：{pos_days}/{len(days)} 天赚钱，最差一天 {fmt_usd(worst)}"),
    ]
    return {"ok": all(c[0] for c in checks), "checks": checks, "n": n, "total": total, "exp": exp, "pen": pen,
            "since": since}


async def text(app: "App") -> str:
    r = await evaluate(app)
    head = "✅ <b>已达到上实盘标准</b>" if r["ok"] else "⏳ <b>上实盘标准</b>（还没全部达标，继续跑模拟盘）"
    lines = [head, f"统计范围：{datetime.fromtimestamp(r['since'], app.tz):%m-%d %H:%M} 规则改版后的模拟盘单子"
             + (f"，老单子已按实盘实测补扣 {r['pen'] * 100:.1f}% 成本" if r["pen"] > 0 else "")]
    lines += [("✅ " if ok else "❌ ") + t for ok, t in r["checks"]]
    if r["ok"]:
        lines += ["", "<b>建议的加钱计划</b>",
                  "1. 第一周：实盘仓位用模拟盘的 1/10（/size 设置），GMGN 钱包只放够 20 笔的钱",
                  "2. 实盘满 30 笔、每笔期望和模拟盘差距在 15 个百分点以内 → 仓位翻倍；每周最多翻一次",
                  "3. 实盘每笔期望转负，或比模拟盘差 15 个百分点以上 → 退回模拟盘查原因",
                  "4. 赚到的钱定期转出，钱包里只留交易需要的（/costs 看真实成本）"]
    lines.append(await live_vs_paper(app) or "")
    return "\n".join(x for x in lines if x is not None)


async def live_vs_paper(app: "App") -> str:
    """实盘期间：对比实盘和模拟盘的每笔期望，差太多就提醒。"""
    db = app.db
    start = float(await db.kv_get("live_since", 0) or 0)
    if not start or app.cfg.dry_run:
        return ""
    live = await db.fetchall("SELECT cost_usd, realized_usd FROM positions WHERE status='closed' AND dry_run=0 "
                             "AND opened_at>=? AND cost_usd>0", (start,))
    paper = await db.fetchall("SELECT cost_usd, realized_usd FROM positions WHERE status='closed' AND dry_run=1 "
                              "AND opened_at>=? AND cost_usd>0", (start - 14 * 86400,))
    if len(live) < 10 or len(paper) < 30:
        return f"实盘已 {len(live)} 笔，满 10 笔后开始和模拟盘对比"
    el = sum(f(r["realized_usd"]) / f(r["cost_usd"]) - 1 for r in live) / len(live)
    ep = sum(f(r["realized_usd"]) / f(r["cost_usd"]) - 1 for r in paper) / len(paper)
    warn = len(live) >= 30 and (el < 0 or el < ep - 0.15)
    return (f"{'⚠️' if warn else '📊'} 实盘 {len(live)} 笔每笔 {el * 100:+.1f}% vs 模拟盘 {ep * 100:+.1f}%"
            + ("——实盘明显比模拟盘差，建议退回模拟盘查原因（/costs 看真实成本）" if warn else ""))


async def daily_line(app: "App") -> str:
    r = await evaluate(app)
    done = sum(1 for ok, _ in r["checks"] if ok)
    first = next((t for ok, t in r["checks"] if not ok), "")
    if r["ok"]:
        if not await app.db.kv_get("golive_notified", False):
            await app.db.kv_set("golive_notified", True)
            return "🎉 <b>模拟盘已达到上实盘标准！</b>发 /golive 看详情和加钱计划"
        return "✅ 上实盘标准：全部达标（/golive）"
    return f"⏳ 上实盘标准：{done}/{len(r['checks'])} 项达标，下一项：{first}（/golive）"
