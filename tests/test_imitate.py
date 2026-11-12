"""学来的打法：出手范围、学卖出、学买入（检验合格才用）、实时评估下单、卖出用学到的规则。"""
import asyncio
import random
import time
from types import SimpleNamespace

from bot.imitate import PREFIX, Imitator, in_gate, learn_exit, learn_gate, train_entry


class Cfg:
    def __init__(self, d, chains=("solana",)):
        self.d, self.enabled_chains, self.dry_run = d, list(chains), True

    def get(self, k, default=None):
        return self.d.get(k, default)


def sample(n=400, seed=1):
    """合成数据：1 小时内已拉 50%~150%、离高点回撤 10%~25% 的买入晚 30 秒照样赚（+40%），其余亏（-10%）。"""
    rnd = random.Random(seed)
    rows, t0 = [], time.time() - 5 * 86400
    for i in range(n):
        run_up, dd = rnd.uniform(0, 3), rnd.uniform(0, 0.5)
        good = 0.5 <= run_up <= 1.5 and 0.1 <= dd <= 0.25
        r = 0.4 if good else -0.1
        x = {"age_min": rnd.uniform(5, 300), "mcap": rnd.uniform(5e3, 2e5), "run_up": run_up, "dd": dd,
             "chg60": run_up * 0.8, "chg5": rnd.uniform(-0.1, 0.3), "holders": rnd.randint(30, 500),
             "ex_sell_gain": 0.6 if good else -0.25, "ex_first_frac": 0.5, "ex_hold_min": 40,
             "ex_peak_gain": 1.0 if good else 0.05, "ex_dd_at_sell": 0.2}
        rows.append({"t": t0 + i * 600, "own": 0.6 if good else -0.25, "x": x, "fol": {30: r, 60: r - 0.02}})
    return rows


def test_learn_gate_exit_entry():
    rows = sample()
    gate = learn_gate(rows)
    assert set(gate) == {"age_min", "mcap", "run_up", "chg60"}
    assert in_gate({"mcap": 1e9}, gate) and not in_gate({"mcap": 5e4, "age_min": None}, gate)
    ext = learn_exit(rows)
    assert ext["take_profit"][0]["gain_pct"] == 60 and ext["stop_loss_pct"] == -25
    assert ext["trailing_start_pct"] == 50 and ext["trailing_drop_pct"] == 20 and ext["runner"] == {"enabled": False}
    ent = train_entry(rows, gate, 120, 15)
    assert ent["ok"] and ent["auc"] >= 0.8 and ent["pick_avg"] > 0.1, ent
    assert not train_entry(rows[:50], gate, 120, 15)["ok"]                 # 样本不够：不用
    rnd = random.Random(100)
    bad = [{**r, "fol": {30: rnd.choice([0.4, -0.1])}} for r in rows]       # 好坏随机，和特征无关
    assert not train_entry(bad, gate, 120, 15)["ok"]                        # 分不出好坏：不用


def test_realtime_buy_and_exit_rules(tmp_path):
    from bot.db import DB

    async def go():
        db = DB(tmp_path / "i.db")
        await db.open()
        try:
            bought = []

            async def confirm_and_buy(chain, token, rep, reason, wallets=None, tier=None):
                bought.append((token, reason, tier))
            now = time.time()

            async def kline(chain, token, res, a, b):
                # 1 小时内从 1.0 拉到 2.0（+100%），最高 2.4，现在 2.0（回撤 17%）
                if token == "GOOD":
                    return [{"time": (now - 3600) * 1000, "open": 1, "close": 1, "high": 1, "low": 1},
                            {"time": (now - 900) * 1000, "open": 2.4, "close": 2.4, "high": 2.4, "low": 2.3},
                            {"time": (now - 60) * 1000, "open": 2.0, "close": 2.0, "high": 2.0, "low": 2.0}]
                return [{"time": (now - 3600) * 1000, "open": 1, "close": 1, "high": 1, "low": 1},
                        {"time": (now - 60) * 1000, "open": 3.5, "close": 3.5, "high": 3.5, "low": 3.5}]
            app = SimpleNamespace(db=db, cfg=Cfg({"exit": {"stop_loss_pct": -35, "take_profit": []}}),
                                  gmgn=SimpleNamespace(kline=kline), hub=SimpleNamespace(confirm_and_buy=confirm_and_buy),
                                  day_start=lambda: now - 3600)
            im = Imitator(app)
            rows = sample()
            gate = learn_gate(rows)
            im.m = {"solana": {"gate": gate, "entry": train_entry(rows, gate, 120, 15), "exit": learn_exit(rows),
                               "trained_at": now, "rows": len(rows)}}
            assert im.ready("solana")
            rep = {"price": 2.0, "age_hours": 1.0, "mcap": 50_000, "holders": 200, "smart_buyers": []}
            await im.on_analysis("solana", "GOOD", rep)
            await im.on_analysis("solana", "BAD", rep)
            await im.on_analysis("solana", "FAR", {**rep, "mcap": 5e9})          # 市值不在范围：不查 K 线
            await asyncio.gather(*list(im._tasks))
            assert [b[0] for b in bought] == ["GOOD"] and bought[0][1].startswith(PREFIX + "（实验仓")
            await im.on_analysis("solana", "GOOD", rep)                          # 半小时内不重复看
            assert not im._tasks
            r = im.exit_rules("solana", app.cfg.get("exit"))
            assert r["stop_loss_pct"] == -25 and r["take_profit"][0]["gain_pct"] == 60 and "_n" not in r
            # 卖出用学到的规则：涨 60% 止盈卖一半
            from bot.trade.positions import decide_exit
            pct, why = decide_exit({"entry_price": 1.0, "high_price": 1.6, "tp_stage": 0, "opened_at": now, "tier": None},
                                   1.6, r, now, None)
            assert pct == 50 and why.startswith("止盈")
        finally:
            await db.close()
    asyncio.run(go())
