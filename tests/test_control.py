import asyncio
import time
from types import SimpleNamespace

from bot.control import ControlLane, quiet
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
                calls.append((token, reason, wallets))
            hub = SimpleNamespace(confirm_and_buy=buy, _tasks=set())
            msgs = []

            async def send(t, low=False):
                msgs.append(t)
            app = SimpleNamespace(db=db, hub=hub, day_start=lambda: time.time() - 3600, tg=SimpleNamespace(send=send),
                                  cfg=Cfg({"control": {"max_open": 3}, "dry_run": True}))
            app.cfg.dry_run = True
            lane = ControlLane(app)
            rep = {"age_hours": 0.5}
            await lane.on_analysis("solana", "A", rep)
            await lane.on_analysis("solana", "A", rep)                    # 同一个币 6 小时内只考虑一次
            await lane.on_analysis("solana", "B", {**rep, "risks": ["貔貅"]})   # 有致命风险直接不考虑
            await asyncio.sleep(0.01)
            assert len(calls) == 1 and calls[0][1].startswith("对照仓（实验仓") and calls[0][2] == []
            assert source_of(calls[0][1]) == "对照仓" and quiet(app.cfg, calls[0][1]) and not quiet(app.cfg, "跟单")
            # 不限每天 / 每小时笔数：只看同时拿着几个。已经拿着 3 个（上限 3）→ 不买，也不记"看过"
            for i in range(3):
                await db.execute("INSERT INTO positions(chain,token,status,dry_run,reason,opened_at,cost_usd,realized_usd,"
                                 "closed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 ("solana", f"X{i}", "open", 1, calls[0][1], time.time(), 10, 0, None))
            await lane.on_analysis("solana", "C", rep)
            await asyncio.sleep(0.01)
            assert len(calls) == 1
            # 卖掉一个腾出名额：同一个币 C 再被分析到就能买
            await db.execute("UPDATE positions SET status='closed', closed_at=?, realized_usd=12 WHERE token='X0'", (time.time(),))
            await lane.on_analysis("solana", "C", rep)
            await asyncio.sleep(0.01)
            assert [c[0] for c in calls] == ["A", "C"]
            txt = await lane.summary(1)
            assert "买入 3 笔" in txt and "平仓 1 笔" in txt and "+20.0%" in txt and "2 / 3" in txt
        finally:
            await db.close()
    asyncio.run(go())
