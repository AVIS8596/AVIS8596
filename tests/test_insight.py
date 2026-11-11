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
    rows.append({"price": 1.0, "p60": -1, "features": "{}"})          # 没取到价格：单独算"无价"，不参与涨跌
    res = analyze_rows(rows, "p60")
    assert res["n"] == 301 and abs(res["all"]["gone"] - 1 / 301) < 1e-9
    assert all(r <= 3 for r in [res["all"]["mean"]])
    sc = dict(res["features"]["代币评分"])
    assert sc["≥70"]["med"] > sc["＜40"]["med"] and sc["≥70"]["hit"] > sc["＜40"]["hit"]
    assert res["bought"]["med"] > res["all"]["med"]                   # 买过的比全部好
    assert analyze_rows([], "p60") == {"n": 0}


def test_early_report_separates_winners_and_scams(tmp_path):
    """早期快照：赢家（之后 3 倍以上）和骗局（之后取不到价格 / 跌到 3 成以下）刚被发现时的数据分档对比。"""
    import asyncio
    import time
    from types import SimpleNamespace

    from bot.db import DB
    from bot.trade.early_report import early_report, outcome

    assert outcome(1.0, 3.5, 0.8) == "win" and outcome(1.0, -1, -1) == "dead" and outcome(1.0, 1.2, 0.2) == "dead"
    assert outcome(1.0, 1.2, None) == "mid" and outcome(1.0, None, None) == "" and outcome(1.0, 0.9, -1) == "dead"

    async def go():
        db = DB(tmp_path / "e.db")
        await db.open()
        try:
            now = time.time() - 6 * 3600
            random.seed(3)
            for i in range(200):
                good = i % 2 == 0                         # 规律：赢家刚发现时捆绑低、10 分钟持有人涨得快
                tk = f"T{i}"
                t0 = now + i * 60
                for k, (h, px) in enumerate(((100, 1.0), (180 if good else 105, 1.3 if good else 0.9))):
                    await db.execute("INSERT INTO early_obs(ts,chain,token,price,holders,volume,rug,top10,bundler,insider,"
                                     "creator,wash,smart,kol,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (t0 + k * 600, "solana", tk, px, h, 20000, 0.05, 0.2,
                                      0.03 if good else 0.4, 0.01, 0.01, 0, 1, 0, t0 - 600))
                await db.execute("INSERT INTO early_tok(chain,token,first_ts,p60,p240) VALUES(?,?,?,?,?)",
                                 ("solana", tk, t0, 4.0 if good else -1, 2.0 if good else -1))
            txt = await early_report(SimpleNamespace(db=db))
            assert "200 个币" in txt and "赢家 50%" in txt
            assert "捆绑交易%" in txt and "10 分钟持有人增长%" in txt
            best = txt.split("最能提前认出赢家的几档")[1].split("\n")[0]
            assert "捆绑交易% ＜5" in best or "10 分钟持有人增长% ≥80" in best
            assert "候选的早期埋伏规则" in txt and "全部有 10 分钟快照的（对照）：200 个" in txt
            assert "10 分钟持有人涨 ≥80% + 老鼠仓 ＜15% + 前10 ＜40%：100 个，翻倍 100%，归零 0%" in txt
            assert "全部有 10 分钟快照的（对照）：200 个，翻倍 50%，归零 50%" in txt
        finally:
            await db.close()
    asyncio.run(go())
