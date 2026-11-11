"""向高手学习：画像计算（不连网）。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.learn import Learner, build_positions, profile_from, suggestions  # noqa: E402

T0 = 1_800_000_000


def _acts(n=8, age_sec=120):
    acts, holds = [], []
    for i in range(n):
        tok = f"T{i}"
        created = T0 + i * 10000
        buy_ts = created + age_sec        # 默认发币后 2 分钟买
        win = i % 4 != 0                  # 75% 赚钱
        sell_px = 2e-5 if win else 0.7e-5
        acts.append({"event_type": "buy", "token": {"address": tok, "symbol": f"S{i}"}, "price_usd": "1e-5",
                     "cost_usd": "100", "timestamp": buy_ts, "launchpad_platform": "Pump.fun"})
        acts.append({"event_type": "sell", "token": {"address": tok}, "price_usd": str(sell_px * 0.8),
                     "cost_usd": "60", "timestamp": buy_ts + 600})
        acts.append({"event_type": "sell", "token": {"address": tok}, "price_usd": str(sell_px),
                     "cost_usd": "80", "timestamp": buy_ts + 1200})
        holds.append({"token": {"token_address": tok, "creation_timestamp": created, "total_supply": "1000000000"},
                      "history_bought_cost": "100", "history_sold_income": "200" if win else "70",
                      "total_profit_pnl": "1.0" if win else "-0.25", "balance": "0", "usd_value": "0",
                      "end_holding_at": buy_ts + 1200})
    return acts, holds


def test_profile():
    acts, holds = _acts()
    pos = build_positions(acts, holds)
    assert len(pos) == 8 and abs(pos[0]["entry_age_min"] - 2) < 0.01 and abs(pos[0]["entry_mcap"] - 10000) < 1
    pr = profile_from(pos)
    assert pr["n"] == 8 and abs(pr["win_rate"] - 0.75) < 1e-9
    assert pr["entry_age_min"] == 2 and pr["hold_min"] == 20 and pr["early_pct"] == 1.0
    assert pr["loss_cut"] == -25 and pr["win_gain"] == 100 and pr["style"].startswith("狙击型")
    cfg = SimpleNamespace(get=lambda k, d=None: {"exit.stop_loss_pct": -35,
                                                  "exit.take_profit": [{"gain_pct": 100}]}.get(k, d))
    # 狙击型学不来：不拿来总结规律
    assert "能学的画像只有 0 个" in suggestions([pr, pr, pr], cfg)[0]
    good = profile_from(build_positions(*_acts(age_sec=1800)))     # 发币 30 分钟后买、拿 20 分钟
    assert good["style"].startswith("早期型")
    tips = suggestions([good, good, good, pr], cfg)
    assert "排除 狙击 1 个" in tips[0] and any("止损" in t for t in tips) and any("+100%" in t for t in tips)


def test_learn_flow(tmp_path):
    from bot.db import DB
    acts, holds = _acts(age_sec=1800)

    class GM:
        async def wallet_activity(self, chain, w, limit=50, cursor=None):
            return {"activities": acts, "next": None}

        async def wallet_holdings(self, chain, w, limit=50, cursor=None):
            return {"list": holds, "next": None}

        async def wallet_stats(self, chain, w, period="30d"):
            return {"realized_profit": "25000", "pnl_stat": {"winrate": 0.7}, "common": {"twitter_username": "ace"}}

        async def kol(self, chain, n):
            return [{"maker": "W1"}, {"maker": "W2"}]

        async def smartmoney(self, chain, n):
            return [{"maker": "W2"}, {"maker": "W3"}]

        async def wallet_profits(self, chain, ws, period):
            return [{"wallet_address": w, "realized_profit": str(1000 * (i + 1))} for i, w in enumerate(ws)]

    async def go():
        db = DB(tmp_path / "l.db")
        await db.open()
        evaluated = []

        async def evaluate_many(chain, addrs):
            evaluated.append(dict(addrs))
            return 0

        async def wallet(chain, a):
            return None
        db.wallet = wallet
        app = SimpleNamespace(gmgn=GM(), db=db, cfg=SimpleNamespace(get=lambda k, d=None: d, enabled_chains=["solana"]),
                              smart=SimpleNamespace(evaluate_many=evaluate_many))
        ln = Learner(app)
        try:
            txt = await ln.learn("solana", "WALLETX", "")
            assert "@ace" in txt and "入场" in txt and "只学习" in txt
            n = await ln.auto("solana", top=2)
            assert n == 2 and set(evaluated[-1]) == {"W3", "W2"}       # 30 天利润最高的 2 个
            s = await ln.summary()
            assert "学习中的交易者 3 个" in s and "共同规律" in s
        finally:
            await db.close()

    asyncio.run(go())


def test_learn_token(tmp_path):
    from bot.db import DB
    acts, holds = _acts(age_sec=1800)
    T = 1_800_000_000

    class GM:
        async def top_traders(self, chain, token, limit, order_by="profit"):
            return [
                {"address": "POOL", "addr_type": 2, "profit": "999999"},
                {"address": "SNIPER1", "profit": "50000", "profit_change": "20", "start_holding_at": T + 2,
                 "avg_cost": "0.000002", "avg_sold": "0.00004", "sell_amount_percentage": "1",
                 "history_bought_cost": "500", "maker_token_tags": ["sniper"]},
                {"address": "HUMAN1", "profit": "8000", "profit_change": "4", "start_holding_at": T + 1800,
                 "avg_cost": "0.00005", "avg_sold": "0.00025", "sell_amount_percentage": "0.8",
                 "history_bought_cost": "2000", "tags": ["smart_degen"]},
            ]

        async def wallet_activity(self, *a, **k):
            return {"activities": acts}

        async def wallet_holdings(self, *a, **k):
            return {"list": holds}

        async def wallet_stats(self, *a, **k):
            return {"realized_profit": "9000", "pnl_stat": {"winrate": 0.66}}

    async def go():
        db = DB(tmp_path / "t.db")
        await db.open()
        ev = []

        async def evaluate_many(chain, addrs):
            ev.append(dict(addrs))

        async def overview(chain, token):
            return {"symbol": "MEME", "created_ts": T, "total_supply": 1e9, "mcap": 3e6}
        app = SimpleNamespace(gmgn=GM(), db=db, cfg=SimpleNamespace(get=lambda k, d=None: d),
                              data=SimpleNamespace(overview=overview), smart=SimpleNamespace(evaluate_many=evaluate_many))
        try:
            txt = await Learner(app).learn_token("solana", "TOKENX")
            assert "MEME 盈利前 2 名" in txt and "POOL" not in txt
            assert "1 个是狙击" in txt and "狙击" in txt.split("\n")[1]
            assert "发币后 30 分钟、市值 $50.0K 买" in txt and "卖出均价 5.0 倍" in txt and "聪明钱" in txt
            assert ev and set(ev[-1]) == {"HUMAN1"}
        finally:
            await db.close()

    asyncio.run(go())
