"""几套打法放在一起比（/compare）：用这几天的数据，同一个尺子，看哪套最有可能赚钱。

三个来源，都扣过成本：
1. 真下过的单（模拟盘）：按买入来源分，已平仓的实际收益。
2. 影子采样：机器人分析过、通过防收割检查的币，在被分析的那一刻虚拟买入，按现在的卖出规则用 K 线模拟到底。
   这是同一把尺子——同一批币、同样的卖法，只比"选哪些币"：
     - 全部（对照组：不挑，见一个买一个）；
     - 按币是从哪来的（聪明钱信号 / 新币 / 早期建仓 / 强叙事……）；
     - 选币模型当时打了高分的；
     - 落在"学来的打法""钻石手打法"出手范围里的。
3. 被风控拦下的币（影子单）：每类拦截如果没拦、按同样卖法会怎样——平均是正的说明可能拦错了。
最后按"每笔平均（去掉最好的一笔后也要看）+ 中位"排出哪几套最有希望，哪几套可以停。
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from ..utils import f
from .sizing import source_of

if TYPE_CHECKING:
    from ..app import App

MIN_N = 20


def stats(rs: list[float]) -> dict:
    s = sorted(rs)
    n = len(s)
    if not n:
        return {"n": 0}
    avg = sum(s) / n
    return {"n": n, "avg": avg, "med": s[n // 2], "win": sum(1 for x in s if x > 0) / n,
            "ex_best": (sum(s) - s[-1]) / (n - 1) if n > 1 else avg}


def line(name: str, st: dict) -> str:
    if not st.get("n"):
        return f"  {name}：没有数据"
    tail = "" if st["n"] >= MIN_N else "（样本少）"
    return (f"  {name}：{st['n']} 笔，平均 {st['avg'] * 100:+.1f}%（去掉最好一笔 {st['ex_best'] * 100:+.1f}%）、"
            f"中位 {st['med'] * 100:+.0f}%、赚钱 {st['win'] * 100:.0f}%{tail}")


SRC_NAME = {"smart_buy": "聪明钱信号的币", "manual": "手动查的币", "whynot": "手动查的币", "下单前复查": "下单前复查"}


def src_name(s: str) -> str:
    s = s or "其他"
    if s in SRC_NAME:
        return SRC_NAME[s]
    for k in ("早期建仓", "强叙事", "新币", "模型"):
        if s.startswith(k):
            return k + "的币"
    return "新币（" + s[:12] + "）"


def in_range(x: dict, gate: dict) -> bool:
    for k, (lo, hi) in (gate or {}).items():
        v = x.get(k)
        if v is None or not lo <= f(v) <= hi:
            return False
    return True


async def compare(app: "App", days: float = 4) -> str:
    db = app.db
    since = time.time() - days * 86400
    dry = int(bool(app.cfg.dry_run))
    L = [f"⚖️ <b>几套打法对比</b>（最近 {days:g} 天，都扣过成本）"]
    ranking: list[tuple[str, dict]] = []

    # ---- 1. 真下过的单
    by: dict[str, list[float]] = {}
    for p in await db.fetchall("SELECT reason, cost_usd, realized_usd FROM positions WHERE status='closed' AND dry_run=? "
                               "AND opened_at>?", (dry, since)):
        c = f(p["cost_usd"])
        if c > 0:
            by.setdefault(source_of(p["reason"]), []).append(f(p["realized_usd"]) / c - 1)
    L.append("<b>1. 真下过的单</b>（模拟盘，按买入来源）")
    for src, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        st = stats(rs)
        L.append(line(src, st))
        ranking.append((f"真下单·{src}", st))
    if not by:
        L.append("  这几天没有已平仓的单子")

    # ---- 2. 影子采样：同一批币、同样卖法，只比选币
    rows = await db.fetchall("SELECT ts, chain, token, source, features, sim_ret FROM token_outcomes WHERE ts>? "
                             "AND sim_ret BETWEEN -1 AND 199", (since,))
    L.append(f"<b>2. 同一把尺子：影子采样 {len(rows)} 个币</b>（被分析那一刻虚拟买入，按现在的卖出规则模拟到底）")
    if len(rows) >= MIN_N:
        allst = stats([float(r["sim_ret"]) for r in rows])
        L.append(line("全部（对照组：见一个买一个）", allst))
        ranking.append(("影子·全部", allst))
        groups: dict[str, list[float]] = {}
        for r in rows:
            groups.setdefault(src_name(r["source"]), []).append(float(r["sim_ret"]))
        for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:6]:
            st = stats(rs)
            L.append(line(g, st))
            ranking.append((f"影子·{g}", st))
        # 选币模型当时打了高分的
        picks = await db.fetchall("SELECT chain, token, ts FROM model_picks WHERE ts>?", (since,))
        pk = {(p["chain"], p["token"]) for p in picks}
        if pk:
            rs = [float(r["sim_ret"]) for r in rows if (r["chain"], r["token"]) in pk]
            st = stats(rs)
            L.append(line("选币模型打高分的", st))
            ranking.append(("影子·选币模型", st))
        # 落在学来的打法 / 钻石手打法出手范围里的
        from ..diamond import base_x
        for attr, name in (("imitate", "学来的打法出手范围"), ("diamond", "钻石手出手范围")):
            obj = getattr(app, attr, None)
            ms = getattr(obj, "m", None) or {}
            if not ms:
                continue
            hit, miss = [], []
            for r in rows:
                gate = (ms.get(r["chain"]) or {}).get("gate")
                if not gate:
                    continue
                try:
                    x = base_x(json.loads(r["features"] or "{}") or {})
                except (TypeError, ValueError):
                    continue
                g = {k: v for k, v in gate.items() if k in ("age_min", "mcap", "chg60")}
                (hit if in_range(x, g) else miss).append(float(r["sim_ret"]))
            if hit:
                st = stats(hit)
                L.append(line(f"{name}内", st) + f" | 范围外 {stats(miss).get('avg', 0) * 100:+.1f}%")
                ranking.append((f"影子·{name}", st))
    else:
        L.append("  影子采样结果还太少")

    # ---- 3. 被拦下的
    sk = await db.fetchall("SELECT cat, sim_ret FROM skipped WHERE ts>? AND sim_ret BETWEEN -1 AND 199", (since,))
    cats: dict[str, list[float]] = {}
    for r in sk:
        cats.setdefault(r["cat"] or "其他", []).append(float(r["sim_ret"]))
    if cats:
        L.append("<b>3. 被风控拦下的币，如果没拦</b>（同样卖法；平均是正的 = 可能拦错了）")
        for cat, rs in sorted(cats.items(), key=lambda kv: -len(kv[1]))[:8]:
            st = stats(rs)
            mark = " ⚠️" if st["n"] >= MIN_N and st["ex_best"] > 0.05 and st["med"] > -0.1 else ""
            L.append(line(cat, st) + mark)

    # ---- 卖法对比：真实入场按"买卖力量卖法"重放
    from ..flow import replay_report
    L += await replay_report(app, days)
    from ..lanes import lane_report
    L += await lane_report(app, days)

    # ---- 结论
    good = [(n, s) for n, s in ranking if s.get("n", 0) >= MIN_N]
    good.sort(key=lambda x: -(x[1]["ex_best"] + 0.5 * x[1]["med"]))
    if good:
        L.append("<b>排名</b>（按去掉最好一笔后的平均 + 一半中位；样本 ≥ 20 才排）")
        for i, (n, s) in enumerate(good[:8], 1):
            flag = "✅ 有希望" if s["ex_best"] > 0.03 and s["med"] > -0.15 else ("➖ 持平" if s["ex_best"] > -0.03 else "❌ 亏")
            L.append(f"  {i}. {flag} {n}：去掉最好一笔 {s['ex_best'] * 100:+.1f}%、中位 {s['med'] * 100:+.0f}%（{s['n']} 笔）")
    L.append("（学来的打法 / 钻石手打法刚开始跑，还没有自己的单子；它们自己的检验成绩看 /imitate、/diamond）")
    return "\n".join(L)
