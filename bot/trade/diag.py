"""自检（/diag）：把"为什么这几天不赚 / 买得少"拆开看，每一条都从数据库里算，不靠猜。

1. 按链：每天在哪条链买了几笔、赚亏多少（Solana 断了以后只剩 EVM 链在交易？EVM 链手续费和 meme 行情都不一样）
2. 买入失败：模拟盘下单失败的次数和原因（节点额度用完时 Solana 模拟买入会失败）
3. 按开仓日期的真实成绩（复盘按平仓日期算，跨天的单子会让某一天看起来特别好 / 特别差）
4. 去掉最赚的 3 笔还剩多少（成绩是不是靠一两笔运气）
5. 止损：止损的笔数、平均亏多少、止损后又涨回成本以上的有几笔
6. 自我学习改过哪些参数（参数天天在变，前后几天其实不是同一个机器人）
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import TYPE_CHECKING

from ..chains import CHAINS
from ..utils import fmt_usd

if TYPE_CHECKING:
    from ..app import App


def _ret(p: dict) -> float:
    c = float(p["cost_usd"] or 0)
    return float(p["realized_usd"] or 0) / c - 1 if c > 0 else 0.0


def _name(c: str) -> str:
    return CHAINS[c].name if c in CHAINS else c


async def diag(app: "App", days: int = 4) -> dict:
    db = app.db
    dry = int(app.cfg.dry_run)
    day0 = app.day_start()
    start = day0 - (days - 1) * 86400
    pos = await db.fetchall("SELECT id, chain, symbol, reason, status, cost_usd, realized_usd, opened_at, closed_at, "
                            "close_reason, post_max FROM positions WHERE dry_run=? AND opened_at>=? ORDER BY opened_at",
                            (dry, start))
    out: dict = {"days": [], "start": start}
    for i in range(days):
        a, b = start + i * 86400, start + (i + 1) * 86400
        ps = [p for p in pos if a <= p["opened_at"] < b]
        closed = [p for p in ps if p["status"] == "closed"]
        by_chain: dict[str, list] = defaultdict(list)
        for p in closed:
            by_chain[p["chain"]].append(p)
        opened = Counter(p["chain"] for p in ps)
        fails = await db.fetchall("SELECT chain, error FROM trades WHERE side='buy' AND ok=0 AND dry_run=? AND ts>=? AND ts<?",
                                  (dry, a, b))
        ferr: dict[str, Counter] = defaultdict(Counter)
        for f in fails:
            ferr[f["chain"]][(f["error"] or "未知")[:60]] += 1
        out["days"].append({
            "start": a, "opened": opened, "open_now": sum(1 for p in ps if p["status"] != "closed"),
            "chains": {c: (len(v), sum(float(p["realized_usd"] or 0) - float(p["cost_usd"] or 0) for p in v),
                           sum(_ret(p) for p in v) / len(v)) for c, v in by_chain.items()},
            "fails": ferr})
    closed = [p for p in pos if p["status"] == "closed"]
    pnl = sorted((float(p["realized_usd"] or 0) - float(p["cost_usd"] or 0) for p in closed), reverse=True)
    out["n"], out["pnl"] = len(closed), sum(pnl)
    out["top3"] = sum(pnl[:3])
    out["wins"] = sum(1 for x in pnl if x > 0)
    stops = [p for p in closed if "止损" in (p["close_reason"] or "") and float(p["cost_usd"] or 0) > 0]
    out["stops"] = len(stops)
    out["stop_avg"] = sum(_ret(p) for p in stops) / len(stops) if stops else 0.0
    # post_max = 平仓后 4 小时最高价 / 平均卖出价；卖出价 ≈ 成本 × (1 + 收益)，涨回成本 = post_max × (1 + 收益) ≥ 1
    # （写成乘法：归零的单子收益 -100%，用除法会除以 0）
    out["stop_back"] = sum(1 for p in stops if p["post_max"] and float(p["post_max"]) * (1 + _ret(p)) >= 1)
    out["stop_known"] = sum(1 for p in stops if p["post_max"])
    out["tune"] = [h for h in (await db.kv_get("tune_log", []) or []) if h.get("ts", 0) >= start]
    return out


async def diag_text(app: "App", days: int = 4) -> str:
    d = await diag(app, days)
    L = [f"🩺 <b>自检</b>（最近 {days} 天，按<b>开仓日期</b>算；🧪模拟盘）"]
    for x in d["days"]:
        day = datetime.fromtimestamp(x["start"], app.tz).strftime("%m-%d")
        n = sum(x["opened"].values())
        parts = []
        for c, k in x["opened"].most_common():
            got = x["chains"].get(c)
            parts.append(f"{_name(c)} {k} 笔" + (f"（已平 {got[0]}：{fmt_usd(got[1])}，每笔 {got[2] * 100:+.0f}%）" if got else ""))
        L.append(f"{day}：买入 {n} 笔" + ("：" + "；".join(parts) if parts else "") +
                 (f"；还有 {x['open_now']} 笔持仓中" if x["open_now"] else ""))
        for c, errs in x["fails"].items():
            top = "、".join(f"{e}×{k}" for e, k in errs.most_common(2))
            L.append(f"  ⚠️ {_name(c)} 模拟买入失败 {sum(errs.values())} 次：{top}")
    if d["n"]:
        L.append(f"合计已平仓 {d['n']} 笔，赚 {d['wins']} 笔，{fmt_usd(d['pnl'])}；"
                 f"最赚的 3 笔 {fmt_usd(d['top3'])}，去掉它们 {fmt_usd(d['pnl'] - d['top3'])}")
        if d["stops"]:
            back = (f"，其中 {d['stop_back']}/{d['stop_known']} 笔止损后 4 小时内又涨回成本以上"
                    if d["stop_known"] else "")
            L.append(f"止损 {d['stops']} 笔（占 {d['stops'] / d['n'] * 100:.0f}%），平均每笔 {d['stop_avg'] * 100:+.0f}%{back}")
    if d["tune"]:
        L.append(f"自我学习这几天改了 {len(d['tune'])} 次参数：" + "、".join(
            f"{h['key'].split('.')[-1]} {h.get('old')}→{h.get('new')}" for h in d["tune"][-6:]))
    return "\n".join(L)
