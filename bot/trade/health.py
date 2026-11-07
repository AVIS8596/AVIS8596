"""动态持仓管理：定期复查每个持仓"当初买入的理由还在不在"，算出 0~100 的健康分。

健康分从 70 起算，数据变好加分、变差扣分：
  聪明钱      大部分已卖出 -30 / 三分之一以上卖出 -15；买入后又有新聪明钱进场 +8 / +15
  捆绑/埋伏   持仓比买入时少一半以上（在出货）-20 / -15
  dev         持仓比买入时少一半以上 -15；两次复查之间几乎清仓 → 紧急卖出
  流动性      扣除价格涨跌影响后少了 25% 以上 -20；少了一半以上（撤池子）→ 紧急卖出
  持有人      减少 15% 以上 -10；增加 20% / 50% 以上 +5 / +10
  成交量      持有满 1 小时后，1 小时成交量不到买入时的 20% -10；超过 1.5 倍 +5
  新出现安全风险（增发/冻结/转账税等）-100（仍要连续两次复查确认，防止接口抖动误判）

紧急情况不等确认立刻全卖（调高滑点和优先费）；其余情况要连续两次复查都偏低才动手。
"""
from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db import DB

DYN_DEFAULTS: dict = {
    "mode": "shadow",            # off 关闭 / shadow 只记录不执行 / live 接管卖出（TG 发 /dynamic 切换）
    "check_sec": 120,            # 每个持仓多久复查一次
    "confirm_checks": 2,         # 健康分偏低要连续几次复查才动手（紧急情况除外）
    "base_score": 70,
    "reduce_below": 40,          # 健康分低于此值 → 卖一半
    "exit_below": 25,            # 健康分低于此值 → 全卖
    "strong_above": 80,          # 健康分高于此值 → 强势：推迟止盈、放宽移动止损、延长观察期
    "breakeven_after_pct": 50,   # 涨过 +50% 后止损线上移到成本价
    "breakeven_floor_pct": 3,    # 留 3% 覆盖手续费和滑点，卖出时基本不亏
    "rug_liquidity_ratio": 0.5,  # 扣除价格影响后流动性只剩一半以下 → 撤池子，紧急卖出
    "urgent_slippage_bps": 3000,
    "urgent_priority_fee_sol": "0.002",
    "urgent_tip_fee": "0.002",
    "shadow_report_hours": 24,   # 观察满多少小时后推送一次新旧规则对比
}


def dyn_cfg(exit_cfg: dict | None) -> dict:
    return {**DYN_DEFAULTS, **(((exit_cfg or {}).get("dynamic")) or {})}


def snapshot(rep: dict) -> dict:
    """从分析报告里取出复查需要的数据。"""
    return {
        "ts": time.time(), "price": float(rep.get("price") or 0), "liquidity": float(rep.get("liquidity") or 0),
        "holders": int(rep.get("holders") or 0), "volume_1h": float(rep.get("volume_1h") or 0),
        "bundle": float(rep.get("bundle_held_pct") or 0), "insider": float(rep.get("insider_held_pct") or 0),
        "dev": float(rep.get("dev_held_pct") or 0), "risks": list(rep.get("risks") or []),
    }


async def smart_stats(db: "DB", pos: dict) -> dict:
    """这个币上聪明钱的动向：买过的有几个、其中几个最后一笔是卖出、买入后新进场几个。"""
    rows = await db.fetchall(
        "SELECT s.wallet, s.side, s.ts FROM signals s JOIN wallets w ON w.chain=s.chain AND w.address=s.wallet "
        "WHERE s.chain=? AND s.token=? AND s.ts>=? AND w.status IN ('active','manual') ORDER BY s.ts",
        (pos["chain"], pos["token"], float(pos.get("opened_at") or time.time()) - 72 * 3600))
    first_buy: dict[str, float] = {}
    last_side: dict[str, str] = {}
    for r in rows:
        if r["side"] == "buy":
            first_buy.setdefault(r["wallet"], r["ts"])
        last_side[r["wallet"]] = r["side"]
    sources = set(json.loads(pos.get("source_wallets") or "[]"))
    opened = float(pos.get("opened_at") or 0)
    bought = set(first_buy) | sources
    return {
        "smart": len(bought),
        "sold": sum(1 for w in bought if last_side.get(w) == "sell"),
        "sources": len(sources),
        "sources_sold": sum(1 for w in sources if last_side.get(w) == "sell"),
        "new_buyers": sum(1 for w, t in first_buy.items() if t > opened and w not in sources),
    }


def score_health(entry: dict, prev: dict | None, cur: dict, smart: dict, held_h: float,
                 cfg: dict) -> tuple[int, list[str], str]:
    """返回 (健康分, 加减分说明, 紧急原因)。紧急原因非空表示要立刻全卖。"""
    s = float(cfg.get("base_score", 70))
    notes: list[str] = []
    emergency = ""

    def add(v: float, why: str) -> None:
        nonlocal s
        s += v
        notes.append(f"{'+' if v > 0 else ''}{v:.0f} {why}")

    # ---- 聪明钱 ----
    n, sold = smart.get("smart", 0), smart.get("sold", 0)
    if n and sold:
        r = sold / n
        if r >= 0.66:
            add(-30, f"聪明钱 {sold}/{n} 已卖出")
        elif r >= 0.34:
            add(-15, f"聪明钱 {sold}/{n} 已卖出")
    nb = smart.get("new_buyers", 0)
    if nb >= 2:
        add(15, f"买入后又有 {nb} 个聪明钱进场")
    elif nb == 1:
        add(8, "买入后又有 1 个聪明钱进场")

    # ---- 捆绑 / 埋伏 / dev 出货 ----
    for key, name, pen in (("bundle", "捆绑地址", -20), ("insider", "埋伏地址", -15)):
        e0, e1 = float(entry.get(key) or 0), float(cur.get(key) or 0)
        if e0 >= 3 and e1 <= e0 * 0.5:
            add(pen, f"{name}在出货（{e0:.1f}% → {e1:.1f}%）")
    d0, d1 = float(entry.get("dev") or 0), float(cur.get("dev") or 0)
    dp = float((prev or entry).get("dev") or 0)
    if dp >= 2 and d1 < dp * 0.15:
        emergency = f"dev 清仓（{dp:.1f}% → {d1:.1f}%）"
    elif d0 >= 2 and d1 <= d0 * 0.5:
        add(-15, f"dev 在减仓（{d0:.1f}% → {d1:.1f}%）")

    # ---- 流动性（扣除价格涨跌的影响：池子里的币值大约随价格的平方根变化）----
    l0, l1 = float(entry.get("liquidity") or 0), float(cur.get("liquidity") or 0)
    p0, p1 = float(entry.get("price") or 0), float(cur.get("price") or 0)
    if l0 > 0 and l1 > 0 and p0 > 0 and p1 > 0:
        adj = (l1 / l0) / math.sqrt(p1 / p0)
        if adj < float(cfg.get("rug_liquidity_ratio", 0.5)):
            emergency = emergency or f"流动性被撤走（${l0:,.0f} → ${l1:,.0f}）"
        elif adj < 0.75:
            add(-20, f"流动性被抽走（${l0:,.0f} → ${l1:,.0f}）")

    # ---- 持有人 / 成交量 ----
    h0, h1 = int(entry.get("holders") or 0), int(cur.get("holders") or 0)
    if h0 >= 50 and h1 > 0:
        r = h1 / h0
        if r < 0.85:
            add(-10, f"持有人减少（{h0} → {h1}）")
        elif r >= 1.5:
            add(10, f"持有人大增（{h0} → {h1}）")
        elif r >= 1.2:
            add(5, f"持有人增加（{h0} → {h1}）")
    v0, v1 = float(entry.get("volume_1h") or 0), float(cur.get("volume_1h") or 0)
    if v0 > 0 and held_h >= 1:
        if v1 < v0 * 0.2:
            add(-10, f"成交量萎缩（1h ${v0:,.0f} → ${v1:,.0f}）")
        elif v1 > v0 * 1.5:
            add(5, f"成交量放大（1h ${v0:,.0f} → ${v1:,.0f}）")

    # ---- 新出现的安全风险 ----
    new_risks = [r for r in cur.get("risks") or [] if r not in (entry.get("risks") or [])]
    if new_risks:
        add(-100, "新出现风险：" + "；".join(new_risks))

    return int(max(0, min(100, round(s)))), notes, emergency
