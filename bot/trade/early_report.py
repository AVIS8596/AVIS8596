"""早期赢家 vs 早期骗局（/learn early）：从已经涨起来 / 已经归零的币身上，反推它们刚被发现时长什么样。

数据：主动建仓扫描时给"快毕业 / 新发"的币拍的快照（early_obs，每 2~3 分钟一张）+ 第一次看到后 1 小时 / 4 小时的价格（early_tok）。
- 赢家：之后最高到第一次看到时价格的 3 倍以上
- 骗局 / 归零：之后取不到价格（池子被撤 / 归零），或者跌到 3 成以下
看的是"刚被发现的那一刻"和"发现后 10 分钟"的数据：持有人、成交、跑路概率、前10、捆绑、老鼠仓、dev 持仓、刷量、聪明钱、KOL、
发币多久了、10 分钟里持有人和价格的变化。每项分成几档，看各档里赢家多还是骗局多。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..utils import f

if TYPE_CHECKING:
    from ..app import App

WIN_X, DEAD_X = 3.0, 0.3
# (特征, 显示名, 分档边界)
FEATS = [
    ("age_min", "发现时发币多久（分钟）", [5, 15, 60]),
    ("holders", "发现时持有人", [50, 150, 400]),
    ("volume", "发现时成交额$", [5_000, 20_000, 60_000]),
    ("rug", "跑路概率%", [1, 20, 50]),
    ("top10", "前10持仓%", [15, 25, 40]),
    ("bundler", "捆绑交易%", [5, 15, 30]),
    ("insider", "老鼠仓%", [1, 5, 15]),
    ("creator", "dev 持仓%", [0.5, 3, 10]),
    ("wash", "刷量（1=是）", [1]),
    ("smart", "聪明钱", [1, 2, 4]),
    ("kol", "KOL", [1, 2, 4]),
    ("h_growth", "10 分钟持有人增长%", [10, 30, 80]),
    ("p_change", "10 分钟价格变化%", [-20, 20, 100]),
]


def _pct(x: float) -> float:
    return x * 100 if 0 < x <= 1 else x


def outcome(p0: float, p60: float | None, p240: float | None) -> str:
    """win / dead / mid / ""（还没结果）。"""
    vals = [p for p in (p60, p240) if p is not None]
    if not vals or p0 <= 0:
        return ""
    if any(p == -1 for p in vals) and not any(p > 0 for p in vals):
        return "dead"
    priced = [p for p in vals if p > 0]
    if not priced:
        return ""
    best, last = max(priced) / p0, priced[-1] / p0
    if best >= WIN_X:
        return "win"
    if last <= DEAD_X or (p240 is not None and p240 == -1):
        return "dead"
    return "mid"


def features(snaps: list[dict]) -> dict:
    """第一次看到时 + 10 分钟后的快照 → 特征。snaps 按时间排好。"""
    s0 = snaps[0]
    out = {"age_min": (f(s0["ts"]) - f(s0["created"])) / 60 if f(s0["created"]) else None,
           "holders": f(s0["holders"]), "volume": f(s0["volume"]), "rug": _pct(f(s0["rug"])),
           "top10": _pct(f(s0["top10"])), "bundler": _pct(f(s0["bundler"])), "insider": _pct(f(s0["insider"])),
           "creator": _pct(f(s0["creator"])), "wash": int(s0["wash"] or 0), "smart": int(s0["smart"] or 0),
           "kol": int(s0["kol"] or 0)}
    later = [s for s in snaps if f(s["ts"]) - f(s0["ts"]) >= 480]       # 8 分钟以后的第一张，近似"10 分钟后"
    if later and f(s0["holders"]) > 0 and f(s0["price"]) > 0:
        s1 = later[0]
        out["h_growth"] = (f(s1["holders"]) / f(s0["holders"]) - 1) * 100
        out["p_change"] = (f(s1["price"]) / f(s0["price"]) - 1) * 100 if f(s1["price"]) > 0 else None
    return out


def analyze(tokens: list[tuple[dict, str]]) -> dict:
    """tokens：(特征, 结果) → 各特征分档后的赢家率 / 骗局率。"""
    done = [(ft, o) for ft, o in tokens if o]
    n = len(done)
    if not n:
        return {"n": 0}
    base_win = sum(1 for _, o in done if o == "win") / n
    base_dead = sum(1 for _, o in done if o == "dead") / n
    res = {"n": n, "win": base_win, "dead": base_dead, "features": {}}
    for key, name, edges in FEATS:
        groups: dict[int, list[str]] = {}
        for ft, o in done:
            v = ft.get(key)
            if v is None:
                continue
            groups.setdefault(sum(1 for e in edges if f(v) >= e), []).append(o)
        rows = []
        for i, os_ in sorted(groups.items()):
            if len(os_) < 15:
                continue
            rows.append((label(edges, i), len(os_), sum(1 for o in os_ if o == "win") / len(os_),
                         sum(1 for o in os_ if o == "dead") / len(os_)))
        if len(rows) >= 2:
            res["features"][name] = rows
    return res


def label(edges: list[float], i: int) -> str:
    def k(x: float) -> str:
        return f"{x / 1000:g}K" if abs(x) >= 1000 else f"{x:g}"
    if i == 0:
        return f"＜{k(edges[0])}"
    if i == len(edges):
        return f"≥{k(edges[-1])}"
    return f"{k(edges[i - 1])}~{k(edges[i])}"


async def early_report(app: "App", days: float = 4) -> str:
    db = app.db
    since = time.time() - days * 86400
    toks = await db.fetchall("SELECT chain, token, first_ts, p60, p240 FROM early_tok WHERE first_ts>? "
                             "AND (p60 IS NOT NULL OR p240 IS NOT NULL)", (since,))
    if not toks:
        return "还没有早期快照的结果（主动建仓扫描后 1 小时才有第一批结果）"
    snaps: dict[tuple, list[dict]] = {}
    rows = await db.fetchall("SELECT * FROM early_obs WHERE ts>? ORDER BY ts", (since - 3600,))
    for r in rows:
        snaps.setdefault((r["chain"], r["token"]), []).append(dict(r))
    data = []
    for t in toks:
        ss = snaps.get((t["chain"], t["token"]))
        if not ss:
            continue
        p0 = f(ss[0]["price"])
        o = outcome(p0, None if t["p60"] is None else f(t["p60"]), None if t["p240"] is None else f(t["p240"]))
        data.append((features(ss), o))
    res = analyze(data)
    if res["n"] < 50:
        return f"早期快照里有结果的币只有 {res['n']} 个，至少 50 个才有统计意义，过几个小时再看"
    L = [f"🔬 <b>早期赢家 vs 早期骗局</b>（最近 {days:g} 天主动建仓扫描到的 {res['n']} 个币，按刚被发现时的数据分档）",
         f"赢家 = 之后最高涨到发现价 {WIN_X:g} 倍以上；骗局 / 归零 = 之后取不到价格或跌到 {DEAD_X * 10:g} 成以下",
         f"全部：赢家 {res['win'] * 100:.0f}%，骗局 / 归零 {res['dead'] * 100:.0f}%"]
    spread = []
    for name, rows_ in res["features"].items():
        L.append(f"\n<b>{name}</b>")
        for lab, n, w, d in rows_:
            L.append(f"  {lab}：{n} 个，赢家 {w * 100:.0f}%，骗局 {d * 100:.0f}%")
            spread.append((w - d - (res["win"] - res["dead"]), f"{name} {lab}（赢家 {w * 100:.0f}% / 骗局 {d * 100:.0f}%，{n} 个）"))
    spread.sort(reverse=True)
    L.append("\n<b>最能提前认出赢家的几档</b>：" + "；".join(s for _, s in spread[:4]))
    L.append("<b>最像骗局的几档</b>：" + "；".join(s for _, s in spread[::-1][:4]))
    L.append("（只是相关，不一定是原因；样本越多越可靠。新记录的 GMGN 数据（新钱包、蓝筹、钓鱼钱包……）看 /learn data）")
    return "\n".join(L)
