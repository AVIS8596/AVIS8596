import asyncio
import time
from types import SimpleNamespace

from bot.control import ControlLane
from bot.db import DB
from bot.trade.sizing import source_of


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_control_lane(tmp_path):
    async def go():
        db = DB(tmp_path / "c.db")
        await db.open()
        calls = []
        try:
            async def buy(chain, token, rep, reason, wallets=None, tier=None):
                calls.append(reason)
            hub = SimpleNamespace(confirm_and_buy=buy, _tasks=set())
            app = SimpleNamespace(db=db, hub=hub, day_start=lambda: time.time() - 3600,
                                  cfg=Cfg({"control": {"sample_rate": 1.0, "max_per_hour": 2}}))
            lane = ControlLane(app)
            rep = {"age_hours": 0.5}
            await lane.on_analysis("solana", "A", rep)
            await lane.on_analysis("solana", "A", rep)                    # 同一个币 6 小时内只考虑一次
            await lane.on_analysis("solana", "B", {**rep, "risks": ["貔貅"]})   # 有致命风险直接不考虑
            await asyncio.sleep(0)
            assert len(calls) == 1 and calls[0].startswith("对照仓（实验仓")
            assert source_of(calls[0]) == "对照仓"
            for i in range(2):                                           # 这一小时已经买了 2 笔：满了
                await db.execute("INSERT INTO positions(chain,token,status,dry_run,reason,opened_at) VALUES(?,?,?,?,?,?)",
                                 ("solana", f"X{i}", "open", 1, calls[0], time.time()))
            await lane.on_analysis("solana", "C", rep)
            await asyncio.sleep(0)
            assert len(calls) == 1
        finally:
            await db.close()
    asyncio.run(go())
