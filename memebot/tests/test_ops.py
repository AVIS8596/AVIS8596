"""运维：每日维护、TG 优先级队列（不连网）。"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.db import DB  # noqa: E402


def test_maintenance(tmp_path):
    import main

    async def go():
        db = DB(tmp_path / "data" / "memebot.db")
        await db.open()
        await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                         (time.time() - 40 * 86400, "solana", "W", "T", "buy", "old"))
        await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                         (time.time(), "solana", "W", "T", "buy", "new"))
        cfg = SimpleNamespace(get=lambda k, d=None: str(tmp_path / "data" / "memebot.db") if k == "general.db_path" else d)
        app = SimpleNamespace(db=db, cfg=cfg, tz=ZoneInfo("Asia/Phnom_Penh"))
        await main.maintenance(app)
        await main.maintenance(app)                      # 同一天第二次不重复备份
        rows = await db.fetchall("SELECT tx FROM signals")
        assert [r["tx"] for r in rows] == ["new"]
        assert len(list((tmp_path / "data" / "backup").glob("memebot-*.db"))) == 1
        await db.close()

    asyncio.run(go())


def test_tg_priority_and_mute():
    from bot.notify.telegram import Telegram
    tg = Telegram.__new__(Telegram)
    tg.q = asyncio.PriorityQueue(maxsize=500)
    tg._seq, tg._low_pending, tg.muted = 0, 0, False

    async def go():
        for i in range(40):
            await tg.send(f"alert{i}", low=True)
        await tg.send("TRADE")
        assert tg._low_pending == 30                     # 提醒积压到 30 条就不再收
        first = await tg.q.get()
        assert first[2] == "TRADE"                        # 交易消息插队
        tg.muted = True
        await tg.send("muted alert", low=True)
        await tg.send("TRADE2")
        assert tg.q.qsize() == 31

    asyncio.run(go())
