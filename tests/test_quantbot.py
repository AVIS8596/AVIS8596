import math

import numpy as np
import pandas as pd
import pytest

from quantbot import indicators as ind
from quantbot.backtest import Backtester, BacktestConfig
from quantbot.bot import ReplayFeed, TradingBot
from quantbot.broker import PaperBroker
from quantbot.data import generate_synthetic, normalize
from quantbot.metrics import compute_metrics, max_drawdown
from quantbot.optimize import grid_search, walk_forward
from quantbot.risk import RiskConfig, RiskManager
from quantbot.strategies import STRATEGIES, Strategy, get_strategy

NO_COST = BacktestConfig(initial_cash=10_000, commission=0.0, slippage=0.0)


@pytest.fixture(scope="module")
def df():
    return generate_synthetic(n=600, seed=1)


def make_df(closes, opens=None, highs=None, lows=None):
    closes = np.asarray(closes, float)
    opens = closes if opens is None else np.asarray(opens, float)
    highs = np.maximum(opens, closes) if highs is None else np.asarray(highs, float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, float)
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=idx)


class Fixed(Strategy):
    name = "fixed"
    default_params = {"values": None}

    def generate_signals(self, df):
        return pd.Series(self.params["values"], index=df.index, dtype=float)


# ---------------- indicators ----------------
def test_sma_and_rsi_basic():
    s = pd.Series(np.arange(1, 21, dtype=float))
    assert ind.sma(s, 5).iloc[4] == 3.0
    assert math.isnan(ind.sma(s, 5).iloc[3])
    assert ind.rsi(s, 14).iloc[-1] == 100.0  # 只涨不跌
    down = pd.Series(np.arange(20, 0, -1, dtype=float))
    assert ind.rsi(down, 14).iloc[-1] == pytest.approx(0.0)


def test_atr_and_donchian(df):
    a = ind.atr(df, 14)
    assert (a.dropna() > 0).all()
    upper, _ = ind.donchian(df, 20)
    # 通道不包含当前 K 线
    assert upper.iloc[30] == df["high"].iloc[10:30].max()


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_strategies_no_lookahead(name, df):
    """截断未来数据不应改变历史信号。"""
    strat = get_strategy(name)
    full = strat.generate_signals(df)
    part = strat.generate_signals(df.iloc[:400])
    pd.testing.assert_series_equal(full.iloc[:400], part, check_names=False)
    assert full.between(-1, 1).all()


def test_strategy_param_validation():
    with pytest.raises(ValueError):
        get_strategy("sma_cross", fast=50, slow=20)
    with pytest.raises(ValueError):
        get_strategy("sma_cross", bogus=1)
    with pytest.raises(ValueError):
        get_strategy("nope")


# ---------------- backtest ----------------
def test_buy_and_hold_matches_price():
    d = make_df([10, 11, 12, 13, 14], opens=[10, 10, 11, 12, 13])
    res = Backtester(get_strategy("buy_hold"), config=NO_COST).run(d)
    # 第 0 根收盘产生信号, 第 1 根开盘 10 买入, 期末收盘 14 卖出
    assert res.metrics["final_equity"] == pytest.approx(10_000 * 14 / 10)
    assert len(res.trades) == 1
    assert res.trades[0].exit_reason == "end_of_data"


def test_signal_executes_next_open():
    d = make_df([10, 10, 20, 20, 20], opens=[10, 10, 15, 20, 20])
    res = Backtester(Fixed(values=[0, 1, 1, 0, 0]), config=NO_COST).run(d)
    t = res.trades[0]
    assert t.entry_time == d.index[2] and t.entry_price == 15  # 第 1 根信号 -> 第 2 根开盘
    assert t.exit_time == d.index[4] and t.exit_price == 20
    assert t.pnl == pytest.approx(10_000 / 15 * 5)


def test_costs_reduce_equity():
    d = make_df([10] * 6)
    cfg = BacktestConfig(initial_cash=10_000, commission=0.001, slippage=0.001)
    res = Backtester(Fixed(values=[1, 0, 1, 0, 0, 0]), config=cfg).run(d)
    assert len(res.trades) == 2
    assert res.metrics["final_equity"] < 10_000
    assert all(t.pnl < 0 for t in res.trades)


def test_short_position_profits_on_decline():
    d = make_df([10, 10, 8, 6, 6], opens=[10, 10, 10, 8, 6])
    res = Backtester(Fixed(values=[-1, -1, -1, 0, 0]), config=NO_COST).run(d)
    assert res.trades[0].side == -1
    assert res.trades[0].pnl == pytest.approx(10_000 / 10 * (10 - 6))


def test_stop_loss_triggers_and_blocks_reentry():
    # 恒定 ATR=2 (高低差), 2 倍 ATR 止损 => 入场 100 后止损价 96
    closes = [100] * 20 + [100, 95, 100, 100]
    opens = list(closes)
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    lows[21] = 90
    opens[21] = 100  # 开盘未跳空, 盘中跌破止损价
    d = make_df(closes, opens, highs, lows)
    sig = [0] * 19 + [1] * 5
    risk = RiskManager(RiskConfig(stop_loss_atr=2.0, atr_period=5))
    res = Backtester(Fixed(values=sig), risk, NO_COST).run(d)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == pytest.approx(96.0)
    assert res.equity["position"].iloc[-1] == 0  # 信号未反转, 不再入场


def test_risk_per_trade_sizing():
    rm = RiskManager(RiskConfig(risk_per_trade=0.01, stop_loss_atr=2.0))
    qty = rm.position_size(1.0, equity=100_000, price=100, atr=5)
    assert qty == pytest.approx(100)  # 风险 1000 / 止损距离 10
    assert rm.position_size(-0.5, 100_000, 100, None) == pytest.approx(-500)
    whole = RiskManager(RiskConfig(allow_fractional=False, lot_size=100))
    assert whole.position_size(1.0, 12_345, 10, None) == 1200


def test_drawdown_halt():
    closes = [100, 100, 80, 60, 70, 90, 100]
    d = make_df(closes)
    risk = RiskManager(RiskConfig(max_drawdown_halt=0.15))
    res = Backtester(get_strategy("buy_hold"), risk, NO_COST).run(d)
    assert res.trades[0].exit_reason == "halt"
    assert (res.equity["position"].iloc[-3:] == 0).all()


def test_equity_accounting_consistent(df):
    cfg = BacktestConfig(initial_cash=50_000)
    res = Backtester(get_strategy("ensemble"), config=cfg).run(df)
    total_pnl = sum(t.pnl for t in res.trades)
    assert res.metrics["final_equity"] == pytest.approx(50_000 + total_pnl, rel=1e-9)
    assert (res.equity["cash"] > -1e-6).all()  # 不加杠杆时现金不为负


# ---------------- metrics ----------------
def test_metrics():
    eq = pd.Series([100, 120, 90, 110], index=pd.date_range("2024-01-01", periods=4))
    assert max_drawdown(eq) == pytest.approx(0.25)
    m = compute_metrics(eq)
    assert m["total_return"] == pytest.approx(0.10)


# ---------------- data ----------------
def test_normalize_chinese_columns():
    raw = pd.DataFrame({"日期": ["2024-01-02", "2024-01-01"], "开盘": [1, 2], "最高": [2, 3], "最低": [0.5, 1], "收盘": [1.5, 2.5]})
    out = normalize(raw)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.is_monotonic_increasing


# ---------------- optimize ----------------
def test_grid_search_and_walk_forward(df):
    res = grid_search("sma_cross", {"fast": [5, 10], "slow": [20, 10]}, df)
    assert len(res) == 3  # fast=10, slow=10 无效被跳过
    assert {"train_sharpe", "test_sharpe"} <= set(res.columns)
    wf = walk_forward("sma_cross", {"fast": [5, 10], "slow": [30]}, df, n_splits=3)
    assert len(wf) == 3


# ---------------- bot ----------------
def test_paper_bot_replay_matches_direction(df, tmp_path):
    state = tmp_path / "state.json"
    broker = PaperBroker(cash=10_000, commission=0.0, slippage=0.0, state_file=str(state))
    strat = get_strategy("sma_cross", fast=10, slow=30)
    feed = ReplayFeed(df, start=40)
    bot = TradingBot(strat, broker, feed, "ASSET")
    bot.run(interval=0)
    assert len(bot.state.fills) > 0
    final_sig = strat.generate_signals(df).iloc[-1]
    assert (broker.get_position("ASSET") > 0) == (final_sig > 0)
    # 状态可从文件恢复
    restored = PaperBroker(state_file=str(state))
    assert restored.cash == pytest.approx(broker.cash)
    assert restored.get_position("ASSET") == pytest.approx(broker.get_position("ASSET"))


def test_bot_ignores_repeated_bar(df):
    class Static(ReplayFeed):
        def get_bars(self):
            return self.df.iloc[:100]

    broker = PaperBroker(cash=10_000)
    bot = TradingBot(get_strategy("buy_hold"), broker, Static(df), "X")
    assert len(bot.step()) == 1
    assert bot.step() == []
