"""参数优化: 网格搜索 + 样本内/样本外划分, 以及滚动窗口 (walk-forward) 验证。"""

from __future__ import annotations

import itertools
from typing import Any

import pandas as pd

from .backtest import Backtester, BacktestConfig
from .risk import RiskManager
from .strategies import STRATEGIES


def _param_combos(grid: dict[str, list[Any]]):
    keys = list(grid)
    for values in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, values))


def grid_search(
    strategy_name: str,
    grid: dict[str, list[Any]],
    df: pd.DataFrame,
    metric: str = "sharpe",
    train_frac: float = 0.7,
    risk: RiskManager | None = None,
    config: BacktestConfig | None = None,
    min_trades: int = 1,
) -> pd.DataFrame:
    """在训练集上搜索参数, 同时报告测试集 (样本外) 表现, 按训练集指标排序。"""
    cls = STRATEGIES[strategy_name]
    split = int(len(df) * train_frac)
    train, test = df.iloc[:split], df.iloc[split:]
    rows = []
    for params in _param_combos(grid):
        try:
            strat = cls(**params)
        except ValueError:
            continue  # 无效组合, 如 fast >= slow
        r_train = Backtester(strat, risk, config).run(train)
        if r_train.metrics.get("num_trades", 0) < min_trades:
            continue
        row = {**params, f"train_{metric}": r_train.metrics.get(metric, 0.0),
               "train_return": r_train.metrics["total_return"], "train_trades": r_train.metrics["num_trades"]}
        if len(test) > 2:
            # 测试集前面拼接训练集尾部用于指标预热, 只统计测试区间
            warm = df.iloc[max(0, split - strat.warmup * 2):]
            r_test = Backtester(strat, risk, config).run(warm)
            eq = r_test.equity["equity"].loc[test.index[0]:]
            from .metrics import compute_metrics

            m = compute_metrics(eq)
            row[f"test_{metric}"] = m.get(metric, 0.0)
            row["test_return"] = m.get("total_return", 0.0)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(f"train_{metric}", ascending=False).reset_index(drop=True)


def walk_forward(
    strategy_name: str,
    grid: dict[str, list[Any]],
    df: pd.DataFrame,
    n_splits: int = 4,
    train_size: int | None = None,
    metric: str = "sharpe",
    risk: RiskManager | None = None,
    config: BacktestConfig | None = None,
) -> pd.DataFrame:
    """滚动窗口优化: 每个窗口在训练段选最优参数, 然后在紧随其后的测试段检验。"""
    n = len(df)
    test_size = n // (n_splits + 1)
    train_size = train_size or test_size
    rows = []
    for k in range(n_splits):
        test_start = train_size + k * test_size
        test_end = min(test_start + test_size, n)
        if test_end - test_start < 3:
            break
        train = df.iloc[test_start - train_size : test_start]
        res = grid_search(strategy_name, grid, train, metric=metric, train_frac=1.0, risk=risk, config=config)
        if res.empty:
            continue
        best = {k2: res[k2].iloc[0] for k2 in grid}  # 按列取值以保留 int/bool 类型
        best = {k2: (v.item() if hasattr(v, "item") else v) for k2, v in best.items()}
        strat = STRATEGIES[strategy_name](**best)
        warm_start = max(0, test_start - strat.warmup * 2)
        r = Backtester(strat, risk, config).run(df.iloc[warm_start:test_end])
        from .metrics import compute_metrics

        m = compute_metrics(r.equity["equity"].loc[df.index[test_start]:])
        rows.append({"window": k, "test_start": df.index[test_start], "test_end": df.index[test_end - 1],
                     **best, f"test_{metric}": m.get(metric, 0.0), "test_return": m.get("total_return", 0.0)})
    return pd.DataFrame(rows)
