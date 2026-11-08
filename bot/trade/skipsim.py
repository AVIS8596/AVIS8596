"""影子单：每个被风控拦下的信号，满 4 小时后拉真实 K 线，按真实的卖出规则（止损、出本、强势长拿……）完整模拟一遍，
算出"如果当时买了，按规则卖，最后赚多少"。这样每条风控规则拦对了还是拦错了都算得清楚，又不用真的去买可疑的币。
同一个币 6 小时内被拦多次只模拟第一次（其余复用结果）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .positions import exit_rules
from .replay import candles, simulate, to_path

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
                path = to_path(await candles(app, r["chain"], r["token"], float(r["ts"])))
                await asyncio.sleep(0.4)          # 后台慢慢算，别和交易抢 GMGN 额度
            except Exception as e:  # noqa: BLE001
                log.debug("影子单取 K 线失败 %s: %s", r["token"], e)
                path = []
            if len(path) >= 8:
                entry = float(r["price"]) * 1.01             # 买入滑点
                m = simulate(entry, float(r["ts"]), path, dict(exit_rules(exit_cfg, "normal")), None, 100, cost)
                ret = m - 1
            else:
                ret = -9.0           # 取不到 K 线：标记跳过（不参与统计）
        await db.execute("UPDATE skipped SET sim_ret=? WHERE id=?", (ret, r["id"]))
        done += 1
    return done


async def summary(app: "App", start: float) -> dict[str, tuple[int, float, float]]:
    """各类拦截原因的影子单结果：类别 → (笔数, 每笔平均, 中位)。"""
    rows = await app.db.fetchall("SELECT cat, sim_ret FROM skipped WHERE ts>=? AND sim_ret IS NOT NULL AND sim_ret>-5",
                                 (start,))
    g: dict[str, list[float]] = {}
    for r in rows:
        g.setdefault(r["cat"], []).append(float(r["sim_ret"]))
    return {k: (len(v), sum(v) / len(v), sorted(v)[len(v) // 2]) for k, v in g.items()}


class SkipSim:
    def __init__(self, app: "App"):
        self.app = app

    async def run(self) -> None:
        await asyncio.sleep(900)
        while True:
            try:
                n = await run_batch(self.app)
                if n:
                    log.info("影子单：模拟了 %d 个被拦下的信号", n)
            except Exception as e:  # noqa: BLE001
                log.exception("影子单出错: %s", e)
            await asyncio.sleep(600)
