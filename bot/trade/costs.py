"""真实成交成本校准：用实盘成交算出"实际比报价多花了多少"，替换模拟盘的估算成本。

- 卖出：成交价 vs 下单那一刻机器人看到的价格（trades.price_usd 记的就是它）
- 买入：成交价 vs 下单时的报价（新记录有 quote_price；老记录用成交那一分钟的 1 分钟 K 线中间价近似）
- 失败率：实盘下单失败的比例（失败的买入 = 错过；失败的卖出 = 卖晚了）
模拟盘的单边成本 = max(公式估算, 实盘中位成本)；另外每笔扣固定的优先费 / Gas（按链配置）。
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..utils import f

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.costs")

MIN_N = 8          # 至少 8 笔实盘成交才用实测成本


async def ref_price(app: "App", chain: str, token: str, ts: float) -> float:
    """成交那一分钟的 1 分钟 K 线中间价（取不到返回 0）。"""
    if not getattr(app, "gmgn", None):
        return 0.0
    try:
        rows = await app.gmgn.kline(chain, token, "1m", int(ts) - 180, int(ts) + 120)
    except Exception:  # noqa: BLE001
        return 0.0
    best = None
    for c in rows or []:
        t = f(c.get("time"))
        t = t / 1000 if t > 1e12 else t
        if t <= ts and (best is None or t > best[0]):
            best = (t, c)
    if not best or ts - best[0] > 120:
        return 0.0
    o, cl = f(best[1].get("open")), f(best[1].get("close"))
    return (o + cl) / 2 if o > 0 and cl > 0 else cl


def median(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


async def calibrate(app: "App", limit: int = 200) -> dict:
    """把还没算过的实盘成交算出成本，汇总后存到 kv live_costs。"""
    db = app.db
    rows = await db.fetchall(
        "SELECT t.id, t.ts, t.chain, t.token, t.side, t.token_raw, t.price_usd, t.usd_value, t.quote_price, "
        "p.decimals FROM trades t LEFT JOIN positions p ON p.id=t.position_id "
        "WHERE t.dry_run=0 AND t.ok=1 AND t.usd_value>0 AND t.id NOT IN (SELECT trade_id FROM trade_costs) "
        "ORDER BY t.id DESC LIMIT ?", (limit,))
    for r in rows:
        cost = None
        try:
            if r["side"] == "buy":
                fill = f(r["price_usd"])
                ref = f(r["quote_price"]) or await ref_price(app, r["chain"], r["token"], f(r["ts"]))
                if fill > 0 and ref > 0:
                    cost = fill / ref - 1
            else:
                dec = r["decimals"]
                ui = int(r["token_raw"] or 0) / 10 ** int(dec) if dec is not None else 0
                fill = f(r["usd_value"]) / ui if ui > 0 else 0
                ref = f(r["quote_price"]) or f(r["price_usd"])
                if fill > 0 and ref > 0:
                    cost = 1 - fill / ref
        except Exception as e:  # noqa: BLE001
            log.debug("成本计算失败 %s: %s", r["id"], e)
        if cost is not None:
            cost = max(-0.5, min(0.9, cost))      # K 线近似有噪声，截掉离谱值
        await db.execute("INSERT OR REPLACE INTO trade_costs(trade_id,ts,chain,side,cost,usd) VALUES(?,?,?,?,?,?)",
                         (r["id"], r["ts"], r["chain"], r["side"], cost, r["usd_value"]))
    return await summarize(app)


async def summarize(app: "App", days: float = 30) -> dict:
    db = app.db
    since = time.time() - days * 86400
    out: dict[str, dict] = {}
    rows = await db.fetchall("SELECT chain, side, cost FROM trade_costs WHERE ts>? AND cost IS NOT NULL", (since,))
    fails = await db.fetchall("SELECT chain, side, ok FROM trades WHERE dry_run=0 AND ts>?", (since,))
    for scope in ["all"] + sorted({r["chain"] for r in rows}):
        sel = [r for r in rows if scope == "all" or r["chain"] == scope]
        fs = [r for r in fails if scope == "all" or r["chain"] == scope]
        d = {}
        for side in ("buy", "sell"):
            cs = [f(r["cost"]) for r in sel if r["side"] == side]
            tot = [r for r in fs if r["side"] == side]
            d[side] = {"n": len(cs), "med": median(cs), "avg": sum(cs) / len(cs) if cs else 0.0,
                       "fail": sum(1 for r in tot if not r["ok"]) / len(tot) if tot else 0.0, "tries": len(tot)}
        out[scope] = d
    out["_at"] = time.time()
    await db.kv_set("live_costs", out)
    return out


def measured(costs: dict | None, chain: str, side: str) -> float | None:
    """这条链（样本不够就用全部链）实测的单边中位成本；样本不够返回 None。"""
    if not costs:
        return None
    for scope in (chain, "all"):
        d = (costs.get(scope) or {}).get(side) or {}
        if d.get("n", 0) >= MIN_N:
            return max(0.0, float(d["med"]))
    return None


def text(costs: dict | None) -> str:
    if not costs or not (costs.get("all") or {}).get("buy", {}).get("tries"):
        return "还没有实盘成交记录，模拟盘暂时用公式估算成本（手续费 1% + 价格冲击）"
    a = costs["all"]
    lines = ["<b>💸 实盘真实成本</b>（最近 30 天实盘成交）"]
    for scope, d in costs.items():
        if scope.startswith("_"):
            continue
        name = "全部" if scope == "all" else scope
        b, s = d["buy"], d["sell"]
        lines.append(f"  {name}：买入多花 中位 {b['med'] * 100:+.1f}%（{b['n']} 笔），卖出少拿 中位 {s['med'] * 100:+.1f}%"
                     f"（{s['n']} 笔），失败率 买 {b['fail'] * 100:.0f}% / 卖 {s['fail'] * 100:.0f}%")
    rt = max(0.0, a["buy"]["med"]) + max(0.0, a["sell"]["med"])
    use = "已用于模拟盘" if a["buy"]["n"] >= MIN_N or a["sell"]["n"] >= MIN_N else f"样本不到 {MIN_N} 笔，暂未替换"
    lines.append(f"  一买一卖合计约 {rt * 100:.1f}%（{use}；模拟盘每边取 公式估算 和 实测 里更高的那个）")
    return "\n".join(lines)
