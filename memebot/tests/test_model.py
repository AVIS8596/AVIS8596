"""自己的选币模型（不连网）。"""
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.db import DB  # noqa: E402
from bot.model import FEATS_A, FEATS_B, SelfModel, auc, binize, build, edges_of, feats_a, feats_b, fit, score  # noqa: E402
from tests.test_brain import Cfg  # noqa: E402


def _rows(n=3000, seed=0):
    random.seed(seed)
    out = []
    for i in range(n):
        x = [random.random() * 100 if random.random() > 0.05 else None for _ in FEATS_A]
        good = (x[0] or 0) > 70 and (x[5] if x[5] is not None else 100) < 50
        ret = (0.8 if random.random() < 0.5 else -0.39) if good else (0.8 if random.random() < 0.06 else -0.2)
        out.append((float(i), x, ret))
    return out


def test_bins_consistent_with_threshold():
    e = edges_of([1, 2, 3, 4, 5, 6, 7, 8], 4)
    for v in (1, 2, 2.5, 3, 8, 9):
        b = binize(v, e)
        for k in range(1, len(e) + 1):
            assert (b <= k) == (v <= e[k - 1])
    assert binize(None, e) == 0


def test_build_learns_signal_and_refuses_noise():
    r = build(_rows(), FEATS_A)
    assert r["ok"] and r["auc"] > 0.62 and r["pick_avg"] >= 0.10 and r["pick_n"] >= 15
    assert r["imp"][0][0] in ("评分", "1小时涨幅")
    random.seed(3)
    noise = [(float(i), [random.random() for _ in FEATS_A], 0.8 if random.random() < 0.1 else -0.2) for i in range(3000)]
    assert not build(noise, FEATS_A)["ok"]
    assert "样本" in build(_rows(100), FEATS_A)["why"]


def test_auc_and_fit_basic():
    assert auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    X = [[float(i)] for i in range(200)]
    y = [int(i >= 100) for i in range(200)]
    m = fit(X, y, X, y, rounds=60)
    assert score(m, [150.0]) > 0.7 > 0.3 > score(m, [20.0])
    assert 0 < score(m, [None]) < 1


def test_feature_vectors():
    v = feats_a({"score": 70, "mcap": 100000, "liquidity": 20000, "volume_1h": 50000, "buys_1h": 30, "sells_1h": 10,
                 "holders": 300, "age_hours": 2}, "robinhood", "smart_buy")
    assert len(v) == len(FEATS_A) and v[9] == 0.75 and v[10] == 0.5 and v[22] == 4 and v[23] == 1
    s0 = {"ts": 0, "price": 1, "holders": 100, "volume": 1000, "created": -600, "chain": "solana"}
    s1 = {"ts": 120, "price": 1.5, "holders": 120, "volume": 2000, "created": -600, "chain": "solana"}
    b = feats_b(s1, s0, s0)
    assert len(b) == len(FEATS_B) and abs(b[11] - 10) < 1e-9 and abs(b[13] - 50) < 1e-9 and b[14] == 2


def test_shadow_then_on_and_promotion(tmp_path):
    async def go():
        db = DB(tmp_path / "m.db")
        await db.open()
        bought = []

        async def confirm(chain, token, rep, reason, wallets=None):
            bought.append(reason)
        app = SimpleNamespace(db=db, cfg=Cfg({"learning": {"model": "shadow"}}), hub=SimpleNamespace(
            _tasks=set(), confirm_and_buy=confirm), tg=SimpleNamespace())
        sm = SelfModel(app)
        res = build(_rows(), FEATS_A)
        sm.m["a"] = json.loads(json.dumps(res))           # 存进数据库再读出来也能用
        good = {"score": 90, "change_1h": 10}
        x = [None] * len(FEATS_A)
        assert score(sm.m["a"]["model"], x) >= 0
        # 用和训练一致的特征顺序构造"好币"
        ft = dict(good)
        p = await sm.on_analysis("solana", "T1", {"symbol": "GOOD", "price": 1.0}, ft, "Pump.fun已毕业")
        assert p is not None
        picks = await db.fetchall("SELECT * FROM model_picks")
        if p >= sm.m["a"]["cut"]:
            assert len(picks) == 1 and picks[0]["acted"] == 0     # 影子模式只记录
        assert not bought
        # 影子成绩达标 → 模拟盘自动上线
        now = time.time()
        for i in range(35):
            await db.execute("INSERT INTO model_picks(ts,chain,token,symbol,model,score,price,acted,p60,p240) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)", (now - 3600 * 5, "solana", f"X{i}", "S", "a", 0.9, 1.0, 0,
                                                             1.2, 1.6 if i % 3 else 0.9))
        sm.m["a"]["trained_at"] = now - 86400
        # 旧模型两天前选的一大堆烂币（中位 -39%）不算在现在的模型头上
        for i in range(200):
            await db.execute("INSERT INTO model_picks(ts,chain,token,symbol,model,score,price,acted,p60,p240) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)", (now - 2 * 86400, "solana", f"O{i}", "S", "a", 0.9, 1.0, 0,
                                                             0.5, 0.3))
        msg = await sm.promote_check()
        assert "通过考核" in msg and await sm.mode() == "on"
        await sm._pick("a", "solana", "T9", "NEW", 0.9, 1.0, {"symbol": "NEW", "price": 1.0})
        await asyncio.sleep(0.01)
        assert bought and bought[0].startswith("模型买入")
        # 最近 30 个转亏 → 自动退回影子
        for i in range(30):
            await db.execute("INSERT INTO model_picks(ts,chain,token,symbol,model,score,price,acted,p60,p240) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)", (now - 60 + i, "solana", f"Y{i}", "S", "a", 0.9, 1.0, 1,
                                                             0.5, 0.3))
        assert "退回影子" in await sm.promote_check() and await sm.mode() == "shadow"
        assert "影子模式" in await sm.status_text()
        await db.close()
    asyncio.run(go())


def test_live_never_auto_buys(tmp_path):
    async def go():
        db = DB(tmp_path / "l.db")
        await db.open()
        bought = []

        async def confirm(*a, **k):
            bought.append(1)
        app = SimpleNamespace(db=db, cfg=Cfg({"learning": {"model": "on"}}, dry=False),
                              hub=SimpleNamespace(_tasks=set(), confirm_and_buy=confirm))
        sm = SelfModel(app)
        await sm._pick("a", "solana", "T", "S", 0.9, 1.0, {"price": 1.0})
        await asyncio.sleep(0.01)
        assert not bought and (await db.fetchone("SELECT acted FROM model_picks"))["acted"] == 0
        assert await sm.promote_check() == ""
        await db.close()
    asyncio.run(go())


def test_dataset_and_train_from_db(tmp_path):
    async def go():
        db = DB(tmp_path / "d.db")
        await db.open()
        app = SimpleNamespace(db=db, cfg=Cfg({}))
        sm = SelfModel(app)
        now = time.time()
        random.seed(5)
        for i in range(900):
            sc = random.random() * 100
            p240 = 2.0 if sc > 70 and random.random() < 0.6 else 0.8
            await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features,p15,p60,p240) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (now - 5 * 86400 + i * 300, "solana", f"T{i}", "S", "smart_buy", 1.0,
                              json.dumps({"score": sc, "change_1h": random.random() * 200}), 1.0, 1.1, p240))
        rows = await sm.dataset_a()
        assert len(rows) == 900 and len(rows[0][1]) == len(FEATS_A)
        msgs = await sm.train()
        assert "分析模型" in msgs[0] and "AUC" in msgs[0] and "早期模型：数据还不够" in msgs[1]
        sm2 = SelfModel(app)
        await sm2.load()
        assert sm2.ready("a") == sm.ready("a")
        await db.close()
    asyncio.run(go())
