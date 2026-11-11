"""影子采样：把"按我们的规则买卖、到底赚多少"算准。

结果档案（token_outcomes）里有机器人分析过的每个币，但结果只看 1 小时 / 4 小时后两个时间点的价格，
中间发生的事都看不到（一根线砸到 -99%、先跌 40% 止损出局又涨回来、冲到 +300% 先出本……），选币模型的成绩因此虚高。
这里对其中**通过防收割检查**的币（不管机器人有没有买入理由），在它被分析的那一刻记一笔虚拟买入，
满 4 小时后拉完整 K 线，按真实的卖出规则（止损、出本、强势长拿……）一步步模拟，结果写进 token_outcomes.sim_ret。
- 每天最多采 sample_per_day 个（默认 500，每个要拉一次 K 线），同一个币 6 小时内只采一次
- K 线在 4 小时内就断了、之后也取不到价格：池子被撤 / 归零，按归零算（不能按最后一笔的高价算）
- /learn sample 看结果：各项特征分档后，按我们的规则买卖每笔赚多少
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from ..utils import f
from .positions import exit_rules
from .replay import candles, simulate, to_path

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.sampler")
SKIP, NODATA = -8.0, -9.0          # sim_ret 的特殊值：不符合采样条件 / 取不到 K 线


def eligible(ft: dict, cfg) -> bool:
    """通过防收割检查（和真实买入的硬性风控同一口径，用分析时存下的数据近似）。"""
    t = cfg.get("trading", {}) or {}
    return (f(ft.get("risks")) == 0 and f(ft.get("liquidity")) >= float(t.get("min_liquidity_usd", 8000))
            and f(ft.get("bundle_held_pct")) <= float(t.get("max_bundle_pct", 15))
            and f(ft.get("insider_held_pct")) <= float(t.get("max_insider_pct", 20))
            and f(ft.get("top10_pct")) <= float(t.get("max_top10_pct", 45))
            and f(ft.get("cabal_pct")) < float(t.get("max_cabal_pct", 3) or 999)
            and not int(f(ft.get("lp_pullable"))))


def dead_tail(path: list[tuple[float, float]], t0: float, now: float, gone: bool) -> list[tuple[float, float]]:
    """K 线提前断了（之后 2 小时以上没有成交）而且之后取不到价格：池子被撤 / 归零，在断掉的地方补一个归零点。"""
    if not path:
        return path
    end = min(now, t0 + 24 * 3600)
    if gone and end - path[-1][0] >= 2 * 3600:
        return path + [(path[-1][0] + 60, path[-1][1] * 0.001)]
    return path


async def run_batch(app: "App", limit: int = 20) -> int:
    db = app.db
    now = time.time()
    per_day = int(app.cfg.get("learning.sample_per_day", 500))
    day = int(now // 86400)
    used = await db.kv_get("sample_used", {}) or {}
    n_today = int(used.get(str(day), 0))
    if n_today >= per_day:
        return 0
    rows = await db.fetchall(
        "SELECT id, ts, chain, token, price, features, p240 FROM token_outcomes WHERE sim_ret IS NULL AND price>0 "
        "AND ts<? AND ts>? ORDER BY ts DESC LIMIT ?", (now - 4 * 3600, now - 3 * 86400, limit * 5))
    exit_cfg = app.cfg.get("exit", {}) or {}
    cost = float(exit_cfg.get("shadow_cost_pct", 2)) / 100
    done = 0
    for r in rows:
        if done >= limit or n_today >= per_day:
            break
        try:
            ft = json.loads(r["features"] or "{}")
        except (TypeError, ValueError):
            ft = {}
        if not eligible(ft, app.cfg):
            await db.execute("UPDATE token_outcomes SET sim_ret=? WHERE id=?", (SKIP, r["id"]))
            continue
        prev = await db.fetchone("SELECT sim_ret FROM token_outcomes WHERE chain=? AND token=? AND sim_ret>-2 "
                                 "AND ts BETWEEN ? AND ?", (r["chain"], r["token"], r["ts"] - 6 * 3600, r["ts"] + 6 * 3600))
        if prev:
            await db.execute("UPDATE token_outcomes SET sim_ret=? WHERE id=?", (SKIP, r["id"]))   # 同一个币只采一次
            continue
        ret = NODATA
        try:
            path = to_path(await candles(app, r["chain"], r["token"], float(r["ts"])))
            await asyncio.sleep(0.5)                       # 后台慢慢采，别和交易抢 GMGN 额度
        except Exception as e:  # noqa: BLE001
            log.debug("影子采样取 K 线失败 %s: %s", r["token"], e)
            path = []
        if len(path) >= 8 and 0.1 < path[0][1] / float(r["price"]) < 10:
            path = dead_tail(path, float(r["ts"]), now, r["p240"] is not None and float(r["p240"]) < 0)
            entry = float(r["price"]) * 1.01
            m = simulate(entry, float(r["ts"]), path, dict(exit_rules(exit_cfg, "normal")), None, 100, cost)
            ret = m - 1 if m < 200 else NODATA
        await db.execute("UPDATE token_outcomes SET sim_ret=? WHERE id=?", (ret, r["id"]))
        done += 1
        n_today += 1
    used = {str(day): n_today}
    await db.kv_set("sample_used", used)
    return done


async def sample_report(app: "App", days: float = 3) -> str:
    from .insight import BUCKETS, _label, _stats
    rows = await app.db.fetchall("SELECT features, sim_ret, bought, source FROM token_outcomes WHERE ts>? "
                                 "AND sim_ret BETWEEN -1 AND 199", (time.time() - days * 86400,))
    if len(rows) < 50:
        return (f"影子采样还只有 {len(rows)} 个结果（每个币要满 4 小时才模拟，每天最多采 "
                f"{int(app.cfg.get('learning.sample_per_day', 500))} 个），至少 50 个再看")
    data = []
    for r in rows:
        ft = json.loads(r["features"] or "{}")
        b, s = f(ft.get("buys_1h")), f(ft.get("sells_1h"))
        ft["buy_sell"] = b / s if s > 0 else (3.0 if b > 0 else 0.0)
        if f(ft.get("mcap")) > 0 and ft.get("liquidity") is not None:
            ft["liq_mcap"] = f(ft.get("liquidity")) / f(ft.get("mcap"))
        data.append((ft, float(r["sim_ret"]), int(r["bought"] or 0)))
    allst = _stats([d[1] for d in data])
    L = [f"🎯 <b>影子采样</b>（最近 {days:g} 天 {len(data)} 个通过防收割检查的币，在被分析时虚拟买入，按真实卖出规则模拟到底）",
         f"全部：每笔平均 {allst['mean'] * 100:+.1f}%，中位 {allst['med'] * 100:+.0f}%，"
         f"赚 30% 以上 {allst['hit'] * 100:.0f}%，亏 30% 以上 {allst['dump'] * 100:.0f}%"]
    bought = [d[1] for d in data if d[2]]
    if len(bought) >= 10:
        b = _stats(bought)
        L.append(f"其中机器人真买了的 {len(bought)} 个：每笔平均 {b['mean'] * 100:+.1f}%，中位 {b['med'] * 100:+.0f}%")
    best = []
    for key, name, edges in BUCKETS:
        groups: dict[int, list[float]] = {}
        for ft, ret, _ in data:
            v = ft.get(key)
            if v is None:
                continue
            groups.setdefault(sum(1 for e in edges if f(v) >= e), []).append(ret)
        rows_ = [(_label(edges, i), _stats(g)) for i, g in sorted(groups.items()) if len(g) >= 15]
        if len(rows_) < 2:
            continue
        L.append(f"\n<b>{name}</b>")
        for lab, st in rows_:
            L.append(f"  {lab}：{st['n']} 个，每笔平均 {st['mean'] * 100:+.1f}%，中位 {st['med'] * 100:+.0f}%")
            best.append((st["mean"], st["n"], f"{name} {lab}"))
    best.sort(reverse=True)
    L.append("\n<b>按我们的规则买卖最赚的几档</b>：" + "；".join(f"{n}（每笔 {m * 100:+.0f}%，{k} 个）" for m, k, n in best[:4]))
    L.append("<b>最亏的几档</b>：" + "；".join(f"{n}（每笔 {m * 100:+.0f}%，{k} 个）" for m, k, n in best[::-1][:4]))
    L.append("（每笔平均里单笔最多按 +300% 算，免得一个百倍币把平均带飞；样本越多越可靠）")
    return "\n".join(L)


class Sampler:
    def __init__(self, app: "App"):
        self.app = app

    async def run(self) -> None:
        await asyncio.sleep(1200)
        while True:
            try:
                n = await run_batch(self.app)
                if n:
                    log.info("影子采样：模拟了 %d 个币", n)
            except Exception as e:  # noqa: BLE001
                log.exception("影子采样出错: %s", e)
            await asyncio.sleep(600)
