"""早期建仓 / 强叙事 策略（不连网）。"""
import asyncio
import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.strategies import (EARLY_DEFAULTS, NARRATIVE_DEFAULTS, Strategies, early_signal,  # noqa: E402
                            narrative_score, structure_problem)

NOW = 1_800_000_000.0
CLEAN = {"address": "TOK1", "symbol": "CAT", "_stage": "即将毕业", "created_timestamp": NOW - 1800,
         "holder_count": 260, "volume_1h": 45000, "swaps_1h": 600, "rug_ratio": 0.05, "top_10_holder_rate": 0.18,
         "bundler_trader_amount_rate": 0.03, "rat_trader_amount_rate": 0.02, "suspected_insider_hold_rate": 0.01,
         "creator_balance_rate": 0.0, "is_wash_trading": False, "smart_degen_count": 0, "renowned_count": 0}


def test_structure_and_early():
    assert structure_problem(CLEAN, EARLY_DEFAULTS) == ""
    assert "捆绑" in structure_problem({**CLEAN, "bundler_trader_amount_rate": 0.4}, EARLY_DEFAULTS)
    assert "刷量" in structure_problem({**CLEAN, "is_wash_trading": "true"}, EARLY_DEFAULTS)
    assert "dev" in structure_problem({**CLEAN, "creator_balance_rate": 8}, EARLY_DEFAULTS)   # 百分数也能识别
    # 3 分钟 200 → 260（+30%）→ 触发
    h = deque([(NOW - 180, 200), (NOW - 120, 215), (NOW - 60, 240)])
    why = early_signal(CLEAN, h, NOW, EARLY_DEFAULTS)
    assert why.startswith("早期建仓") and "200→260" in why
    # 观察时间不够
    assert early_signal(CLEAN, deque([(NOW - 30, 250)]), NOW, EARLY_DEFAULTS) == ""
    # 持有人没怎么涨
    assert early_signal(CLEAN, deque([(NOW - 180, 250)]), NOW, EARLY_DEFAULTS) == ""
    # 结构不干净 / 太老 / 持有人太少
    assert early_signal({**CLEAN, "top_10_holder_rate": 0.5}, h, NOW, EARLY_DEFAULTS) == ""
    assert early_signal({**CLEAN, "created_timestamp": NOW - 5 * 3600}, h, NOW, EARLY_DEFAULTS) == ""
    no_exp = {**EARLY_DEFAULTS, "exp_min_holders": 0}
    assert early_signal({**CLEAN, "holder_count": 90}, deque([(NOW - 180, 60)]), NOW, no_exp) == ""
    # 早期实验仓：持有人 50~min_holders 之间也算达标，理由里标「实验仓」；50 以下照样不要
    cfg = {**EARLY_DEFAULTS, "min_holders": 100, "exp_min_holders": 50}
    why = early_signal({**CLEAN, "holder_count": 90}, deque([(NOW - 180, 60)]), NOW, cfg)
    assert why.startswith("早期建仓") and "实验仓：持有人 90 不到 100" in why
    assert "实验仓" not in early_signal(CLEAN, h, NOW, cfg)          # 人够了就是正常仓
    from bot.strategies import early_status
    assert "实验仓 ≥ 50" in early_status({**CLEAN, "holder_count": 40}, deque([(NOW - 180, 20)]), NOW, cfg)[1]
    st = Strategies(SimpleNamespace(cfg=SimpleNamespace(
        get=lambda k, d=None: {"enabled": False} if k == "trading.experiment" else d)))
    assert st.conf("early")["exp_min_holders"] == 0                  # 实验仓总开关关了，早期也不开


def test_narrative_score():
    hot = {**CLEAN, "renowned_count": 3, "smart_degen_count": 2, "twitter": "x.com/cat", "telegram": "t.me/cat",
           "website": "cat.xyz", "x_user_follower": 25000, "volume_1h": None, "volume_24h": 300000, "holder_count": 900}
    sc, notes = narrative_score(hot, 4, True)
    assert sc >= 80 and any("KOL" in n for n in notes) and any("同名" in n for n in notes)
    sc2, _ = narrative_score(CLEAN, 1, True)
    assert sc2 < 60
    # 绑定的是大号 2 小时前的推文 → 正在发酵的热点；8 年前的旧推文不算
    from bot.strategies import TWITTER_EPOCH_MS, tweet_info
    tid = (int((NOW - 7200) * 1000) - TWITTER_EPOCH_MS) << 22
    fresh = {**CLEAN, "twitter": f"nikitabier/status/{tid}", "x_user_follower": 1_356_527}
    assert tweet_info(fresh, NOW)[0] == "nikitabier" and abs(tweet_info(fresh, NOW)[1] - 2) < 0.01
    sc3, notes3 = narrative_score(fresh, 1, True, NOW)
    assert sc3 >= 30 and "nikitabier" in notes3[0]
    old = {**CLEAN, "twitter": "elonmusk/status/1359791422711492608", "x_user_follower": 241_433_363}
    assert narrative_score(old, 1, True, NOW)[0] == 0
    # 只有 24h 成交额（GMGN 列表实际情况）也能用
    from bot.strategies import volume
    assert volume({"volume_1h": None, "volume_24h": "25115.42"}) == 25115.42


def test_poll_triggers_once_and_skips_copycats():
    fired_calls = []

    class Hub:
        _tasks = set()

    app = SimpleNamespace(cfg=SimpleNamespace(get=lambda k, d=None: d), hub=Hub())
    st = Strategies(app)

    async def fake_fire(chain, token, reason, tier):
        fired_calls.append((token, reason, tier))
    st.fire = fake_fire

    async def go():
        t0 = time.time()
        it = dict(CLEAN, created_timestamp=t0 - 600)
        st.hist[("solana", "TOK1")] = deque([(t0 - 180, 200)])
        # 第一次达标只进观察名单，不买
        assert await st.poll("solana", [it], now=t0) == []
        # 1 分钟后仍达标、持有人没减少 → 交给下一步论证
        out = await st.poll("solana", [dict(it, holder_count=275)], now=t0 + 65)
        assert out and out[0][1].startswith("早期建仓") and "论证：持续" in out[0][1] and out[0][2] == "normal"
        assert await st.poll("solana", [dict(it, holder_count=290)], now=t0 + 95) == []   # 同一个币只触发一次
        # 持有人在减少 → 热度退了，不买
        it2 = dict(CLEAN, address="TOK2", created_timestamp=t0 - 600)
        st.hist[("solana", "TOK2")] = deque([(t0 - 180, 200)])
        assert await st.poll("solana", [it2], now=t0) == []
        assert await st.poll("solana", [dict(it2, holder_count=240)], now=t0 + 65) == []
        # 强叙事：同名 3 个，只有成交量最大的领头者会被选中，观察 3 分钟
        base = {**CLEAN, "_stage": "已毕业", "symbol": "DOGEX", "created_timestamp": t0 - 3600, "renowned_count": 3,
                "smart_degen_count": 2, "twitter": "x", "x_user_follower": 20000, "holder_count": 800}
        items = [dict(base, address="A", volume_1h=200000), dict(base, address="B", volume_1h=50000),
                 dict(base, address="C", volume_1h=40000)]
        assert await st.poll("solana", items, now=t0) == []
        assert await st.poll("solana", items, now=t0 + 100) == []
        out = await st.poll("solana", items, now=t0 + 190)
        assert [o[0] for o in out] == ["A"] and out[0][1].startswith("强叙事")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert NARRATIVE_DEFAULTS["min_score"] == 60


def test_fire_requires_token_score_and_hourly_cap(tmp_path):
    from bot.db import DB

    async def go():
        db = DB(tmp_path / "s.db")
        await db.open()
        calls = {"skip": [], "buy": []}
        reps = {"LOW": {"symbol": "LOW", "score": 35}, "OK1": {"symbol": "OK1", "score": 70},
                "OK2": {"symbol": "OK2", "score": 70}, "OK3": {"symbol": "OK3", "score": 70}}

        async def analyze(chain, token, source=""):
            return reps[token]

        async def skip(chain, token, rep, trig, why):
            calls["skip"].append(why)

        async def confirm_and_buy(chain, token, rep, reason, wallets=None, tier=None):
            calls["buy"].append((token, reason))
            await db.execute("INSERT INTO positions(chain,token,symbol,status,reason,opened_at) VALUES(?,?,?,?,?,?)",
                             (chain, token, token, "open", reason, time.time()))

        app = SimpleNamespace(cfg=SimpleNamespace(get=lambda k, d=None: d), db=db, day_start=lambda: 0,
                              analyzer=SimpleNamespace(analyze=analyze), trader=SimpleNamespace(skip=skip),
                              hub=SimpleNamespace(confirm_and_buy=confirm_and_buy, _tasks=set()))
        st = Strategies(app)
        await st.fire("solana", "LOW", "强叙事（90 分）", "normal")
        assert "代币评分 35" in calls["skip"][-1] and not calls["buy"]
        for t in ("OK1", "OK2", "OK3"):
            await st.fire("solana", t, "强叙事（80 分）", "normal")
        assert [b[0] for b in calls["buy"]] == ["OK1", "OK2"]        # 每小时最多 2 笔
        assert "代币评分 70" in calls["buy"][0][1]
        # 实验仓不占正常的每小时 / 每天名额，单独每天限量
        reps.update({f"E{i}": {"symbol": f"E{i}", "score": 70} for i in range(4)})
        await db.execute("INSERT INTO positions(chain,token,symbol,status,reason,opened_at) VALUES(?,?,?,?,?,?)",
                         ("solana", "X", "X", "open", "早期建仓（…）（实验仓：持有人 60 不到 100）", time.time()))
        st.cfg = SimpleNamespace(get=lambda k, d=None: {"exp_max_per_day": 3} if k == "strategies.early" else d)
        for i in range(4):
            await st.fire("solana", f"E{i}", "早期建仓（快毕业）（实验仓：持有人 60 不到 100）", "normal")
        assert [b[0] for b in calls["buy"][2:]] == ["E0", "E1"]        # 已有 1 笔，上限 3
        assert "代币评分 70" in calls["buy"][-1][1] and "实验仓" in calls["buy"][-1][1]
        await db.close()

    asyncio.run(go())


def test_explain_lines():
    from types import SimpleNamespace as NS
    from bot.trade.trader import Trader
    tr = Trader.__new__(Trader)
    tr.cfg = NS(get=lambda k, d=None: d)
    rep = {"risks": [], "liquidity": 106000, "bundle_held_pct": 5, "insider_held_pct": 0, "top10_pct": 18.75,
           "change_1h": 1160, "age_hours": 0.4, "mcap": 223700, "volume_1h": 855000, "buys_1h": 900,
           "sells_1h": 700, "holders": 1560}
    lines = tr.explain(rep)
    assert all(ln.startswith("✅") for ln in lines), lines
    st = Strategies(NS(cfg=NS(get=lambda k, d=None: d), hub=NS(_tasks=set())))
    it = {**CLEAN, "address": "FRODO1", "symbol": "FRODO", "_stage": "已毕业", "bundler_trader_amount_rate": 0.3041,
          "renowned_count": 3, "smart_degen_count": 44, "volume_24h": 855000, "volume_1h": None, "holder_count": 1560,
          "created_timestamp": time.time() - 1300}
    st.last_items["solana"] = {"FRODO1": it}
    out = st.explain("solana", "FRODO1")
    assert any("捆绑交易 30.4%，上限 30%" in x for x in out) and any("强叙事评分" in x for x in out)
    assert "不在 GMGN" in st.explain("solana", "NOPE")[0]



def _hot(t0, **kw):
    """叙事分 ≥ 90 的强叙事币（KOL 3、聪明钱 3、社交齐全、成交火热）。"""
    return {**CLEAN, "_stage": "已毕业", "symbol": "NB", "created_timestamp": t0 - 600, "renowned_count": 3,
            "smart_degen_count": 3, "twitter": "x.com/nb", "telegram": "t.me/nb", "website": "nb.xyz",
            "x_user_follower": 25000, "holder_count": 900, "volume_1h": 300000, "swaps_1h": 1500, **kw}


def test_soft_structure_only_relaxes_surface_metrics():
    from bot.strategies import soft_structure
    from bot.trade.probe import PROBE_DEFAULTS as PB
    nc = NARRATIVE_DEFAULTS
    assert soft_structure(CLEAN, nc, PB) == ("", "")
    prob, soft = soft_structure({**CLEAN, "bundler_trader_amount_rate": 0.55}, nc, PB)      # TREMP：捆绑交易 55%
    assert prob == "" and "捆绑交易 55.0%" in soft
    prob, soft = soft_structure({**CLEAN, "top_10_holder_rate": 0.451}, nc, PB)             # 牛逼：前10 45.1%
    assert prob == "" and "前10持仓 45.1%" in soft
    assert soft_structure({**CLEAN, "bundler_trader_amount_rate": 0.75}, nc, PB) == ("捆绑交易 75.0%，上限 60%", "")
    # 直接说明是阴谋盘的不放宽：老鼠仓、刷量、跑路风险、dev 持仓
    for bad in ({"rat_trader_amount_rate": 0.3}, {"is_wash_trading": True}, {"rug_ratio": 0.5},
                {"creator_balance_rate": 0.2}):
        prob, soft = soft_structure({**CLEAN, "bundler_trader_amount_rate": 0.55, **bad}, nc, PB)
        assert prob and not soft, bad
    assert soft_structure({**CLEAN, "bundler_trader_amount_rate": 0.55}, nc, {**PB, "enabled": False})[0]


def test_narrative_probe_fast_and_tolerant():
    """强叙事 ≥ 90 分、只是捆绑交易 / 前10 超限：1 分钟后开试探仓；持有人少一两个不算热度回落；分数不够高或结构太差不买。"""
    skips = []

    async def skip(chain, token, rep, trigger, why):
        skips.append((token, why))
    app = SimpleNamespace(cfg=SimpleNamespace(get=lambda k, d=None: d), hub=SimpleNamespace(_tasks=set()),
                          trader=SimpleNamespace(skip=skip))
    st = Strategies(app)
    fired = []

    async def fire(chain, token, reason, tier):
        fired.append((token, reason))
    st.fire = fire

    async def go():
        t0 = time.time()
        items = [
            _hot(t0, address="TREMP", symbol="TREMP", bundler_trader_amount_rate=0.55),
            _hot(t0, address="NB", symbol="NB", top_10_holder_rate=0.451),
            _hot(t0, address="WORSE", symbol="WORSE", bundler_trader_amount_rate=0.8),       # 超过试探仓上限
            _hot(t0, address="RAT", symbol="RAT", bundler_trader_amount_rate=0.55, rat_trader_amount_rate=0.3),
            {**_hot(t0, address="WEAK", symbol="WEAK", bundler_trader_amount_rate=0.55),      # 叙事分 60 多，不到 80
             "renowned_count": 2, "smart_degen_count": 2, "swaps_1h": 100},
            _hot(t0, address="FADE", symbol="FADE", top_10_holder_rate=0.451),
        ]
        assert narrative_score(items[0], 1, True, t0)[0] >= 90 and 60 <= narrative_score(items[4], 1, True, t0)[0] < 80
        assert await st.poll("solana", items, now=t0) == []
        later = [dict(it) for it in items]
        later[0]["holder_count"] = 890                    # 少 1%：正常波动
        later[5]["holder_count"] = 780                    # 少 13%：热度在退
        out = await st.poll("solana", later, now=t0 + 65)
        await asyncio.sleep(0)
        assert sorted(o[0] for o in out) == ["NB", "TREMP"]
        assert all("试探仓" in o[1] for o in out) and "捆绑交易 55.0%" in dict((o[0], o[1]) for o in out)["TREMP"]
        assert {t for t, _ in skips} == {"WORSE", "RAT", "WEAK"} and all(w.startswith("论证：叙事分") for _, w in skips)
        assert "FADE" not in [o[0] for o in out] and "solana:FADE" not in st.pending
    asyncio.run(go())
