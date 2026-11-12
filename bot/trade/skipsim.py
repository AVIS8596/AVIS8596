"""影子单：每个被风控拦下的信号，满 4 小时后拉真实 K 线，按真实的卖出规则（止损、出本、强势长拿……）完整模拟一遍，
算出"如果当时买了，按规则卖，最后赚多少"。这样每条风控规则拦对了还是拦错了都算得清楚，又不用真的去买可疑的币。
同一个币 6 小时内被拦多次只模拟第一次（其余复用结果）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .positions import active_rules
from .replay import candles, fair_entry, simulate

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.skipsim")


async def run_batch(app: "App", limit: int = 30) -> int:
    db = app.db
    now = time.time()
    rows = await db.fetchall(
        "SELECT id, ts, chain, token, price FROM skipped WHERE sim_ret IS NULL AND price>0 AND ts<? AND ts>? "
        "ORDER BY ts LIMIT ?", (now - 4 * 3600, now - 3 * 86400, limit))
    exit_cfg = app.cfg.get("exit", {}) or {}
    cost = float(exit_cfg.get("shadow_cost_pct", 2)) / 100
    done = 0
    for r in rows:
        prev = await db.fetchone("SELECT sim_ret FROM skipped WHERE chain=? AND token=? AND sim_ret IS NOT NULL "
                                 "AND ts BETWEEN ? AND ? LIMIT 1", (r["chain"], r["token"], r["ts"] - 6 * 3600, r["ts"]))
        ret = prev["sim_ret"] if prev else None
        if ret is None:
            try:
                px0, path = fair_entry(await candles(app, r["chain"], r["token"], float(r["ts"])), float(r["ts"]))
                await asyncio.sleep(0.4)          # 后台慢慢算，别和交易抢 GMGN 额度
            except Exception as e:  # noqa: BLE001
                log.debug("影子单取 K 线失败 %s: %s", r["token"], e)
                px0, path = 0.0, []
            if len(path) >= 8 and not (px0 > 0 and 0.1 < px0 / float(r["price"]) < 10):
                path = []            # 记录的价格和 K 线对不上（单位不对 / 别的池子）：不算
            if len(path) >= 8 and min(now, float(r["ts"]) + 24 * 3600) - path[-1][0] >= 2 * 3600:
                # K 线提前断了：现在也取不到价格 = 池子被撤 / 归零，按归零算（不能按最后一笔的高价算）
                try:
                    px = (await app.data.prices(r["chain"], [r["token"]])).get(r["token"])
                except Exception:  # noqa: BLE001
                    px = 1.0
                from .sampler import dead_tail
                path = dead_tail(path, float(r["ts"]), now, not px)
            if len(path) >= 8:
                entry = px0 * 1.01               # 30 秒确认期后第一根 K 线的开盘价 + 买入滑点
                m = simulate(entry, float(r["ts"]), path, dict(active_rules(exit_cfg)), None, 100, cost)
                ret = m - 1 if m < 200 else -9.0
            else:
                ret = -9.0           # 取不到 K 线：标记跳过（不参与统计）
        await db.execute("UPDATE skipped SET sim_ret=? WHERE id=?", (ret, r["id"]))
        done += 1
    return done


async def summary(app: "App", start: float) -> dict[str, tuple[int, float, float]]:
    """各类拦截原因的影子单结果：类别 → (笔数, 每笔平均, 中位)。"""
    rows = await app.db.fetchall("SELECT cat, sim_ret FROM skipped WHERE ts>=? AND sim_ret BETWEEN -1 AND 199",
                                 (start,))
    g: dict[str, list[float]] = {}
    for r in rows:
        g.setdefault(r["cat"], []).append(float(r["sim_ret"]))
    return {k: (len(v), sum(v) / len(v), sorted(v)[len(v) // 2]) for k, v in g.items()}


async def recompute_fair(app: "App", days: float = 3) -> int:
    """一次性（11-12）：最近几天的影子单 / 影子采样按公平入场（30 秒后第一根完整 K 线开盘价）和现在实际用的卖法重算。
    只用已经缓存的 K 线，不花额度；没缓存的清空，按正常流程重新算。返回重算的条数。"""
    import json
    from .sampler import dead_tail
    db = app.db
    if await db.kv_get("fair_sim_1112", False):
        return 0
    exit_cfg = app.cfg.get("exit", {}) or {}
    cost = float(exit_cfg.get("shadow_cost_pct", 2)) / 100
    rules = active_rules(exit_cfg)
    now = time.time()
    n = 0
    for table in ("skipped", "token_outcomes"):
        rows = await db.fetchall(f"SELECT id, ts, chain, token, price FROM {table} WHERE ts>? AND sim_ret BETWEEN -1 AND 199",
                                 (now - days * 86400,))
        for r in rows:
            c = await db.fetchone("SELECT data FROM kline_cache WHERE chain=? AND token=? AND t0=?",
                                  (r["chain"], r["token"], int(r["ts"])))
            ret = None
            if c:
                px0, path = fair_entry(json.loads(c["data"]), float(r["ts"]))
                if len(path) >= 8 and px0 > 0 and 0.1 < px0 / float(r["price"] or 0 or 1) < 10:
                    path = dead_tail(path, float(r["ts"]), now, False)
                    m = simulate(px0 * 1.01, float(r["ts"]), path, dict(rules), None, 100, cost)
                    ret = m - 1 if m < 200 else -9.0
            await db.execute(f"UPDATE {table} SET sim_ret=? WHERE id=?", (ret, r["id"]))
            n += ret is not None
            if n % 200 == 0:
                await asyncio.sleep(0)
    await db.kv_set("fair_sim_1112", True)
    log.info("影子单 / 影子采样按公平入场重算了 %d 条", n)
    return n


class SkipSim:
    def __init__(self, app: "App"):
        self.app = app

    async def run(self) -> None:
        await asyncio.sleep(120)
        try:
            await recompute_fair(self.app)
        except Exception as e:  # noqa: BLE001
            log.warning("影子单重算失败: %s", e)
        await asyncio.sleep(780)
        while True:
            try:
                n = await run_batch(self.app)
                if n:
                    log.info("影子单：模拟了 %d 个被拦下的信号", n)
            except Exception as e:  # noqa: BLE001
                log.exception("影子单出错: %s", e)
            await asyncio.sleep(600)
