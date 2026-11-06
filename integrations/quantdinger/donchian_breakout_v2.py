"""Donchian Breakout With ATR Risk
Long-only Donchian channel breakout on Crypto:BTC/USDT@spot daily bars, ported from
quantbot's DonchianBreakout strategy and RiskManager.

Signal: enter when the completed close breaks above the highest high of the prior
entry_period bars; exit when it breaks below the lowest low of the prior
exit_period bars. Orders are queued at the close and filled at the next open.

Risk: stop distance = stop_atr * ATR(atr_period). The initial stop is attached
to the entry as a native stop_loss_pct (checked intrabar by the protection
engine). When trailing is enabled, an ATR chandelier stop (close - stop_atr *
ATR, ratcheted up at each close) is evaluated on completed bars and exits at the
next open; this mirrors quantbot's ATR trailing stop, which the native
percentage trailing stop (measured from the highest high) does not reproduce.
Position weight = min(max_position_pct, risk_per_trade / stop ratio). After a
stop exit the strategy waits for the channel exit signal before re-entering.
Trading halts permanently once equity falls max_drawdown_halt below its peak.
"""

import math

import pandas as pd

# @param entry_period int 20 Breakout lookback in bars range=5:100:1
# @param exit_period int 10 Exit channel lookback in bars range=2:100:1
# @param atr_period int 14 ATR smoothing period range=5:30:1
# @param stop_atr float 2.0 Stop distance in ATR multiples, 0 disables range=0.0:6.0:0.5
# @param trailing bool true Ratchet an ATR chandelier stop at each close
# @param risk_per_trade float 0.02 Equity ratio risked per trade, 0 disables range=0.0:0.1:0.005
# @param max_position_pct float 0.95 Maximum portfolio weight range=0.1:1.0:0.05
# @param max_drawdown_halt float 0.30 Halt after this peak-to-trough drawdown, 0 disables range=0.0:0.9:0.05

PERSIST_RUNTIME_STATE = True


def initialize(context):
    g.symbol = "Crypto:BTC/USDT@spot"
    context.set_universe([g.symbol])
    context.subscribe(frequency="1d", fields=["open", "high", "low", "close", "volume"])
    context.set_warmup(250)
    context.set_benchmark(g.symbol)
    context.set_metadata(direction_mode="long_only")
    g.in_trend = False
    g.stopped = False
    g.halted = False
    g.peak_equity = 0.0
    g.trail_stop = 0.0


def wilder_atr(bars, period):
    high = bars["high"]
    low = bars["low"]
    prev_close = bars["close"].shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    true_range = ranges.max(axis=1)
    return float(true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().iloc[-1])


def handle_data(context, data):
    entry_period = int(context.params.get("entry_period", 20))
    exit_period = int(context.params.get("exit_period", 10))
    atr_period = int(context.params.get("atr_period", 14))
    stop_atr = float(context.params.get("stop_atr", 2.0))
    trailing = bool(context.params.get("trailing", True))
    risk_per_trade = float(context.params.get("risk_per_trade", 0.02))
    max_position_pct = float(context.params.get("max_position_pct", 0.95))
    max_drawdown_halt = float(context.params.get("max_drawdown_halt", 0.30))

    # ATR uses exponential smoothing, so read several periods of extra history.
    lookback = max(entry_period, exit_period, atr_period * 5) + 2
    bars = get_history(lookback, "1d", ["high", "low", "close"], g.symbol)
    if len(bars) < max(entry_period, exit_period, atr_period) + 1:
        return

    close = float(bars["close"].iloc[-1])
    entry_high = float(bars["high"].iloc[-(entry_period + 1):-1].max())
    exit_low = float(bars["low"].iloc[-(exit_period + 1):-1].min())
    position = get_position(g.symbol)
    holding = float(position.amount or 0.0) > 0

    # Channel state machine on completed bars only.
    if g.in_trend and close < exit_low:
        g.in_trend = False
    elif not g.in_trend and close > entry_high:
        g.in_trend = True

    atr = wilder_atr(bars, atr_period)
    atr_ok = stop_atr > 0 and atr > 0 and not math.isnan(atr)

    # A native protective exit blocks re-entry until the trend resets.
    exit_reason = consume_last_exit_reason(g.symbol)
    if exit_reason and g.in_trend:
        g.stopped = True
        log.info("protective exit " + exit_reason + ", waiting for channel exit")

    # ATR chandelier stop on completed bars: check the level set at the previous
    # close first, then ratchet it with the current close.
    trail_hit = False
    if holding and trailing and g.trail_stop > 0:
        if close <= g.trail_stop:
            trail_hit = True
            if g.in_trend:
                g.stopped = True
        elif atr_ok:
            g.trail_stop = max(float(g.trail_stop), close - stop_atr * atr)
    if not holding:
        g.trail_stop = 0.0
    if not g.in_trend:
        g.stopped = False

    # Drawdown circuit breaker on portfolio equity.
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

    wants_long = g.in_trend and not g.stopped and not g.halted and not trail_hit

    if wants_long and not holding:
        stop_pct = stop_atr * atr / close if atr_ok else 0.0
        weight = max_position_pct
        if risk_per_trade > 0 and stop_pct > 0:
            weight = min(weight, risk_per_trade / stop_pct)
        g.trail_stop = close - stop_atr * atr if (trailing and atr_ok) else 0.0
        order_target_percent(
            g.symbol,
            weight,
            reason="donchian_long_entry",
            stop_loss_pct=stop_pct,
        )
    elif not wants_long and holding:
        if g.halted:
            reason = "drawdown_halt_exit"
        elif trail_hit:
            reason = "atr_trailing_exit"
        else:
            reason = "donchian_long_exit"
        g.trail_stop = 0.0
        order_target_percent(g.symbol, 0.0, reason=reason)
