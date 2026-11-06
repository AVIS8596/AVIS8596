"""绩效指标。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from .backtest import Trade


def periods_per_year(index: pd.Index) -> float:
    """根据时间索引推断每年的 K 线数量 (用于年化)。"""
    if isinstance(index, pd.DatetimeIndex) and len(index) > 1:
        years = (index[-1] - index[0]).total_seconds() / (365.25 * 86400)
        if years > 0:
            return (len(index) - 1) / years
    return 252.0


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤 (正数, 如 0.25 表示 25%)。"""
    peak = equity.cummax()
    dd = (peak - equity) / peak
    return float(dd.max()) if len(dd) else 0.0


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def compute_metrics(equity: pd.Series, trades: Sequence["Trade"] = (), exposure: pd.Series | None = None) -> dict:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    ppy = periods_per_year(equity.index)
    rets = equity.pct_change().dropna()
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    years = (len(equity) - 1) / ppy
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 and equity.iloc[-1] > 0 else -1.0
    std = rets.std(ddof=1)
    vol = std * math.sqrt(ppy) if len(rets) > 1 else 0.0
    sharpe = rets.mean() / std * math.sqrt(ppy) if std and std > 0 else 0.0
    downside = rets[rets < 0]
    dstd = math.sqrt((downside**2).sum() / len(rets)) if len(rets) else 0.0
    sortino = rets.mean() / dstd * math.sqrt(ppy) if dstd > 0 else 0.0
    mdd = max_drawdown(equity)
    calmar = cagr / mdd if mdd > 0 else 0.0

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    out = {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "volatility": float(vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": float(mdd),
        "calmar": float(calmar),
        "num_trades": int(len(pnls)),
        "win_rate": float(len(wins) / len(pnls)) if len(pnls) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "avg_trade_return": float(np.mean([t.return_pct for t in trades])) if len(pnls) else 0.0,
        "final_equity": float(equity.iloc[-1]),
    }
    if exposure is not None and len(exposure):
        out["exposure"] = float((exposure != 0).mean())
    return out


_PCT = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate", "avg_trade_return", "exposure"}
_LABELS = {
    "total_return": "总收益率",
    "cagr": "年化收益率",
    "volatility": "年化波动率",
    "sharpe": "夏普比率",
    "sortino": "索提诺比率",
    "max_drawdown": "最大回撤",
    "calmar": "卡玛比率",
    "num_trades": "交易次数",
    "win_rate": "胜率",
    "profit_factor": "盈亏比(毛利/毛损)",
    "avg_trade_return": "平均每笔收益",
    "exposure": "持仓时间占比",
    "final_equity": "期末权益",
}


def format_metrics(m: dict) -> str:
    lines = []
    for key, label in _LABELS.items():
        if key not in m:
            continue
        v = m[key]
        if key in _PCT:
            s = f"{v * 100:8.2f}%"
        elif key == "num_trades":
            s = f"{v:8d}"
        elif key == "final_equity":
            s = f"{v:,.2f}"
        else:
            s = f"{v:8.2f}"
        lines.append(f"  {label:<14}{s}")
    return "\n".join(lines)
