"""结果档案学习（不连网）。"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.trade.insight import analyze_rows  # noqa: E402


def test_analyze_rows_finds_signal():
    random.seed(1)
    rows = []
    for i in range(300):
        score = random.choice([30, 50, 65, 80])
        # 造一个规律：评分越高，之后越容易涨
        ret = random.gauss((score - 55) / 100, 0.3)
        rows.append({"price": 1.0, "p60": max(1 + ret, 0.01), "bought": int(score >= 70), "source": "x",
                     "features": json.dumps({"score": score, "smart": random.randint(0, 3), "buys_1h": 100,
                                             "sells_1h": 80, "holders": 600, "liquidity": 40000})})
    rows.append({"price": 1.0, "p60": -1, "features": "{}"})          # 没取到价格的跳过
    res = analyze_rows(rows, "p60")
    assert res["n"] == 300
    sc = dict(res["features"]["代币评分"])
    assert sc["≥70"]["med"] > sc["＜40"]["med"] and sc["≥70"]["hit"] > sc["＜40"]["hit"]
    assert res["bought"]["med"] > res["all"]["med"]                   # 买过的比全部好
    assert analyze_rows([], "p60") == {"n": 0}
