"""/history：一个币在机器人这里的完整时间线 —— 发币、第一次被看到、每次分析、每次被拦（原因）、买卖。
用来回答"为什么买得这么晚 / 为什么没买"：看第一次看到它时已经涨了几倍，以及中间被哪条规则拦了。"""
from __future__ import annotations

import html
import json
import time
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

from ..utils import f, fmt_usd

if TYPE_CHECKING:
    from ..app import App


def _x(px: float, base: float) -> str:
    return f"×{px / base:.1f}" if base > 0 and px > 0 else ""


async def launch_price(app: "App", chain: str, token: str, created: float) -> float:
    """发币后第一根 5 分钟 K 线的开盘价（取不到返回 0）。"""
    if not created or not getattr(app, "gmgn", None):
        return 0.0
    try:
        rows = await app.gmgn.kline(chain, token, "5m", int(created) - 60, int(min(created + 3600, time.time())))
    except Exception:  # noqa: BLE001
        return 0.0
    rows = sorted(rows, key=lambda r: f(r.get("time")))
    for r in rows:
        if f(r.get("open")) > 0:
            return f(r.get("open"))
    return 0.0


def build(app_tz, symbol: str, created: float, base: float, outcomes: list[dict], skipped: list[dict],
          positions: list[dict], now: float, notes: list[dict] | None = None) -> str:
    def hm(ts: float) -> str:
        return datetime.fromtimestamp(ts, app_tz).strftime("%m-%d %H:%M")

    ev: list[tuple[float, str]] = []
    for o in outcomes:
        ft = json.loads(o.get("features") or "{}")
        ev.append((o["ts"], f"🔍 分析（{html.escape(str(o.get('source') or '?'))}）{_x(o['price'], base)} 评分 {ft.get('score', '?')}，"
                            f"市值 {fmt_usd(f(ft.get('mcap')))}，1 小时 {f(ft.get('change_1h')):+.0f}%，"
                            f"聪明钱 {ft.get('smart', 0)} 个"))
    for s in skipped:
        ev.append((s["ts"], f"❌ 没买（{s.get('trigger') or '?'}）{_x(f(s.get('price')), base)}：{html.escape(str(s.get('reason')))}"))
    for n in notes or []:
        ev.append((n["ts"], f"🧪 {html.escape(str(n.get('msg')))} {_x(f(n.get('price')), base)}".rstrip()))
    for p in positions:
        ev.append((p["opened_at"], f"✅ 买入 #{p['id']} {_x(f(p['entry_price']), base)}：{html.escape(p.get('reason') or '')}"))
        if p.get("closed_at"):
            ev.append((p["closed_at"], f"💰 平仓 #{p['id']}：{html.escape(p.get('close_reason') or '')}"
                                       f"（盈亏 {fmt_usd(f(p.get('realized_usd')))}）"))
    ev.sort()
    head = [f"<b>{html.escape(symbol)} 在机器人这里的时间线</b>"]
    if created:
        head.append(f"发币时间 {hm(created)}（{(now - created) / 3600:.1f} 小时前）"
                    + ("，下面的 ×N 是相对发币价的倍数" if base else ""))
    if not ev:
        return "\n".join(head + ["", "机器人还没看到过这个币：它没进过新币列表 / 主动建仓观察名单，也没有监听的钱包买它"])
    first = ev[0][0]
    if created:
        pts = sorted([(o["ts"], f(o.get("price"))) for o in outcomes] + [(x["ts"], f(x.get("price"))) for x in skipped]
                     + [(p["opened_at"], f(p.get("entry_price"))) for p in positions]
                     + [(n["ts"], f(n.get("price"))) for n in notes or []])
        px0 = next((px for _, px in pts if px > 0), 0.0)
        mult = f"，当时价格已经是发币价的 {px0 / base:.1f} 倍" if base > 0 and px0 > 0 else ""
        head.append(f"第一次看到：发币后 {(first - created) / 60:.0f} 分钟{mult}")
    buy = next((ts for ts, txt in ev if txt.startswith("✅")), None)
    if buy:
        head.append(f"从第一次看到到买入：{(buy - first) / 60:.0f} 分钟")
    from .trader import skip_cat
    blocked = Counter(skip_cat(str(s.get("reason") or "")) for s in skipped if not buy or s["ts"] < buy)
    if blocked:
        head.append("拦下它的规则：" + html.escape("；".join(f"{k} ×{n}" for k, n in blocked.most_common(3))))
    rows = [f"{hm(ts)} {txt}" for ts, txt in ev]
    if len(rows) > 20:   # 开头（为什么没早买）和结尾（最近发生了什么）都重要，中间省略
        rows = rows[:8] + [f"……中间省略 {len(rows) - 20} 条……"] + rows[-12:]
    lines = head + [""] + rows
    return "\n".join(lines)


async def history(app: "App", chain: str, token: str) -> str:
    db = app.db
    outcomes = [dict(r) for r in await db.fetchall(
        "SELECT ts, source, price, features FROM token_outcomes WHERE chain=? AND token=? ORDER BY ts", (chain, token))]
    skipped = [dict(r) for r in await db.fetchall(
        "SELECT ts, trigger, reason, price, symbol FROM skipped WHERE chain=? AND token=? ORDER BY ts", (chain, token))]
    positions = [dict(r) for r in await db.fetchall(
        "SELECT id, opened_at, closed_at, entry_price, reason, close_reason, realized_usd, symbol FROM positions "
        "WHERE chain=? AND token=? ORDER BY opened_at", (chain, token))]
    try:
        notes = [dict(r) for r in await db.fetchall(
            "SELECT ts, msg, price FROM strat_log WHERE chain=? AND token=? ORDER BY ts", (chain, token))]
    except Exception:  # noqa: BLE001
        notes = []
    try:
        ov = await app.data.overview(chain, token)
    except Exception:  # noqa: BLE001
        ov = {}
    created = f(ov.get("created_ts"))
    symbol = ov.get("symbol") or next((x.get("symbol") for x in positions + skipped if x.get("symbol")), "?")
    base = await launch_price(app, chain, token, created)
    return build(app.tz, str(symbol), created, base, outcomes, skipped, positions, time.time(), notes)
