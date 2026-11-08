"""复盘：每日总结（按档位、按卖出原因）、新旧规则对比、单个持仓诊断（/why）。"""
from __future__ import annotations

import json
import re
from collections import Counter
import time
from typing import TYPE_CHECKING

from ..utils import fmt_usd
from .positions import URGENT, dyn_state, exit_rules

if TYPE_CHECKING:
    from ..app import App

TIER_LABEL = {"best": "🔥优质", "good": "👍较好", "normal": "一般", None: "老持仓"}
# (平仓原因开头, 分类名)；按顺序匹配
REASON_CATS = [
    (URGENT, "紧急卖出"), ("止损", "止损"), ("保本止损", "保本止损"), ("止盈", "止盈"), ("移动止损", "移动止损"),
    ("涨不上去", "涨不上去"), ("持仓超时", "超时"), ("健康分", "健康分卖出"), ("带单聪明钱", "跟卖聪明钱"),
    ("手动", "手动卖出"), ("对账", "对账关闭"),
]


def reason_cat(r: str | None) -> str:
    r = r or ""
    for pre, name in REASON_CATS:
        if r.startswith(pre):
            return name
    return "其他"


def _pnl(p: dict) -> float:
    return float(p["realized_usd"] or 0) - float(p["cost_usd"] or 0)


def _ret(p: dict) -> float:
    c = float(p["cost_usd"] or 0)
    return _pnl(p) / c if c > 0 else 0.0


def expectancy(rows: list[dict]) -> dict:
    """每笔期望（按成本的百分比）、平均盈利/亏损、盈亏比、利润因子。"""
    rs = [_ret(p) for p in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    pw, pl = sum(_pnl(p) for p in rows if _pnl(p) > 0), -sum(_pnl(p) for p in rows if _pnl(p) <= 0)
    return {"n": len(rs), "exp": sum(rs) / len(rs) if rs else 0.0, "win": len(wins) / len(rs) if rs else 0.0,
            "aw": aw, "al": al, "payoff": aw / -al if al < 0 else 0.0, "pf": pw / pl if pl > 0 else 0.0}


def _group(rows: list[dict], key) -> list[str]:
    g: dict[str, list[dict]] = {}
    for p in rows:
        g.setdefault(key(p), []).append(p)
    out = []
    for k, lst in sorted(g.items(), key=lambda kv: -sum(_pnl(p) for p in kv[1])):
        ex = expectancy(lst)
        out.append(f"  {k}：{len(lst)} 笔，胜率 {ex['win'] * 100:.0f}%，{fmt_usd(sum(_pnl(p) for p in lst))}，"
                   f"每笔期望 {ex['exp'] * 100:+.1f}%")
    return out


def payoff_lines(closed: list[dict]) -> list[str]:
    """期望值 + 大赢家 + 卖飞：判断规则是不是在砍掉大赢家。"""
    ex = expectancy(closed)
    lines = ["<b>期望值</b>（meme 币看这个，不看胜率）",
             f"每笔期望 <b>{ex['exp'] * 100:+.1f}%</b> = 胜率 {ex['win'] * 100:.0f}% × 平均赚 {ex['aw'] * 100:+.0f}%"
             f" + 败率 {(1 - ex['win']) * 100:.0f}% × 平均亏 {ex['al'] * 100:+.0f}%（已含成本）",
             f"盈亏比 {ex['payoff']:.2f}，利润因子 {ex['pf']:.2f}（> 1 才赚钱）"]
    prof = sorted((_pnl(p) for p in closed if _pnl(p) > 0), reverse=True)
    if prof:
        lines.append(f"大赢家：最赚的 3 笔占全部盈利的 {sum(prof[:3]) / sum(prof) * 100:.0f}%，"
                     f"最大一笔 {max(_ret(p) for p in closed) * 100:+.0f}%")
    flown = [p for p in closed if float(p.get("post_max") or 0) >= 2]
    known = [p for p in closed if p.get("post_max") is not None and float(p["post_max"]) > 0]
    if known:
        by = Counter(reason_cat(p.get("close_reason")) for p in flown)
        lines.append(f"卖飞：平仓后 4 小时内又涨到卖出价 2 倍以上的 {len(flown)}/{len(known)} 笔"
                     + ("（" + "、".join(f"{k} {v}" for k, v in by.most_common()) + "）" if by else ""))
    return lines


async def daily_review(app: "App", start: float) -> str:
    db = app.db
    dry = int(app.cfg.dry_run)
    closed = await db.fetchall("SELECT * FROM positions WHERE status!='open' AND closed_at>=? AND dry_run=?",
                               (start, dry))
    lines = [f"📅 <b>今日复盘</b> {app.tg.mode()}（只统计{'模拟盘' if dry else '实盘'}的单子）"]
    other = await db.fetchone("SELECT COUNT(*) n, COALESCE(SUM(realized_usd-cost_usd),0) s FROM positions "
                              "WHERE status!='open' AND closed_at>=? AND dry_run!=?", (start, dry))
    if other and other["n"]:
        lines.append(f"（今天另有{'实盘' if dry else '模拟盘'} {other['n']} 笔，合计 {fmt_usd(other['s'])}，不计入下面的统计）")
    if closed:
        pnl = sum(_pnl(p) for p in closed)
        wins = sum(1 for p in closed if _pnl(p) > 0)
        lines.append(f"平仓 {len(closed)} 笔：赚 {wins} 笔、亏 {len(closed) - wins} 笔，"
                     f"胜率 {wins / len(closed) * 100:.0f}%，已实现 <b>{fmt_usd(pnl)}</b>")
        lines += payoff_lines(closed)
        lines.append("<b>按买入来源</b>")
        lines += _group(closed, lambda p: re.split(r"[（(|]", p.get("reason") or "其他")[0].strip() or "其他")
        lines.append("<b>按档位</b>")
        lines += _group(closed, lambda p: TIER_LABEL.get(p.get("tier"), "老持仓"))
        lines.append("<b>按卖出原因</b>（最后一笔）")
        lines += _group(closed, lambda p: reason_cat(p.get("close_reason")))
    else:
        lines.append("今天没有平仓")
    sk = await skipped_review(app, start)
    if sk:
        lines.append(sk)
    from ..brain import lessons_text, missed_text
    for fn in (lessons_text, missed_text):
        try:
            txt = await fn(app)
        except Exception:  # noqa: BLE001
            txt = ""
        if txt:
            lines.append(txt)
    br = getattr(app, "brain", None)
    if br:
        tl = [h for h in (await db.kv_get("tune_log", []) or []) if h["ts"] >= start]
        lines.append(f"🧠 自我学习：今天调整了 {len(tl)} 个参数" + ("（" + "、".join(
            f"{h['key'].split('.')[-1]} {h['old']}→{h['new']}" for h in tl) + "）" if tl else "")
                     + "，发 /tune 查看")
    from .regime import regime, text as rg_text
    lines.append(rg_text(await regime(app)))
    n_open = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE status='open'")
    lines.append(f"当前持仓 {n_open['n']} 个")
    mode = await app.positions.mode()
    if mode != "off":
        lines.append(f"<b>动态管理</b>（{MODE_TEXT[mode]}）\n{await shadow_compare(app)}")
    nw = await db.fetchone("SELECT COUNT(*) n FROM wallets WHERE status IN ('active','manual')")
    lines.append(f"监听聪明钱 {nw['n']} 个")
    sig = await db.fetchone("SELECT COUNT(*) n FROM signals WHERE ts>=?", (start,))
    neww = await db.fetchone("SELECT COUNT(*) n FROM wallets WHERE created_at>=? AND status='active'", (start,))
    lines.append(f"聪明钱信号 {sig['n']} 条，新增聪明钱 {neww['n']} 个\n{app.data.usage_text()}")
    return "\n".join(lines)


MODE_TEXT = {"off": "已关闭", "shadow": "观察中，只记录不执行", "live": "已接管卖出"}


async def skipped_review(app: "App", start: float) -> str:
    """被风控拦下的信号，1 小时后涨跌如何。平均为负说明拦得对；经常大涨说明这条过滤可能太严。"""
    rows = await app.db.fetchall(
        "SELECT cat, price, px_1h FROM skipped WHERE ts>=? AND px_1h>0 AND price>0", (start - 3600,))
    if not rows:
        return ""
    g: dict[str, list[float]] = {}
    for r in rows:
        g.setdefault(r["cat"], []).append((float(r["px_1h"]) / float(r["price"]) - 1) * 100)
    lines = ["<b>被过滤的信号</b>（1 小时后涨跌，平均为负说明拦得对）"]
    for cat, ch in sorted(g.items(), key=lambda kv: -len(kv[1])):
        up = sum(1 for c in ch if c >= 30)
        down = sum(1 for c in ch if c <= -30)
        lines.append(f"  {cat}：{len(ch)} 个，平均 {sum(ch) / len(ch):+.0f}%（大涨 {up} 个 / 大跌 {down} 个）")
    return "\n".join(lines)


def _mult(s: dict) -> float:
    """虚拟持仓的收益倍数（已卖出部分 + 剩余部分按最新价）。"""
    entry = float(s["entry_price"] or 0)
    left = float(s["frac"] or 0) * float(s["last_price"] or 0) / entry if entry else 0
    return float(s["realized"] or 0) + left


async def shadow_compare(app: "App") -> str:
    """观察模式：同一批持仓按旧规则 / 动态管理 / 高手规则分别虚拟卖出，比较谁赚得多。"""
    from .positions import VARIANT_NAME
    rows = await app.db.fetchall(
        "SELECT s.*, p.entry_price, p.cost_usd, p.symbol FROM shadow s JOIN positions p ON p.id=s.pid")
    by: dict[int, dict[str, dict]] = {}
    for r in rows:
        by.setdefault(r["pid"], {})[r["variant"]] = r
    running = sum(1 for v in by.values() if not all(x["closed"] for x in v.values()))
    lines: list[str] = []
    for b in ("v2", "v3"):
        done = [(pid, v) for pid, v in by.items() if "v1" in v and b in v and v["v1"]["closed"] and v[b]["closed"]]
        if not done:
            continue
        u1 = sum(float(v["v1"]["cost_usd"] or 0) * (_mult(v["v1"]) - 1) for _, v in done)
        ub = sum(float(v[b]["cost_usd"] or 0) * (_mult(v[b]) - 1) for _, v in done)
        a1 = sum(_mult(v["v1"]) - 1 for _, v in done) / len(done) * 100
        ab = sum(_mult(v[b]) - 1 for _, v in done) / len(done) * 100
        better = sum(1 for _, v in done if _mult(v[b]) > _mult(v["v1"]) + 0.005)
        worse = sum(1 for _, v in done if _mult(v[b]) < _mult(v["v1"]) - 0.005)
        name = VARIANT_NAME[b]
        lines += [f"{name} vs 当前规则（{len(done)} 个持仓，卖出已扣估算成本）：",
                  f"  当前规则 平均 {a1:+.1f}%，合计 {fmt_usd(u1)} | {name} 平均 {ab:+.1f}%，合计 {fmt_usd(ub)}",
                  f"  {name}更好 {better} 个，更差 {worse} 个"]
        for pid, v in sorted(done, key=lambda x: abs(_mult(x[1][b]) - _mult(x[1]["v1"])), reverse=True)[:2]:
            d = (_mult(v[b]) - _mult(v["v1"])) * 100
            if abs(d) < 0.5:
                break
            last = (json.loads(v[b]["log"] or "[]") or [[0, 0, "", 0]])[-1][2]
            lines.append(f"  #{pid} {v['v1']['symbol']}：{name} {d:+.0f}%（{last[:40]}）")
    if not lines:
        return f"还没有可以对比的已卖完持仓（进行中 {running} 个）"
    return "\n".join(lines + [f"（进行中 {running} 个）"])


async def why_text(app: "App", pid: int) -> str:
    db = app.db
    p = await db.fetchone("SELECT * FROM positions WHERE id=?", (pid,))
    if not p:
        return "没有这个持仓编号"
    now = time.time()
    pm = app.positions
    mode = await pm.mode()
    px = await app.data.price(p["chain"], p["token"]) if p["status"] == "open" else 0.0
    entry = float(p["entry_price"] or 0)
    gain = (px / entry - 1) * 100 if px and entry else 0.0
    high_gain = (float(p["high_price"] or entry) / entry - 1) * 100 if entry else 0.0
    held = (now - float(p["opened_at"] or now)) / 3600
    tier = TIER_LABEL.get(p.get("tier"), "老持仓")
    lines = [f"<b>#{pid} {p['symbol']}</b> {tier} | 状态 {p['status']}"]
    if p["status"] == "open":
        lines.append(f"现 {gain:+.1f}% | 最高 {high_gain:+.0f}% | 已拿 {held:.1f} 小时 | 成本 {fmt_usd(p['cost_usd'])}")
    else:
        lines.append(f"已平仓：{p['close_reason']} | 盈亏 {fmt_usd(_pnl(p))}")

    # ---- 健康分 ----
    if p.get("health") is not None:
        ago = (now - float(p["health_at"] or now)) / 60
        lines.append(f"\n<b>健康分 {p['health']}</b>（{ago:.0f} 分钟前复查，70 为中性）")
        notes = json.loads(p.get("health_note") or "[]")
        lines += [f"  {n}" for n in notes] or ["  各项数据和买入时差不多"]
        if p.get("health_emergency"):
            lines.append(f"  🚨 {p['health_emergency']}")
    elif mode != "off":
        lines.append("\n健康分：还没复查过（每 2 分钟一轮）")

    # ---- 当前规则 ----
    r = exit_rules(pm.exit_cfg, p.get("tier"))
    dc = pm.dcfg
    dyn = dyn_state(p, dc, now) if mode == "live" else None
    strong = bool(dyn) and dyn["health"] is not None and dyn["health"] >= float(dc["strong_above"])
    rules = [f"止损 {float(r.get('stop_loss_pct', -35)):.0f}%"]
    if mode == "live":
        be = float(dc["breakeven_after_pct"])
        rules.append(f"保本止损{'已启用' if high_gain >= be else f'（涨过 +{be:.0f}% 后启用）'}")
    tps = r.get("take_profit") or []
    st = int(p["tp_stage"] or 0)
    if st < len(tps):
        rules.append(f"下一档止盈 +{float(tps[st]['gain_pct']):.0f}% 卖 {float(tps[st]['sell_pct']):.0f}%"
                     + ("（强势，暂缓）" if strong and st >= 1 else ""))
    if r.get("stall_hours") and st == 0:
        rules.append(f"{float(r['stall_hours']) * (2 if strong else 1):g} 小时涨不到 "
                     f"+{float(r.get('stall_min_gain_pct', 0)):g}% 就卖")
    rules.append(f"最长拿 {float(r.get('max_hold_hours', 24)) * (1.5 if strong else 1):g} 小时")
    lines.append("\n<b>卖出规则</b>：" + " | ".join(rules))
    lines.append(f"动态管理：{MODE_TEXT[mode]}" + ("，强势持仓已放宽" if strong else ""))

    # ---- 观察模拟 ----
    sh = {s["variant"]: s for s in await db.fetchall(
        "SELECT s.*, p.entry_price FROM shadow s JOIN positions p ON p.id=s.pid WHERE s.pid=?", (pid,))}
    if sh:
        def desc(s: dict) -> str:
            st = "已卖完" if s["closed"] else f"还剩 {float(s['frac']) * 100:.0f}%"
            return f"{st}，收益 {(_mult(s) - 1) * 100:+.1f}%"
        if "v1" in sh and "v2" in sh:
            lines.append(f"\n<b>模拟对比</b>：当前规则 {desc(sh['v1'])} | 动态管理 {desc(sh['v2'])}"
                         + (f" | 高手规则 {desc(sh['v3'])}" if "v3" in sh else ""))
            lg = json.loads(sh["v2"]["log"] or "[]")
            if lg:
                lines.append(f"  新规则最近一次：卖 {lg[-1][1]}%（{lg[-1][2]}）")
    return "\n".join(lines)
