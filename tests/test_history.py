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
