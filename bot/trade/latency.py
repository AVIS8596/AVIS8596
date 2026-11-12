"""延迟和实盘成本实测（/lag）：模拟盘的成绩，实盘能拿到几成？

1. 延迟：从带单的聪明钱买入（链上时间）到我们买入，过了多少秒、价格涨了多少（用 1 分钟 K 线取聪明钱买入那一刻的价格）。
   这段涨幅模拟盘已经算在成绩里了（模拟盘就是按我们买入那一刻的价格成交），这里是让你看清楚"晚了多少、让出去多少"，
   以及"晚到的单子是不是更亏"。
2. 实盘成本：模拟盘每笔按模拟金额扣固定费用（优先费 / Gas），金额大，固定费用占比小。
   实盘按计划的小金额（默认 Solana 0.05 SOL）下单，同样的优先费 + 小费占比高得多。
   把每笔模拟单的收益按实盘金额重新扣一次固定费用，看还剩多少。
   没算进去的（只会更差）：实盘的真实滑点比公式估算大、交易失败、卖不出去。这些要实盘跑起来用 /costs 实测。
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from ..utils import f
from .costs import median, ref_price
from .sizing import source_of

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.latency")

# 实盘计划每笔金额（原生币）和每笔额外的小费（GMGN 默认小费不公开，按常见值估）；都可在配置 golive.* 里改
PLAN_DEFAULT = {"solana": 0.05, "bsc": 0.02}
TIP_DEFAULT = {"solana": 0.001, "bsc": 0.0}
MAX_LOOKUPS = 60          # 一次最多查 60 笔的 K 线（结果缓存，下次不再查）


def _ret(p: dict) -> float:
    c = f(p.get("cost_usd"))
    return (f(p.get("realized_usd")) - c) / c if c > 0 else 0.0


async def measure(app: "App", days: int = 7) -> list[dict]:
    """每笔模拟单：来源、带单钱包买入时间、延迟秒数、这段时间的涨幅、收益（已平仓的）。"""
    db = app.db
    dry = int(bool(app.cfg.dry_run))
    since = time.time() - days * 86400
    cache = await db.kv_get("lag_cache", {}) or {}
    rows = await db.fetchall(
        "SELECT id, chain, token, symbol, reason, source_wallets, opened_at, status, cost_usd, cost_native, "
        "realized_usd FROM positions WHERE dry_run=? AND opened_at>? ORDER BY opened_at DESC", (dry, since))
    out: list[dict] = []
    looked = 0
    for p in rows:
        p = dict(p)
        rec = {"id": p["id"], "chain": p["chain"], "symbol": p["symbol"], "src": source_of(p["reason"]),
               "closed": p["status"] == "closed", "ret": _ret(p) if p["status"] == "closed" else None,
               "cost_native": f(p["cost_native"]), "cost_usd": f(p["cost_usd"])}
        key = str(p["id"])
        if key in cache:
            rec.update(cache[key])
            out.append(rec)
            continue
        t_open = f(p["opened_at"])
        wallets = json.loads(p["source_wallets"] or "[]")
        q = ("SELECT MIN(ts) t FROM signals WHERE chain=? AND token=? AND side='buy' AND ts BETWEEN ? AND ?")
        args: list = [p["chain"], p["token"], t_open - 7200, t_open + 5]
        sig = None
        if wallets:
            sig = await db.fetchone(q + f" AND wallet IN ({','.join('?' * len(wallets))})", tuple(args + wallets))
        if not sig or not sig["t"]:
            sig = await db.fetchone(q, tuple(args))            # 没记带单钱包：用最早买它的聪明钱
        t_sig = f(sig["t"]) if sig else 0.0
        buy = await db.fetchone("SELECT quote_price, price_usd FROM trades WHERE chain=? AND token=? AND side='buy' "
                                "AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1", (p["chain"], p["token"], t_open - 120, t_open + 120))
        q_entry = f(buy["quote_price"]) if buy and f(buy["quote_price"]) > 0 else (f(buy["price_usd"]) if buy else 0.0)
        info = {"lag": t_open - t_sig if t_sig else None, "move": None}
        if t_sig and q_entry > 0 and looked < MAX_LOOKUPS:
            looked += 1
            p_sig = await ref_price(app, p["chain"], p["token"], t_sig)
            if p_sig > 0 and 0.05 < q_entry / p_sig < 20:     # 排除价格单位不对的数据
                info["move"] = q_entry / p_sig - 1
        if info["lag"] is None or info["move"] is not None or looked >= MAX_LOOKUPS:
            if rec["closed"]:
                cache[key] = info                               # 已平仓的结果不会再变，缓存
        rec.update(info)
        out.append(rec)
    if len(cache) > 3000:
        cache = dict(list(cache.items())[-2000:])
    await db.kv_set("lag_cache", cache)
    return out


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


async def lag_text(app: "App", days: int = 7) -> str:
    recs = await measure(app, days)
    if not recs:
        return f"最近 {days} 天没有模拟单"
    lines = [f"⏱ <b>延迟和实盘成本实测</b>（最近 {days} 天 {len(recs)} 笔模拟单）"]
    # ---- 1. 延迟
    lines.append("<b>1. 比带单的聪明钱晚多少</b>（聪明钱链上买入 → 我们买入）")
    by: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("lag") is not None:
            by.setdefault(r["src"], []).append(r)
    if not by:
        lines.append("  没有带聪明钱信号的单子")
    for src, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        lags = [r["lag"] for r in rs]
        mv = [r["move"] for r in rs if r.get("move") is not None]
        s = f"  {src}：{len(rs)} 笔，中位晚 {median(lags):.0f} 秒"
        if mv:
            s += (f"，这段时间价格中位 {_pct(median(mv))}，"
                  f"{sum(1 for x in mv if x >= 0.2)}/{len(mv)} 笔已经涨了 20% 以上才买到")
        lines.append(s)
    done = [r for r in recs if r["closed"] and r.get("move") is not None]
    late = [r["ret"] for r in done if r["move"] >= 0.2]
    early = [r["ret"] for r in done if r["move"] < 0.2]
    if late and early:
        lines.append(f"  晚到（已涨 ≥20% 才买到）{len(late)} 笔：平均 {_pct(sum(late) / len(late))}、中位 {_pct(median(late))}"
                     f" | 不晚 {len(early)} 笔：平均 {_pct(sum(early) / len(early))}、中位 {_pct(median(early))}")
        lines.append("  （晚到的明显更亏 → 应该加一条\"聪明钱买入后已经涨太多就不跟\"；差不多 → 延迟不是主要问题）")
    # ---- 2. 实盘成本
    lines.append("<b>2. 换成实盘的小金额，固定费用吃掉多少</b>")
    plan = {**PLAN_DEFAULT, **(app.cfg.get("golive.plan_buy_native", {}) or {})}
    tip = {**TIP_DEFAULT, **(app.cfg.get("golive.tip_native", {}) or {})}
    for chain in sorted({r["chain"] for r in recs}):
        rs = [r for r in recs if r["chain"] == chain and r["closed"] and r["cost_native"] > 0]
        if not rs:
            continue
        fixed_sim = float(app.cfg.per_chain("trading.sim_fixed_fee_native", chain, 0) or 0)
        fixed_live = fixed_sim + float(tip.get(chain, 0) or 0)
        size = float(plan.get(chain) or 0)
        if size <= 0:
            continue
        sim_size = median([r["cost_native"] for r in rs])
        extra = 2 * (fixed_live / size - fixed_sim / sim_size)       # 一买一卖，多扣的固定费用比例
        paper = [r["ret"] for r in rs]
        live = [x - extra for x in paper]
        npx = await app.native_price(chain)
        unit = "SOL" if chain == "solana" else "BNB" if chain == "bsc" else "原生币"
        lines.append(f"  {chain.upper()}：模拟单中位每笔 {sim_size:.3g} {unit}，实盘计划每笔 {size:g} {unit}；"
                     f"每笔固定费用约 {fixed_live:g} {unit}（优先费 {fixed_sim:g} + 小费 {fixed_live - fixed_sim:g}），"
                     f"一买一卖占实盘金额 {2 * fixed_live / size * 100:.1f}%（模拟单只占 {2 * fixed_sim / sim_size * 100:.1f}%）")
        lines.append(f"    {len(rs)} 笔已平仓：模拟成绩每笔平均 {_pct(sum(paper) / len(paper))}、中位 {_pct(median(paper))}"
                     f" → 按实盘金额扣完平均 {_pct(sum(live) / len(live))}、中位 {_pct(median(live))}")
        if npx > 0:
            lines.append(f"    按实盘每笔 {size:g} {unit}（约 ${size * npx:.0f}）算，这 {len(rs)} 笔合计约 "
                         f"${sum(live) * size * npx:+,.2f}")
    lines.append("没算进去的（只会更差）：实盘真实滑点比估算大、交易失败、卖不出去（貔貅 / 撤池子后卖单失败）。"
                 "上实盘后用 /costs 看实测。")
    return "\n".join(lines)
