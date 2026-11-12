"""高收益地址解剖：仓位拼接、跟单收益（晚几秒买卖、扣成本）、钱包分类、整体报告。"""
import asyncio
import time
from types import SimpleNamespace

from bot.autopsy import Autopsy, build_trades, follower_returns, price_at, wallet_summary
from bot.db import DB


class Cfg:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)


def act(side, tok, ts, px, cost):
    return {"event_type": side, "token": {"address": tok, "symbol": tok}, "timestamp": ts, "price_usd": px,
            "cost_usd": cost}


def candle(ts, px):
    return {"time": int(ts // 60 * 60) * 1000, "open": px, "close": px}


def test_build_and_follow():
    now = time.time()
    t0 = now - 3600
    acts = [act("buy", "A", t0, 1.0, 100), act("sell", "A", t0 + 600, 2.0, 100), act("sell", "A", t0 + 900, 3.0, 150),
            act("buy", "B", t0, 1.0, 100), act("sell", "B", t0 + 60, 1.1, 50),        # B 只卖了一半：不算
            act("buy", "OLD", now - 10 * 86400, 1.0, 100), act("sell", "OLD", now - 9 * 86400, 2.0, 200)]
    holds = [{"token": {"address": "A", "creation_timestamp": t0 - 1800, "total_supply": 1e6}}]
    trs = build_trades(acts, holds, now)
    assert [t["token"] for t in trs] == ["A"]
    a = trs[0]
    assert abs(a["own"] - 1.5) < 1e-9 and abs(a["age_min"] - 30) < 1e-9 and a["mcap"] == 1e6 and a["t_sell"] == t0 + 600
    # K 线：它买入那分钟 1.0，晚 30/60/120 秒分别 1.2/1.5/2.0；它第一次卖出后 30/60/120 秒都是 1.8
    ce = [candle(t0, 1.0), candle(t0 + 60, 1.5), candle(t0 + 120, 2.0)]
    ce[0]["close"] = 1.4                                                    # 第一分钟中间价 1.2
    cx = [candle(t0 + 600 + d, 1.8) for d in (0, 60, 120)]
    fol = follower_returns(a, ce, cx, 0.04)
    assert price_at(ce, t0 + 30) == 1.2
    assert abs(fol[30] - (1.8 / 1.2 - 1 - 0.04)) < 1e-9 and abs(fol[120] - (1.8 / 2.0 - 1 - 0.04)) < 1e-9
    assert follower_returns({**a, "px_buy": 50.0}, ce, cx, 0.04) is None       # 价格单位对不上：不算
    s = wallet_summary([{**a, "fol": fol}] * 6)
    assert s["n"] == 6 and s["cls"] == "跟着也能赚"
    s = wallet_summary([{**a, "fol": {30: -0.2, 60: -0.3, 120: -0.4}}] * 6)
    assert s["cls"] == "自己赚、跟不上"


def test_autopsy_run(tmp_path):
    async def go():
        db = DB(tmp_path / "a.db")
        await db.open()
        try:
            now = time.time()
            t0 = now - 7200

            async def smartmoney(chain, n):
                return [{"maker": "W1"}, {"maker": "W2"}]

            async def kol(chain, n):
                return []

            async def wallet_profits(chain, addrs, period):
                return [{"wallet_address": a, "realized_profit": "1000"} for a in addrs]

            async def wallet_activity(chain, addr, n, cursor):
                acts = []
                for i in range(6):
                    tok = f"{addr}T{i}"
                    acts += [act("buy", tok, t0 + i * 600, 1.0, 100), act("sell", tok, t0 + i * 600 + 300, 2.0, 200)]
                return {"activities": acts}

            async def wallet_holdings(chain, addr, n, cursor=None):
                return {"list": []}

            async def kline(chain, token, res, a, b):
                # W1 的币：买后一直涨到卖出（跟得上）；W2 的币：买后马上被拉高、卖时跌回（跟不上）
                i = int(token.split("T")[1])
                tb = t0 + i * 600
                up = token.startswith("W1")
                return [candle(tb, 1.0), candle(tb + 60, 1.1 if up else 1.9), candle(tb + 120, 1.2 if up else 2.0),
                        candle(tb + 300, 2.0 if up else 1.0), candle(tb + 360, 2.0 if up else 1.0),
                        candle(tb + 420, 2.0 if up else 1.0)]
            gm = SimpleNamespace(smartmoney=smartmoney, kol=kol, wallet_profits=wallet_profits,
                                 wallet_activity=wallet_activity, wallet_holdings=wallet_holdings, kline=kline)
            sent = []

            async def send(t):
                sent.append(t)
            app = SimpleNamespace(db=db, gmgn=gm, cfg=Cfg({"autopsy.call_gap_sec": 0}), tg=SimpleNamespace(send=send))
            ap = Autopsy(app)
            secs = await ap.run(["solana"], 5)
            assert "学高收益地址的买币逻辑" in secs[0] and "1. 它们买入那一刻" in secs[0]
            txt = secs[1]
            assert "2 个钱包、12 笔" in txt and "跟着也能赚 1 个" in txt and "自己赚、跟不上 1 个" in txt
            assert "W1…W1" in txt or "W1" in txt
            assert (await ap.last())[0].startswith("（上次解剖")
            r = await db.fetchone("SELECT COUNT(*) n FROM autopsy")
            assert r["n"] == 2
            import json
            x = json.loads((await db.fetchone("SELECT x FROM autopsy_trades LIMIT 1"))["x"])
            assert abs(x["ex_sell_gain"] - 1.0) < 1e-9 and x["ex_first_frac"] == 1.0 and x["ex_hold_min"] == 5
            assert "ex_peak_gain" in x and "ex_dd_at_sell" in x
            assert ap.start(["solana"], 5) and not ap.start(["solana"], 5)    # 同时只跑一个
            await ap.task
            assert sent
        finally:
            await db.close()
    asyncio.run(go())


def test_learn_buy_logic():
    """学买币逻辑：K 线还原买入那一刻的样子；赚钱的单 vs 亏钱的单；学成规则在全部币上回测。"""
    from bot.autopsy_learn import candidate_rules, kline_features, profile, separating, test_rules
    t = 1_760_000_000.0
    # 买入前 1 小时：从 1.0 涨到 2.0，最高 2.5；买入前 5 分钟 1.8 → 2.0
    cs = [{"time": (t - 3600) * 1000, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 100},
          {"time": (t - 1200) * 1000, "open": 2.4, "close": 2.4, "high": 2.5, "low": 2.3, "volume": 100},
          {"time": (t - 300) * 1000, "open": 1.8, "close": 1.8, "high": 1.9, "low": 1.8, "volume": 500},
          {"time": (t - 60) * 1000, "open": 2.0, "close": 2.0, "high": 2.0, "low": 2.0, "volume": 700}]
    x = kline_features(cs, t, t - 1800, 1e6, 2.0)
    assert x["age_min"] == 30 and x["mcap"] == 2e6
    assert abs(x["chg5"] - (2.0 / 1.8 - 1)) < 1e-9 and abs(x["chg60"] - 1.0) < 1e-9
    assert abs(x["dd"] - 0.2) < 1e-9 and abs(x["run_up"] - 1.0) < 1e-9 and x["vol5"] == 1200
    # 40 笔：发币 10~30 分钟买的赚（+80%），2 小时以上买的亏（-40%）
    trs = [{"own": 0.8, "x": {"age_min": 10 + i, "mcap": 50_000, "holders": 200 + i}} for i in range(20)] + \
          [{"own": -0.4, "x": {"age_min": 200 + i, "mcap": 60_000, "holders": 210 + i}} for i in range(20)]
    prof = "\n".join(profile(trs))
    assert "赚 30% 以上 20 笔、亏钱 20 笔" in prof and "发币多久" in prof
    sep = separating(trs)
    assert sep and sep[0].startswith("  发币多久")                      # 最能分好坏的是发币多久
    rules = candidate_rules(trs)
    age = next(r for r in rules if r[0] == "age_min")
    assert 10 <= age[2] <= age[3] <= 29
    # 全部币：发币 10~30 分钟的之后 +50%，其余 -20%
    base = [{"x": {"age_min": 15.0}, "ret": 0.5} for _ in range(60)] + [{"x": {"age_min": 300.0}, "ret": -0.2}
                                                                         for _ in range(200)]
    out = "\n".join(test_rules([age], base))
    assert "全部 260 个币" in out and "✅ 发币多久" in out
    assert "太少" in test_rules([age], base[:50])[0]
