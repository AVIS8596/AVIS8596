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
    assert early_signal({**CLEAN, "holder_count": 90}, deque([(NOW - 180, 60)]), NOW, EARLY_DEFAULTS) == ""


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
        await db.close()

    asyncio.run(go())
