"""示例: 编写自定义策略并回测。运行: pip install -e . && python examples/custom_strategy.py
(或在项目根目录: PYTHONPATH=. python examples/custom_strategy.py)"""

import pandas as pd

from quantbot import Backtester, BacktestConfig, RiskConfig, RiskManager, Strategy
from quantbot import indicators as ind
from quantbot.data import generate_synthetic


class TrendWithRsiFilter(Strategy):
    """价格在长期均线上方时才做多, 并用 RSI 过滤过热入场。"""

    name = "trend_rsi"
    default_params = {"trend": 100, "rsi_period": 14, "rsi_max": 70.0}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        trend_ok = df["close"] > ind.sma(df["close"], p["trend"])
        not_hot = ind.rsi(df["close"], p["rsi_period"]) < p["rsi_max"]
        entry = trend_ok & not_hot
        exit_ = ~trend_ok
        return self._state_machine(df.index, entry, exit_)


if __name__ == "__main__":
    df = generate_synthetic(n=1500, seed=7)
    risk = RiskManager(RiskConfig(risk_per_trade=0.02, stop_loss_atr=3.0, trailing_stop=True, max_drawdown_halt=0.3))
    res = Backtester(TrendWithRsiFilter(), risk, BacktestConfig(initial_cash=100_000)).run(df)
    print(res.summary())
    print(res.trades_df().tail())
