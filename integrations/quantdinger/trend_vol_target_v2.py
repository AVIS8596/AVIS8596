"""Multi-Horizon Trend With Volatility Targeting
Long-only time-series momentum on Crypto:BTC/USDT@spot daily bars.

Signal: the share of three lookbacks (short, mid and long, default 20/60/120
bars) whose completed-close return is positive, giving a trend score of 0, 1/3,
2/3 or 1. Sizing: target weight = score * min(max_weight, target_vol /
realized_vol), with realized volatility from vol_window daily log returns,
annualized with 365 days. The position is rebalanced at the next open only when
the target differs from the current weight by more than rebalance_threshold, or
when the target is zero. Parameters are fixed in advance, not optimized.
Optional permanent drawdown halt (0 disables).
"""

import math

import numpy as np

# @param short_lookback int 20 Short trend lookback in bars range=5:60:5
# @param mid_lookback int 60 Mid trend lookback in bars range=20:180:10
# @param long_lookback int 120 Long trend lookback in bars range=60:365:10
# @param vol_window int 30 Realized volatility window in bars range=10:90:5
# @param target_vol float 0.5 Annualized volatility target range=0.1:1.0:0.05
# @param max_weight float 0.95 Maximum portfolio weight range=0.1:1.0:0.05
# @param rebalance_threshold float 0.1 Minimum weight change to trade range=0.0:0.5:0.05
# @param max_drawdown_halt float 0.0 Halt after this peak-to-trough drawdown, 0 disables range=0.0:0.9:0.05

PERSIST_RUNTIME_STATE = True


def initialize(context):
    g.symbol = "Crypto:BTC/USDT@spot"
    context.set_universe([g.symbol])
    context.subscribe(frequency="1d", fields=["open", "high", "low", "close", "volume"])
    context.set_warmup(400)
    context.set_benchmark(g.symbol)
    context.set_metadata(direction_mode="long_only")
    g.halted = False
    g.peak_equity = 0.0


def handle_data(context, data):
    short_lookback = int(context.params.get("short_lookback", 20))
    mid_lookback = int(context.params.get("mid_lookback", 60))
    long_lookback = int(context.params.get("long_lookback", 120))
    vol_window = int(context.params.get("vol_window", 30))
    target_vol = float(context.params.get("target_vol", 0.5))
    max_weight = float(context.params.get("max_weight", 0.95))
    rebalance_threshold = float(context.params.get("rebalance_threshold", 0.1))
    max_drawdown_halt = float(context.params.get("max_drawdown_halt", 0.0))

    lookbacks = [short_lookback, mid_lookback, long_lookback]
    need = max(max(lookbacks), vol_window) + 1
    bars = get_history(need, "1d", "close", g.symbol)
    if len(bars) < need:
        return

    close = bars["close"]
    price = float(close.iloc[-1])
    if price <= 0:
        return

    # Trend score from completed closes only.
    votes = 0
    for lookback in lookbacks:
        past = float(close.iloc[-1 - lookback])
        if past > 0 and price > past:
            votes += 1
    score = votes / len(lookbacks)

    # Realized volatility of daily log returns, annualized with 365 days.
    log_returns = np.log(close / close.shift(1)).tail(vol_window)
    realized_vol = float(log_returns.std()) * math.sqrt(365)
    if realized_vol > 0 and not math.isnan(realized_vol):
        size = min(max_weight, target_vol / realized_vol)
    else:
        size = 0.0
    target = score * size

    # Optional permanent drawdown halt.
    equity = float(context.portfolio.total_value)
    g.peak_equity = max(float(g.peak_equity), equity)
    if (
        not g.halted
        and max_drawdown_halt > 0
        and g.peak_equity > 0
        and (g.peak_equity - equity) / g.peak_equity >= max_drawdown_halt
    ):
        g.halted = True
        log.warning("drawdown halt triggered, flattening and stopping")
    if g.halted:
        target = 0.0

    position = get_position(g.symbol)
    amount = float(position.amount or 0.0)
    current = amount * price / equity if equity > 0 else 0.0
    # Ignore dust left after rounding so it cannot trigger tiny orders.
    holding = current > 0.001

    if target <= 0:
        if holding:
            order_target_percent(
                g.symbol,
                0.0,
                reason="drawdown_halt_exit" if g.halted else "trend_exit",
            )
    elif abs(target - current) > rebalance_threshold:
        order_target_percent(
            g.symbol,
            target,
            reason="trend_rebalance" if holding else "trend_entry",
        )
