"""钻石手打法：认出钻石手、学出手范围和卖法、用同范围的币学"会不会翻倍"、实时下单。"""
import asyncio
import json
import random
import time
from types import SimpleNamespace

from bot.diamond import PREFIX, Diamond, base_x, is_diamond, learn_exit, learn_gate, train
from bot.db import DB


class Cfg:
    def __init__(self, d, chains=("solana",)):
        self.d, self.enabled_chains, self.dry_run = d, list(chains), True

    def get(self, k, default=None):
        return self.d.get(k, default)


def test_is_diamond_and_exit():
    now = time.time()
    base = {"address": "W", "profit": 20000, "start_holding_at": now - 3 * 86400, "sell_amount_percentage": 0.3,
            "avg_cost": 0.001, "avg_sold": 0.008, "tags": []}
    d = is_diamond(base, now, 5000, 6)
    assert d and d["holding"] == 1 and abs(d["hold_h"] - 72) < 0.1 and abs(d["sold_x"] - 8) < 1e-9
    assert is_diamond({**base, "tags": ["sniper"]}, now, 5000, 6) is None              # 开盘狙击：不算
    assert is_diamond({**base, "profit": 1000}, now, 5000, 6) is None                  # 赚得不够多
    assert is_diamond({**base, "sell_amount_percentage": 1, "end_holding_at": now - 3 * 86400 + 600}, now, 5000, 6) is None  # 10 分钟就卖完
    rows = [{"hold_h": h} for h in (10, 20, 30, 50, 80, 100, 120, 150, 200, 300)]
    ext = learn_exit(rows, -50)
    assert ext["max_hold_hours"] == 150 and ext["runner"]["enabled"] and ext["stop_loss_pct"] == -50
    assert ext["take_profit"][0] == {"gain_pct": 100, "sell_pct": 50}


def test_train_and_realtime(tmp_path):
    rnd = random.Random(3)
    ds = [{"x": {"age_min": rnd.uniform(30, 600), "mcap": rnd.uniform(5e4, 5e5), "chg60": rnd.uniform(-0.2, 1)}}
          for _ in range(60)]
    gate = learn_gate(ds)
    assert set(gate) == {"age_min", "mcap", "chg60"}
    assert base_x({"age_hours": 2, "change_1h": 50, "smart_buyers": ["a", "b"], "has_x": True})["chg60"] == 0.5
    # 机器人见过的币：持有人 ≥ 300、有推特的翻倍（+150%），其余归零（-100%）
    now = time.time()
    base = []
    for i in range(1500):
        h, x = rnd.randint(20, 1000), rnd.random() < 0.5
        good = h >= 300 and x
        base.append({"t": now - 9 * 86400 + i * 300, "ret": 1.5 if good else -1.0,
                     "x": {"age_min": rnd.uniform(40, 500), "mcap": rnd.uniform(6e4, 4e5), "chg60": 0.3,
                           "holders": h, "has_x": int(x)}})
    ent = train(base, gate, 400, 20)
    assert ent["ok"] and ent["auc"] > 0.8 and ent["pick_avg"] > 0.5, {k: v for k, v in ent.items() if k != "model"}
    assert not train(base[:100], gate, 400, 20)["ok"]

    async def go():
        db = DB(tmp_path / "d.db")
        await db.open()
        try:
            bought = []

            async def confirm_and_buy(chain, token, rep, reason, wallets=None, tier=None):
                bought.append((token, reason))
            app = SimpleNamespace(db=db, cfg=Cfg({}), hub=SimpleNamespace(confirm_and_buy=confirm_and_buy, _tasks=set()),
                                  day_start=lambda: now - 3600)
            dm = Diamond(app)
            dm.m = {"solana": {"gate": gate, "entry": ent, "exit": learn_exit([{"hold_h": 50}] * 12, -50)}}
            rep = {"age_hours": 3, "mcap": 1e5, "change_1h": 30, "holders": 800, "has_x": True, "smart_buyers": []}
            await dm.on_analysis("solana", "GOOD", rep)
            await dm.on_analysis("solana", "WEAK", {**rep, "holders": 50})
            await dm.on_analysis("solana", "OLD", {**rep, "age_hours": 500})          # 不在出手范围
            await asyncio.gather(*list(app.hub._tasks))
            assert [b[0] for b in bought] == ["GOOD"] and bought[0][1].startswith(PREFIX)
            r = dm.exit_rules("solana", {"stop_loss_pct": -35, "tiers": {"x": 1}})
            assert r["stop_loss_pct"] == -50 and "tiers" not in r and "_n" not in r
            # 找钻石手：假 GMGN
            t0 = now - 2 * 86400

            async def top_traders(chain, tok, n, order_by="profit"):
                return [{"address": "D1", "profit": 30000, "start_holding_at": t0, "sell_amount_percentage": 0.2,
                         "avg_cost": 0.0001, "tags": []},
                        {"address": "S1", "profit": 50000, "start_holding_at": t0, "sell_amount_percentage": 1,
                         "end_holding_at": t0 + 60, "avg_cost": 0.0001, "tags": []}]

            async def rank(chain, iv, n):
                return [{"address": "TOK", "market_cap": 2e6}]

            async def kline(chain, tok, res, a, b):
                return [{"time": (t0 - 3600) * 1000, "open": 5e-5, "close": 5e-5, "high": 5e-5, "low": 5e-5},
                        {"time": (t0 - 120) * 1000, "open": 1e-4, "close": 1e-4, "high": 1e-4, "low": 1e-4}]

            async def overview(chain, tok):
                return {"created_ts": t0 - 7200, "mcap": 2e6, "price": 0.002, "symbol": "TOK"}
            app.gmgn = SimpleNamespace(top_traders=top_traders, rank=rank, kline=kline)
            app.data = SimpleNamespace(overview=overview)
            app.cfg = Cfg({"autopsy.call_gap_sec": 0})
            dm.cfg = app.cfg
            assert await dm.collect("solana") == 1
            row = await db.fetchone("SELECT * FROM diamond_trades")
            x = json.loads(row["x"])
            assert row["wallet"] == "D1" and abs(x["age_min"] - 120) < 1e-6 and abs(x["mcap"] - 1e5) < 1e-3
            assert abs(x["chg60"] - 1.0) < 1e-9
            assert await dm.collect("solana") == 0                                   # 已经记过的不重复
            assert "找到 1 笔钻石手" in await dm.text()
        finally:
            await db.close()
    asyncio.run(go())
