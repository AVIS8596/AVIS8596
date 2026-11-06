"""风险管理: 仓位计算、ATR 止损/止盈、移动止损、最大回撤熔断。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class RiskConfig:
    # 单个持仓的最大名义价值占权益比例 (1.0 = 不加杠杆满仓)
    max_position_pct: float = 1.0
    # 每笔交易愿意承担的风险占权益比例, 需配合 stop_loss_atr 使用; None 表示不按风险定仓
    risk_per_trade: float | None = None
    # 止损距离 = stop_loss_atr * ATR; None 表示不设止损
    stop_loss_atr: float | None = None
    # 止盈距离 = take_profit_atr * ATR; None 表示不设止盈
    take_profit_atr: float | None = None
    # 是否使用移动止损 (止损随价格有利方向移动)
    trailing_stop: bool = False
    # 权益相对历史最高点回撤超过该比例后全部平仓并停止交易; None 表示不启用
    max_drawdown_halt: float | None = None
    atr_period: int = 14
    # 是否允许零碎股/币 (加密货币一般允许, A 股需整手)
    allow_fractional: bool = True
    lot_size: float = 1.0

    def __post_init__(self) -> None:
        if self.max_position_pct <= 0:
            raise ValueError("max_position_pct 必须 > 0")
        if self.risk_per_trade is not None and not 0 < self.risk_per_trade < 1:
            raise ValueError("risk_per_trade 必须在 (0, 1) 之间")
        if self.risk_per_trade is not None and self.stop_loss_atr is None:
            raise ValueError("使用 risk_per_trade 时必须设置 stop_loss_atr")
        if self.max_drawdown_halt is not None and not 0 < self.max_drawdown_halt < 1:
            raise ValueError("max_drawdown_halt 必须在 (0, 1) 之间")


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def position_size(
        self, signal: float, equity: float, price: float, atr: float | None, cost_rate: float = 0.0
    ) -> float:
        """根据信号强度、权益、价格和波动率计算目标持仓数量 (带符号)。"""
        cfg = self.config
        if signal == 0 or equity <= 0 or price <= 0:
            return 0.0
        notional = equity * cfg.max_position_pct * min(abs(signal), 1.0)
        qty = notional / (price * (1.0 + cost_rate))
        if cfg.risk_per_trade is not None and cfg.stop_loss_atr and atr and atr > 0 and not math.isnan(atr):
            stop_dist = cfg.stop_loss_atr * atr
            qty = min(qty, equity * cfg.risk_per_trade / stop_dist)
        qty = self._round(qty)
        return math.copysign(qty, signal)

    def _round(self, qty: float) -> float:
        if self.config.allow_fractional:
            return max(qty, 0.0)
        lot = self.config.lot_size
        return max(math.floor(qty / lot) * lot, 0.0)

    def exit_levels(self, side: int, entry_price: float, atr: float | None) -> tuple[float | None, float | None]:
        """返回 (止损价, 止盈价)。side: 1 多头, -1 空头。"""
        cfg = self.config
        if atr is None or math.isnan(atr) or atr <= 0:
            return None, None
        stop = entry_price - side * cfg.stop_loss_atr * atr if cfg.stop_loss_atr else None
        take = entry_price + side * cfg.take_profit_atr * atr if cfg.take_profit_atr else None
        return stop, take

    def trail_stop(self, side: int, current_stop: float | None, close: float, atr: float | None) -> float | None:
        cfg = self.config
        if not cfg.trailing_stop or not cfg.stop_loss_atr or current_stop is None:
            return current_stop
        if atr is None or math.isnan(atr) or atr <= 0:
            return current_stop
        candidate = close - side * cfg.stop_loss_atr * atr
        return max(current_stop, candidate) if side > 0 else min(current_stop, candidate)

    def should_halt(self, equity: float, peak_equity: float) -> bool:
        limit = self.config.max_drawdown_halt
        if limit is None or peak_equity <= 0:
            return False
        return (peak_equity - equity) / peak_equity >= limit
