"""逐 K 线事件驱动回测引擎。

执行时序 (避免未来函数):
    1. 第 t 根 K 线收盘后, 策略基于截至 t 的数据给出目标仓位信号;
    2. 在第 t+1 根 K 线开盘价成交 (加上滑点与手续费);
    3. 第 t+1 根 K 线盘中检查止损 / 止盈 (若同一根 K 线同时触发, 保守地假设先触发止损);
    4. 第 t+1 根 K 线收盘时更新移动止损、计算权益并检查回撤熔断。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind
from .metrics import compute_metrics, drawdown_series, format_metrics
from .risk import RiskManager
from .strategies import Strategy

REQUIRED_COLUMNS = ("open", "high", "low", "close")


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    commission: float = 0.001  # 手续费率 (按成交额)
    slippage: float = 0.0005  # 滑点 (按价格比例)


@dataclass
class Trade:
    side: int
    entry_time: Any
    entry_price: float
    exit_time: Any = None
    exit_price: float | None = None
    max_qty: float = 0.0
    cost: float = 0.0  # 累计开仓成交额, 用于计算收益率
    fees: float = 0.0
    pnl: float = 0.0  # 已扣除手续费的净盈亏
    exit_reason: str = ""

    @property
    def return_pct(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: list[Trade]
    metrics: dict
    strategy: str = ""
    benchmark: dict = field(default_factory=dict)

    def trades_df(self) -> pd.DataFrame:
        rows = [{**asdict(t), "return_pct": t.return_pct} for t in self.trades]
        return pd.DataFrame(rows)

    def summary(self) -> str:
        out = [f"策略: {self.strategy}", format_metrics(self.metrics)]
        if self.benchmark:
            out.append("基准 (买入持有):")
            out.append(format_metrics({k: self.benchmark[k] for k in ("total_return", "cagr", "sharpe", "max_drawdown")}))
        return "\n".join(out)

    def plot(self, path: str) -> str:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        eq = self.equity
        fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [3, 2, 1]})
        ax = axes[0]
        ax.plot(eq.index, eq["close"], color="#555", lw=1, label="price")
        for t in self.trades:
            color = "#1a9850" if t.side > 0 else "#d73027"
            ax.scatter([t.entry_time], [t.entry_price], marker="^" if t.side > 0 else "v", color=color, s=30, zorder=3)
            if t.exit_time is not None:
                ax.scatter([t.exit_time], [t.exit_price], marker="x", color="#333", s=25, zorder=3)
        ax.set_title(f"{self.strategy}")
        ax.legend(loc="upper left")
        axes[1].plot(eq.index, eq["equity"], color="#2c7bb6", label="strategy")
        bench = eq["close"] / eq["close"].iloc[0] * eq["equity"].iloc[0]
        axes[1].plot(eq.index, bench, color="#aaa", lw=1, label="buy & hold")
        axes[1].legend(loc="upper left")
        axes[1].set_ylabel("equity")
        dd = drawdown_series(eq["equity"])
        axes[2].fill_between(eq.index, dd * 100, 0, color="#d73027", alpha=0.4)
        axes[2].set_ylabel("drawdown %")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        return path


class Backtester:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskManager | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.risk = risk or RiskManager()
        self.config = config or BacktestConfig()

    # ------------------------------------------------------------------
    def run(self, df: pd.DataFrame) -> BacktestResult:
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"数据缺少列: {missing}")
        if len(df) < 2:
            raise ValueError("数据至少需要 2 根 K 线")

        signals = self.strategy.generate_signals(df).reindex(df.index).fillna(0.0).clip(-1.0, 1.0)
        atr = ind.atr(df, self.risk.config.atr_period).to_numpy()
        o, h, l, c = (df[k].to_numpy(float) for k in REQUIRED_COLUMNS)
        sig = signals.to_numpy(float)
        idx = df.index

        self._cash = float(self.config.initial_cash)
        self._qty = 0.0
        self._avg = 0.0
        self._trade: Trade | None = None
        self.trades: list[Trade] = []

        cur_signal = 0.0
        stop: float | None = None
        take: float | None = None
        stopped_side = 0
        halted = False
        peak = self._cash
        cost_rate = self.config.commission + self.config.slippage

        rows = []
        rows.append((self._cash, self._cash, 0.0, 0.0))
        for i in range(1, len(df)):
            t = idx[i]
            desired = 0.0 if halted else float(sig[i - 1])
            d_side = int(np.sign(desired))
            if stopped_side and d_side != stopped_side:
                stopped_side = 0
            if stopped_side:
                desired, d_side = 0.0, 0  # 止损后等待信号反转再入场

            # ---- 1. 开盘执行 ----
            cur_side = int(np.sign(self._qty))
            prev_atr = atr[i - 1]
            if d_side != cur_side:
                if cur_side != 0:
                    self._fill(t, -self._qty, o[i], "halt" if halted else "signal")
                    stop = take = None
                if d_side != 0:
                    equity = self._cash
                    qty = self.risk.position_size(desired, equity, o[i], prev_atr, cost_rate)
                    if qty != 0:
                        self._fill(t, qty, o[i], "")
                        stop, take = self.risk.exit_levels(d_side, self._avg, prev_atr)
                cur_signal = desired if self._qty != 0 else 0.0
            elif d_side != 0 and desired != cur_signal:
                # 同方向调整仓位 (信号强度变化)
                equity = self._cash + self._qty * o[i]
                target = self.risk.position_size(desired, equity, o[i], prev_atr, cost_rate)
                delta = target - self._qty
                if abs(delta) > 1e-12:
                    self._fill(t, delta, o[i], "rebalance")
                cur_signal = desired

            # ---- 2. 盘中止损/止盈 ----
            side = int(np.sign(self._qty))
            if side != 0:
                exit_px, reason = None, ""
                if stop is not None and ((side > 0 and l[i] <= stop) or (side < 0 and h[i] >= stop)):
                    exit_px = min(o[i], stop) if side > 0 else max(o[i], stop)
                    reason = "stop_loss"
                elif take is not None and ((side > 0 and h[i] >= take) or (side < 0 and l[i] <= take)):
                    exit_px = max(o[i], take) if side > 0 else min(o[i], take)
                    reason = "take_profit"
                if exit_px is not None:
                    self._fill(t, -self._qty, exit_px, reason)
                    stopped_side = side
                    stop = take = None
                    cur_signal = 0.0

            # ---- 3. 收盘: 移动止损、权益、熔断 ----
            side = int(np.sign(self._qty))
            if side != 0:
                stop = self.risk.trail_stop(side, stop, c[i], atr[i])
            equity = self._cash + self._qty * c[i]
            peak = max(peak, equity)
            if not halted and self.risk.should_halt(equity, peak):
                halted = True
            rows.append((equity, self._cash, self._qty, cur_signal))

        # 期末按收盘价平仓, 便于统计
        if self._qty != 0:
            self._fill(idx[-1], -self._qty, c[-1], "end_of_data", slippage=False)
            last_eq = self._cash
            rows[-1] = (last_eq, self._cash, 0.0, rows[-1][3])

        eq = pd.DataFrame(rows, index=idx, columns=["equity", "cash", "position", "signal"])
        eq.insert(0, "close", c)
        metrics = compute_metrics(eq["equity"], self.trades, exposure=eq["position"])
        bench_eq = eq["close"] / eq["close"].iloc[0] * self.config.initial_cash
        benchmark = compute_metrics(bench_eq)
        return BacktestResult(eq, self.trades, metrics, strategy=repr(self.strategy), benchmark=benchmark)

    # ------------------------------------------------------------------
    def _fill(self, time: Any, delta: float, raw_price: float, reason: str, slippage: bool = True) -> None:
        """以 raw_price 成交 delta 数量 (正数买入, 负数卖出), 不允许单次跨越零仓位。"""
        if delta == 0:
            return
        slip = self.config.slippage if slippage else 0.0
        price = raw_price * (1 + slip) if delta > 0 else raw_price * (1 - slip)
        fee = abs(delta * price) * self.config.commission
        self._cash -= delta * price + fee

        if self._qty == 0:
            self._trade = Trade(side=int(np.sign(delta)), entry_time=time, entry_price=price)
            self._avg = price
        trade = self._trade
        assert trade is not None
        trade.fees += fee
        trade.pnl -= fee

        if np.sign(delta) == trade.side:  # 加仓
            new_qty = abs(self._qty) + abs(delta)
            self._avg = (self._avg * abs(self._qty) + price * abs(delta)) / new_qty
            trade.cost += abs(delta * price)
        else:  # 减仓
            closed = min(abs(delta), abs(self._qty))
            trade.pnl += closed * (price - self._avg) * trade.side
        self._qty += delta
        trade.max_qty = max(trade.max_qty, abs(self._qty))

        if abs(self._qty) < 1e-12:
            self._qty = 0.0
            trade.exit_time = time
            trade.exit_price = price
            trade.exit_reason = reason
            self.trades.append(trade)
            self._trade = None
            self._avg = 0.0
