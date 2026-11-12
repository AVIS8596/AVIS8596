"""背景信息：大户质量、项目方历史战绩、Telegram 社区人数和增长。"""
import asyncio
import time
from collections import deque
from types import SimpleNamespace

from bot.analysis.intel import Intel, dev_stats, holder_quality


def test_holder_quality():
    hs = [{"address": "POOL", "addr_type": 2, "native_balance": "999000000000"}]
    hs += [{"address": f"W{i}", "native_balance": str(int(150e9)) if i < 3 else "1000000", "tags": ["smart_degen"] if i == 4 else [],
            "maker_token_tags": ["fresh_wallet"] if i in (5, 6) else [],
            "native_transfer": {"from_address": "FUNDER" if i < 4 else f"F{i}"}} for i in range(10)]
    q = holder_quality(hs, "solana")
    assert q == {"whale_n": 3, "smart_hold_n": 1, "fresh_top_n": 2, "same_funder_n": 4}
    assert holder_quality([], "solana") == {}


def test_dev_stats_and_intel():
    st = dev_stats([{"ath_market_cap": "2000000"}, {"market_cap": 30000}, {"usd_market_cap": 600000}])
    assert st == {"dev_tokens_n": 3, "dev_best_mcap": 2e6, "dev_good_n": 2}
    # 现在这个币不算项目方的"以前的战绩"
    assert dev_stats([{"address": "NOW", "market_cap": 2e6}, {"address": "OLD", "market_cap": 1e4}], "now") == \
        {"dev_tokens_n": 1, "dev_best_mcap": 1e4, "dev_good_n": 0}

    async def go():
        calls = {"dev": 0, "tg": 0}

        async def created_tokens(chain, w):
            calls["dev"] += 1
            return {"tokens": [{"ath_mc": 800000, "address": "OLD"}, {"ath_mc": 3e6, "address": "TOK"}]}

        async def get(svc, url, params=None, retries=0):
            calls["tg"] += 1
            return {"ok": True, "result": 1000 + calls["tg"] * 100}
        app = SimpleNamespace(gmgn=SimpleNamespace(created_tokens=created_tokens), tg=SimpleNamespace(base="https://x"),
                              http=SimpleNamespace(get=get), cfg=SimpleNamespace(get=lambda k, d=None: d))
        it = Intel(app)
        rep = {"chain": "solana", "creator": "DEV", "address": "TOK"}
        await it.enrich(rep, {"link": {"telegram": "https://t.me/goodcoin"}})
        assert rep["dev_good_n"] == 1 and rep["dev_tokens_n"] == 1 and rep["tg_members"] == 1100 and rep["tg_link"] == 1 and "tg_growth_h" not in rep
        await it.enrich(dict(rep), {"link": {"telegram": "https://t.me/goodcoin"}})
        assert calls == {"dev": 1, "tg": 1}                      # 项目方 24 小时、TG 30 分钟内用缓存
        # 半小时后再查：算出每小时增长
        it._tg["goodcoin"] = deque([(time.time() - 3600, 1000)], maxlen=50)
        out = await it.tg("t.me/goodcoin")
        assert out["tg_members"] == 1200 and abs(out["tg_growth_h"] - 200) < 1
        assert await it.tg("https://t.me/+AbCdEf123") == {}      # 私密邀请链接查不了
    asyncio.run(go())
