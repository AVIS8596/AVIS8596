"""/compare：几套打法同一把尺子比；/jobs：后台研究任务的额度用量和开关。"""
import asyncio
import json
import time
from types import SimpleNamespace

from bot.db import DB
from bot.trade.compare import compare, stats


class Cfg:
    dry_run = True

    def get(self, k, d=None):
        return d


def test_stats():
    st = stats([1.0, -0.5, -0.5, 0.1])
    assert st["n"] == 4 and abs(st["avg"] - 0.025) < 1e-9 and abs(st["ex_best"] - (-0.3)) < 1e-9 and st["win"] == 0.5


def test_compare(tmp_path):
    async def go():
        db = DB(tmp_path / "c.db")
        await db.open()
        try:
            now = time.time()
            for i in range(30):
                await db.execute("INSERT INTO positions(chain,token,symbol,status,dry_run,reason,cost_usd,realized_usd,opened_at) "
                                 "VALUES('solana',?,?,'closed',1,?,100,?,?)",
                                 (f"P{i}", "P", "跟单 | 一般" if i % 2 else "早期建仓（快毕业）", 60 if i % 2 else 130, now - 3600))
            for i in range(60):
                ft = {"age_hours": 0.5 if i < 30 else 50, "mcap": 20000, "change_1h": 10}
                await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features,sim_ret) "
                                 "VALUES(?,?,?,?,?,?,?,?)", (now - 7200, "solana", f"T{i}", "T", "smart_buy" if i % 2 else "早期建仓",
                                                             1.0, json.dumps(ft), 0.4 if i < 30 else -0.5))
            for i in range(25):
                await db.execute("INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price,sim_ret) "
                                 "VALUES(?,?,?,?,?,?,?,?,?)", (now - 7200, "solana", f"S{i}", "S", "跟单", "池子能被撤", "x", 1.0, 0.2))
            dm = SimpleNamespace(m={"solana": {"gate": {"age_min": [0, 120], "mcap": [1000, 1e6]}}})
            app = SimpleNamespace(db=db, cfg=Cfg(), diamond=dm)
            txt = await compare(app, 4)
            assert "跟单：15 笔，平均 -40.0%" in txt and "早期建仓：15 笔，平均 +30.0%" in txt
            assert "全部（对照组" in txt and "钻石手出手范围内：30 笔，平均 +40.0%" in txt and "范围外 -50.0%" in txt
            assert "池子能被撤：25 笔" in txt and "⚠️" in txt
            assert "✅ 有希望" in txt
        finally:
            await db.close()
    asyncio.run(go())


def test_jobs_command(tmp_path):
    from bot.notify.telegram import Telegram

    async def go():
        db = DB(tmp_path / "j.db")
        await db.open()
        try:
            started = []
            app = SimpleNamespace(db=db, background_jobs={"影子采样", "钻石手打法"}, job_tasks={},
                                  start_job=lambda n: started.append(n),
                                  gmgn=SimpleNamespace(by_job={"钻石手打法": 900, "影子采样": 100, "solana GMGN交易流": 3000}))
            tg = Telegram.__new__(Telegram)
            tg.app = app
            txt = await tg._jobs([])
            assert "▶️ 开着 钻石手打法：900" in txt and "前台" in txt and "solana GMGN交易流 3,000" in txt
            assert "已停掉" in await tg._jobs(["off", "钻石手打法"])
            assert await db.kv_get("jobs_off", []) == ["钻石手打法"]
            assert "⏸ 停着 钻石手打法" in await tg._jobs([])
            assert "没有这个后台任务" in await tg._jobs(["off", "持仓管理"])
            assert "已打开" in await tg._jobs(["on", "钻石手打法"]) and started == ["钻石手打法"]
        finally:
            await db.close()
    asyncio.run(go())
