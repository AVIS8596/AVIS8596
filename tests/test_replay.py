"""回放回测 / 期望值 / 市场冷热（不连网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.trade.positions import exit_rules, learned_rules  # noqa: E402
from bot.trade.regime import classify  # noqa: E402
from bot.trade.replay import follow_time, simulate, to_path, variants  # noqa: E402
from bot.trade.review import expectancy  # noqa: E402

EXIT = {"stop_loss_pct": -35, "take_profit": [{"gain_pct": 100, "sell_pct": 50}, {"gain_pct": 300, "sell_pct": 50}],
        "trailing_start_pct": 60, "trailing_drop_pct": 30, "max_hold_hours": 24}


def _candles(prices, t0=0):
    return [{"time": t0 + i * 300, "open": a, "high": max(a, b) * 1.01, "low": min(a, b) * 0.99, "close": b}
            for i, (a, b) in enumerate(zip(prices, prices[1:]))]


def test_simulate_paths():
    # 先冲到 2.5 倍再回落到 1.2：当前规则翻倍出本卖一半 + 移动止损；只拿满的话最后按 1.2 卖
    path = to_path(_candles([1, 1.5, 2.1, 2.5, 2.0, 1.6, 1.2, 1.2]))
    tier = exit_rules(EXIT, "good")
    cur = simulate(1.0, 0, path, dict(tier), None, 100, 0.0)
    hold = simulate(1.0, 0, path, {**tier, "stall_hours": 0, "trailing_start_pct": 10000}, None, 100, 0.0)
    assert cur > 1.4 and hold < cur
    # 一路跌：止损约 -35% 出局
    down = to_path(_candles([1, 0.9, 0.8, 0.7, 0.6, 0.5]))
    assert 0.55 < simulate(1.0, 0, down, dict(tier), None, 100, 0.0) < 0.7
    # 跟卖：在第 2 根 K 线时跟着卖
    assert abs(simulate(1.0, 0, path, dict(tier), 300, 100, 0.0) - 1.5 * 1.0) < 0.2
    # 成本：每次卖出扣 2%
    assert simulate(1.0, 0, down, dict(tier), None, 100, 0.02) < simulate(1.0, 0, down, dict(tier), None, 100, 0.0)
    assert "高手规则" in variants(EXIT) and learned_rules(EXIT)["stop_loss_pct"] == -20


def test_follow_time():
    rows = [{"wallet": "A", "side": "buy", "ts": 0}, {"wallet": "B", "side": "buy", "ts": 1},
            {"wallet": "A", "side": "sell", "ts": 100}, {"wallet": "B", "side": "sell", "ts": 200}]
    assert follow_time(rows, ["A", "B"], 10) == 200          # 2 个钱包要都卖（过半）
    assert follow_time(rows, ["A"], 10) == 100
    assert follow_time(rows, [], 10) is None


def test_expectancy_and_regime():
    rows = [{"cost_usd": 10, "realized_usd": r} for r in (30, 8, 7, 9, 6)]   # 1 笔 +200%，4 笔小亏
    ex = expectancy(rows)
    assert ex["win"] == 0.2 and abs(ex["exp"] - 0.2) < 1e-9 and ex["payoff"] > 5 and ex["pf"] >= 2
    assert classify([-0.5] * 40)["state"] == "cold"
    assert classify([0.4] * 20 + [0.0] * 20)["state"] == "hot"
    assert classify([0.1] * 10)["state"] == "unknown"


def test_run_report(tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace
    from bot.db import DB
    from bot.trade.replay import run

    async def go():
        db = DB(tmp_path / "r.db")
        await db.open()
        now = time.time()
        for i in range(6):
            await db.execute(
                "INSERT INTO positions(chain,token,symbol,status,dry_run,entry_price,cost_usd,realized_usd,opened_at,"
                "closed_at,tier,source_wallets) VALUES('solana',?,?,'closed',1,1.0,100,?,?,?,'good','[]')",
                (f"T{i}", f"S{i}", 90 if i else 300, now - 10 * 3600, now - 9 * 3600))

        async def kline(chain, token, res, t0, t1):
            up = token == "T0"
            prices = [1, 1.3, 1.8, 2.6, 3.5, 4.0] if up else [1, 0.95, 0.9, 0.85, 0.8, 0.75]
            return _candles(prices * 3, t0)
        app = SimpleNamespace(db=db, gmgn=SimpleNamespace(kline=kline),
                              cfg=SimpleNamespace(get=lambda k, d=None: {"exit": EXIT}.get(k, d), dry_run=True))
        txt = await run(app, 3)
        assert "6 笔模拟盘真实入场" in txt and "去掉最赚的 S0" in txt and "当前规则" in txt
        await db.close()

    asyncio.run(go())
