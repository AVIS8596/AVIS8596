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
            # 池子没烧的币：走"对照仓·池子没烧"，名额和普通的分开数（普通的 3 个名额已经满了也能买）
            await db.execute("UPDATE positions SET status='open', closed_at=NULL WHERE token='X0'")
            pull = {**rep, "chain": "solana", "lp_burn": "", "exchange": "raydium"}
            await lane.on_analysis("solana", "P", pull)
            await lane.on_analysis("solana", "D", rep)                    # 普通的满了：不买
            await asyncio.sleep(0.01)
            assert [c[0] for c in calls] == ["A", "C", "P"] and calls[-1][1].startswith("对照仓·池子没烧（实验仓")
            assert source_of(calls[-1][1]) == "对照仓·池子没烧" and quiet(app.cfg, calls[-1][1])
            await db.execute("INSERT INTO positions(chain,token,status,dry_run,reason,opened_at,cost_usd) VALUES(?,?,?,?,?,?,?)",
                             ("solana", "P", "open", 1, calls[-1][1], time.time(), 10))
            txt = await lane.summary(1)
            assert "<b>池子没烧</b>：最近 1 小时买入 1 笔" in txt and "1 / 20" in txt and "3 / 3" in txt
            app.cfg["control"]["pullable"] = False                         # 关掉：池子没烧的按普通对照仓走（检查时会被拦）
            await db.execute("UPDATE positions SET status='closed', closed_at=? WHERE token='X1'", (time.time(),))
            await lane.on_analysis("solana", "Q", pull)
            await asyncio.sleep(0.01)
            assert calls[-1][0] == "Q" and calls[-1][1].startswith("对照仓（实验仓")
        finally:
            await db.close()
    asyncio.run(go())


def test_post_close_review_with_quiet_lane(tmp_path):
    """平仓 4 小时后的复盘：对照仓的不推送，别的照常推送（11-13：查询里漏了 reason 列，报"持仓检查失败: 'reason'"）。"""
    from bot.trade.positions import PositionManager

    async def go():
        db = DB(tmp_path / "p.db")
        await db.open()
        msgs = []
        try:
            async def send(t, low=False):
                msgs.append(t)

            async def high(chain, token, a, b):
                return 5.0                                    # 卖出后又涨到卖出价的 5 倍 → "卖早了"
            cfg = Cfg({"control": {}})
            cfg.dry_run = True
            app = SimpleNamespace(db=db, cfg=cfg, tg=SimpleNamespace(send=send),
                                  data=SimpleNamespace(recent_high_between=high))
            now = time.time()
            for tok, reason in (("A", "对照仓（实验仓，发现就买）"), ("B", "跟单（2 个聪明钱）")):
                await db.execute(
                    "INSERT INTO positions(chain,token,symbol,status,dry_run,reason,opened_at,closed_at,cost_usd,realized_usd,"
                    "amount_raw,decimals,close_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("solana", tok, tok, "closed", 1, reason, now - 6 * 3600, now - 5 * 3600, 10, 10, "10000000", 6, "止盈"))
            await PositionManager(app).fill_post_max()
            rows = await db.fetchall("SELECT token, lesson FROM positions ORDER BY token")
            assert all("卖早了" in (r["lesson"] or "") for r in rows)          # 两笔都打了标签
            assert len(msgs) == 1 and "#2" in msgs[0]                         # 只推送了不是对照仓的那笔
        finally:
            await db.close()
    asyncio.run(go())
