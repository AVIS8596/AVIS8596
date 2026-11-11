"""/history 时间线（不连网）。"""
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.trade.history import build  # noqa: E402


def test_timeline():
    c = 1_000_000.0
    out = [{"ts": c + 1800, "source": "Pump.fun已毕业", "price": 0.00004,
            "features": json.dumps({"score": 70, "mcap": 40000, "change_1h": 900, "smart": 1})}]
    sk = [{"ts": c + 1850, "trigger": "新币", "reason": "1 小时已涨 900%，不追高（3 个以上聪明钱一起买才追）", "price": 0.00004},
          {"ts": c + 3600, "trigger": "新币", "reason": "1 小时已涨 500%，不追高", "price": 0.0001}]
    pos = [{"id": 56, "opened_at": c + 9000, "closed_at": None, "entry_price": 0.00015, "reason": "新币高评分 <b>",
            "close_reason": None, "realized_usd": 0}]
    txt = build(ZoneInfo("UTC"), "yarl", c, 0.000003, out, sk, pos, c + 9100)
    assert "发币后 30 分钟" in txt and "13.3 倍" in txt
    assert "从第一次看到到买入：120 分钟" in txt
    assert "拦下它的规则：" in txt
    assert "&lt;b&gt;" in txt and "×50.0" in txt
    assert "还没看到过" in build(ZoneInfo("UTC"), "x", c, 0, [], [], [], c + 10)


def test_strategy_notes_in_timeline():
    from collections import deque
    from types import SimpleNamespace
    from bot.strategies import Strategies, early_status
    c = 1_000_000.0
    notes = [{"ts": c + 120, "msg": "早期建仓：持有人增长不够（3 分钟 +8%，要 ≥ 15%/3 分钟）", "price": 0.00001},
             {"ts": c + 600, "msg": "早期建仓：达标，但这 1 小时已经买了 2 笔，跳过", "price": 0}]
    txt = build(ZoneInfo("UTC"), "t", c, 0.000005, [], [], [], c + 700, notes)
    assert "🧪 早期建仓：持有人增长不够" in txt and "×2.0" in txt and "发币后 2 分钟" in txt
    # 判断类别不变不重复记
    logged = []
    st = Strategies(SimpleNamespace(cfg=SimpleNamespace(get=lambda k, d=None: d), hub=SimpleNamespace(_tasks=set()),
                                    db=SimpleNamespace(execute=None)))

    async def ex(sql, args):
        logged.append(args[3])
    st.app.db.execute = ex

    async def go():
        import asyncio
        st.note("solana", "A", {}, "早期建仓：持有人增长不够（3 分钟 +8%）")
        st.note("solana", "A", {}, "早期建仓：持有人增长不够（3 分钟 +11%）")
        st.note("solana", "A", {}, "早期建仓：结构不过关：老鼠仓 18%")
        await asyncio.sleep(0)
    import asyncio
    asyncio.run(go())
    assert len(logged) == 2
    reason, why = early_status({"holder_count": 50, "created_timestamp": c}, deque(), c + 60,
                               {"max_age_min": 180, "min_holders": 100, "min_volume": 1})
    assert not reason and why.startswith("持有人 50")
