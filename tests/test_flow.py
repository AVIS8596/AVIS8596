"""买卖力量打法：只排硬性的坑就买；买盘强就拿、卖盘强就走；重放对比。"""
import asyncio
import time
from types import SimpleNamespace

from bot.flow import DEFAULTS, PREFIX, FlowLane, decide, flow_from_info, flow_from_kline, replay


def test_flow_sources():
    assert flow_from_info({"price": {"buy_volume_5m": 3000, "sell_volume_5m": 1000}})["src"] == "金额"
    assert abs(flow_from_info({"price": {"buys_5m": 10, "sells_5m": 30}})["ratio"] - 10.5 / 30.5) < 1e-9
    assert flow_from_info({"price": {}}) is None
    now = time.time()
    cs = [{"time": (now - 120) * 1000, "open": 1, "close": 1.1, "volume": 300},
          {"time": (now - 60) * 1000, "open": 1.1, "close": 1.0, "volume": 900},
          {"time": (now - 900) * 1000, "open": 1, "close": 2, "volume": 99999}]          # 太早的不算
    fl = flow_from_kline(cs, now)
    assert abs(fl["ratio"] - 301 / 901) < 1e-9 and fl["vol"] == 1200


def test_decide_and_replay():
    c = dict(DEFAULTS)
    st = {}
    sell = {"ratio": 0.5, "vol": 5000}
    buy = {"ratio": 1.5, "vol": 5000}
    assert decide(st, sell, 50, 80, 20, 10, c) == (0, "")              # 第 1 次卖盘占优：先记着
    pct, why = decide(st, sell, 50, 80, 20, 11, c)                      # 连续第 2 次：赚 50%，先卖一半
    assert pct == 50 and "先卖一半" in why
    assert decide(st, sell, 30, 80, 25, 12, c) == (0, "")
    assert decide(st, buy, 30, 80, 25, 13, c) == (0, "") and st["strikes"] == 0     # 买盘回来了：清零接着拿
    decide(st, sell, 30, 80, 25, 14, c)
    pct, why = decide(st, sell, 30, 80, 25, 15, c)
    assert pct == 100 and "离场" in why
    assert decide({}, sell, 50, 80, 5, 10, c) == (0, "")               # 卖盘占优但没从高点回落：不卖
    pct, why = decide({}, {"ratio": 1, "vol": 100}, 2, 5, 3, 40, c)
    assert pct == 100 and "没人交易" in why
    # 重放：买入后涨到 3 倍一路买盘强，然后卖盘占优回落 → 在 2 倍多卖掉
    t0 = 1000.0
    obs = [(t0 + 60, 1.5, 2.0, 9000), (t0 + 120, 2.5, 2.0, 9000), (t0 + 180, 3.0, 1.8, 9000),
           (t0 + 240, 2.5, 0.5, 9000), (t0 + 300, 2.4, 0.5, 9000), (t0 + 360, 2.2, 0.5, 9000), (t0 + 420, 2.0, 0.5, 9000)]
    r = replay(1.0, t0, obs, c, cost=0)
    assert abs(r - (0.5 * 2.4 + 0.5 * 2.0 - 1)) < 1e-9
    assert replay(1.0, t0, obs[:2], c) is None
    r = replay(1.0, t0, [(t0 + 60, 0.9, 1, 9000), (t0 + 120, 0.6, 1, 9000), (t0 + 180, 0.5, 1, 9000)], c, cost=0)
    assert abs(r - (-0.4)) < 1e-9                                       # 跌破 -35% 止损


def test_lane_buy_and_exit(tmp_path):
    from bot.db import DB

    async def go():
        db = DB(tmp_path / "f.db")
        await db.open()
        try:
            now = time.time()
            bought = []

            async def confirm_and_buy(chain, token, rep, reason, wallets=None, tier=None):
                bought.append((token, reason))

            async def gm_info(chain, token, max_age=5):
                return {"price": {"buy_volume_5m": 1000, "sell_volume_5m": 4000, "volume_5m": 5000}}
            app = SimpleNamespace(db=db, cfg=SimpleNamespace(get=lambda k, d=None: d),
                                  hub=SimpleNamespace(confirm_and_buy=confirm_and_buy, _tasks=set()),
                                  data=SimpleNamespace(use_gmgn=True, _gm_info=gm_info), day_start=lambda: now - 3600)
            fl = FlowLane(app)
            await fl.on_analysis("solana", "NEW", {"age_hours": 0.5, "bundle_held_pct": 5})
            await fl.on_analysis("solana", "OLD", {"age_hours": 5, "bundle_held_pct": 5})       # 发币太久
            await fl.on_analysis("solana", "BUN", {"age_hours": 0.5, "bundle_held_pct": 35})    # 捆绑 ≥ 30% 不放开
            await fl.on_analysis("solana", "RUG", {"age_hours": 0.5, "risks": ["貔貅"]})
            await asyncio.gather(*list(app.hub._tasks))
            assert [b[0] for b in bought] == ["NEW"] and bought[0][1].startswith(PREFIX + "（实验仓")
            p = {"id": 1, "chain": "solana", "token": "NEW", "entry_price": 1.0, "high_price": 2.0, "opened_at": now - 600}
            assert await fl.exit_signal(p, 1.6, now) == (0, "")                # 第 1 次卖盘占优
            assert await fl.exit_signal(p, 1.6, now + 30) == (0, "")           # 1 分钟内不重复看
            pct, why = await fl.exit_signal(p, 1.6, now + 61)
            assert pct == 50 and "先卖一半" in why
            r = fl.exit_rules("solana", {"stop_loss_pct": -20, "tiers": {}})
            assert r["stop_loss_pct"] == -35 and r["take_profit"] == [] and "tiers" not in r
        finally:
            await db.close()
    asyncio.run(go())
