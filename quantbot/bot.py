"""交易机器人主循环: 拉取已收盘 K 线 -> 生成信号 -> 风控 -> 下单。

与回测引擎保持相同的决策规则 (止损后等待信号反转再入场、回撤熔断等)。
实盘中止损按每根 K 线收盘价检查, 并以当前价格市价离场。
"""

from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from . import indicators as ind
from .broker import Broker, Fill
from .risk import RiskManager
from .strategies import Strategy

log = logging.getLogger(__name__)


class DataFeed(ABC):
    @abstractmethod
    def get_bars(self) -> pd.DataFrame:
        """返回截至目前所有已收盘的 K 线。"""

    def get_price(self, bars: pd.DataFrame) -> float:
        """当前可成交的参考价格, 默认用最新收盘价。"""
        return float(bars["close"].iloc[-1])


class ReplayFeed(DataFeed):
    """用历史数据逐根回放, 模拟实时行情 (用于演示模拟盘和测试)。"""

    def __init__(self, df: pd.DataFrame, start: int = 100) -> None:
        self.df = df
        self.pos = max(1, min(start, len(df)))

    def get_bars(self) -> pd.DataFrame:
        bars = self.df.iloc[: self.pos]
        if self.pos < len(self.df):
            self.pos += 1
        return bars

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.df)


class CcxtFeed(DataFeed):  # pragma: no cover - 需要网络
    def __init__(self, symbol: str, timeframe: str = "1h", exchange: str = "binance", limit: int = 500) -> None:
        self.symbol, self.timeframe, self.exchange, self.limit = symbol, timeframe, exchange, limit

    def get_bars(self) -> pd.DataFrame:
        from .data import fetch_ccxt

        return fetch_ccxt(self.symbol, self.timeframe, self.limit, self.exchange)


@dataclass
class BotState:
    last_bar: pd.Timestamp | None = None
    stop: float | None = None
    take: float | None = None
    stopped_side: int = 0
    current_signal: float = 0.0
    peak_equity: float = 0.0
    halted: bool = False
    fills: list[Fill] = field(default_factory=list)


class TradingBot:
    def __init__(
        self,
        strategy: Strategy,
        broker: Broker,
        feed: DataFeed,
        symbol: str,
        risk: RiskManager | None = None,
        cost_rate: float = 0.0015,
    ) -> None:
        self.strategy = strategy
        self.broker = broker
        self.feed = feed
        self.symbol = symbol
        self.risk = risk or RiskManager()
        self.cost_rate = cost_rate
        self.state = BotState()

    # ------------------------------------------------------------------
    def step(self) -> list[Fill]:
        """处理一次行情更新; 只有出现新的已收盘 K 线时才会决策。返回本次成交。"""
        bars = self.feed.get_bars()
        if len(bars) < self.strategy.warmup:
            log.info("数据不足 (%d < %d), 等待更多 K 线", len(bars), self.strategy.warmup)
            return []
        ts = bars.index[-1]
        st = self.state
        if st.last_bar is not None and ts <= st.last_bar:
            return []
        st.last_bar = ts

        price = self.feed.get_price(bars)
        close = float(bars["close"].iloc[-1])
        atr_now = float(ind.atr(bars, self.risk.config.atr_period).iloc[-1])
        qty = self.broker.get_position(self.symbol)
        side = int(math.copysign(1, qty)) if qty else 0
        fills: list[Fill] = []

        # 1. 止损 / 止盈 (按收盘价检查)
        if side:
            hit = None
            if st.stop is not None and (close - st.stop) * side <= 0:
                hit = "stop_loss"
            elif st.take is not None and (close - st.take) * side >= 0:
                hit = "take_profit"
            if hit:
                fills += self._order(-qty, price, hit, ts)
                st.stopped_side, st.stop, st.take, st.current_signal = side, None, None, 0.0
                qty, side = 0.0, 0

        # 2. 回撤熔断
        equity = self.broker.equity({self.symbol: price})
        st.peak_equity = max(st.peak_equity, equity)
        if not st.halted and self.risk.should_halt(equity, st.peak_equity):
            st.halted = True
            log.error("回撤达到 %.1f%%, 触发熔断, 平仓并停止交易", self.risk.config.max_drawdown_halt * 100)

        # 3. 信号
        desired = 0.0 if st.halted else float(self.strategy.generate_signals(bars).iloc[-1])
        desired = max(-1.0, min(1.0, desired if not math.isnan(desired) else 0.0))
        d_side = int(math.copysign(1, desired)) if desired else 0
        if st.stopped_side and d_side != st.stopped_side:
            st.stopped_side = 0
        if st.stopped_side:
            desired, d_side = 0.0, 0

        # 4. 调仓
        if d_side != side:
            if side:
                fills += self._order(-qty, price, "halt" if st.halted else "signal", ts)
                st.stop = st.take = None
            if d_side:
                target = self.risk.position_size(desired, self.broker.get_cash(), price, atr_now, self.cost_rate)
                fills += self._order(target, price, "entry", ts)
                st.stop, st.take = self.risk.exit_levels(d_side, price, atr_now)
            st.current_signal = desired
        elif d_side and desired != st.current_signal:
            equity = self.broker.equity({self.symbol: price})
            target = self.risk.position_size(desired, equity, price, atr_now, self.cost_rate)
            fills += self._order(target - qty, price, "rebalance", ts)
            st.current_signal = desired

        # 5. 移动止损
        side = d_side if self.broker.get_position(self.symbol) else 0
        if side:
            st.stop = self.risk.trail_stop(side, st.stop, close, atr_now)

        log.info(
            "%s close=%.4f signal=%.2f pos=%.6f equity=%.2f stop=%s",
            ts, close, desired, self.broker.get_position(self.symbol),
            self.broker.equity({self.symbol: price}), f"{st.stop:.4f}" if st.stop else "-",
        )
        st.fills += fills
        return fills

    def _order(self, qty: float, price: float, reason: str, ts) -> list[Fill]:
        if abs(qty) < 1e-12:
            return []
        kwargs = {"time": str(ts)} if hasattr(self.broker, "positions") else {}
        fill = self.broker.market_order(self.symbol, qty, price, reason, **kwargs)
        if fill:
            log.info("成交 %s %+.6f @ %.4f (%s)", fill.symbol, fill.qty, fill.price, reason)
            return [fill]
        return []

    def run(self, interval: float = 60.0, max_steps: int | None = None) -> None:
        """持续运行。interval 为两次轮询之间的秒数。"""
        steps = 0
        while max_steps is None or steps < max_steps:
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception:  # 网络等临时错误不应让机器人退出
                log.exception("step 失败, 稍后重试")
            steps += 1
            if isinstance(self.feed, ReplayFeed) and self.feed.exhausted:
                self.step()
                break
            if interval > 0:
                time.sleep(interval)
