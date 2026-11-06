"""检验在趋势策略上加 "交易所挂单止损 / 止盈" 的效果 (Binance 现货日线, 14 个币种, 含成本)。

执行模型 (与实盘执行器 quantbot/live.py 一致):
    - 每天日线收盘后计算目标仓位, 按 10% 阈值规则调仓, 在下一根开盘价成交;
    - 持仓期间每天把止损单重挂在 "前一日收盘价 × (1 − 止损幅度)", 盘中最低价触及即按
      min(开盘价, 止损价) 卖出全部仓位 (跳空按开盘价); 止盈同理, 挂在 前一日收盘 × (1 + 止盈幅度);
    - 被止损/止盈后, 当天收盘重新计算, 目标仍 > 阈值就在下一根开盘重新买入。

运行: python research/evaluate_stops.py   输出: research/RESULTS_STOPS.md
注意: 止损幅度是在这份数据上比较后选出的, 结果对所选幅度略偏乐观。
"""

from __future__ import annotations

import math
import os
import statistics
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quantbot.data import fetch_binance  # noqa: E402
from quantbot.strategies import TrendVolTarget  # noqa: E402

COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LTCUSDT",
         "TRXUSDT", "ETCUSDT", "XLMUSDT", "LINKUSDT", "DOGEUSDT", "BCHUSDT", "ATOMUSDT"]
COMM, SLIP, THRESH = 0.001, 0.0005, 0.1
VARIANTS = [
    ("无止损 (现在的策略)", None, None),
    ("止损 8%", 0.08, None),
    ("止损 12%", 0.12, None),
    ("止损 15%", 0.15, None),
    ("止损 20%", 0.20, None),
    ("止损 15% + 止盈 20%", 0.15, 0.20),
    ("止损 15% + 止盈 30%", 0.15, 0.30),
]


def simulate(df: pd.DataFrame, targets: np.ndarray, stop: float | None, take: float | None) -> dict:
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    cash, qty, pending = 1.0, 0.0, None
    equity = np.empty(len(df))
    stops = takes = 0
    for i in range(len(df)):
        if pending is not None:
            eq = cash + qty * o[i]
            tv = eq * pending
            if tv > 0:
                tv /= (1 + SLIP) * (1 + COMM)
            new_qty = tv / o[i]
            d = new_qty - qty
            if abs(d) > 1e-15:
                px = o[i] * (1 + SLIP if d > 0 else 1 - SLIP)
                cash -= d * px + abs(d * px) * COMM
                qty = new_qty
            pending = None
        if qty > 0 and i > 0:
            exit_px = None
            if stop is not None and l[i] <= c[i - 1] * (1 - stop):
                exit_px = min(o[i], c[i - 1] * (1 - stop))
                stops += 1
            elif take is not None and h[i] >= c[i - 1] * (1 + take):
                exit_px = max(o[i], c[i - 1] * (1 + take))
                takes += 1
            if exit_px is not None:
                px = exit_px * (1 - SLIP)
                cash += qty * px - qty * px * COMM
                qty = 0.0
        eq = cash + qty * c[i]
        equity[i] = eq
        cur = qty * c[i] / eq if eq > 0 else 0.0
        t = targets[i]
        if t <= 0:
            if cur > 0.001:
                pending = 0.0
        elif abs(t - cur) > THRESH:
            pending = t
    rets = pd.Series(equity, index=df.index).pct_change().dropna().iloc[150:]
    eqs = (1 + rets).cumprod()
    std = rets.std()
    return {
        "sharpe": float(rets.mean() / std * math.sqrt(365)) if std > 0 else 0.0,
        "cagr": float(eqs.iloc[-1] ** (365 / len(rets)) - 1),
        "mdd": float((1 - eqs / eqs.cummax()).max()),
        "worst_day": float(rets.min()),
        "stops": stops, "takes": takes,
    }


def main() -> None:
    data = {s: fetch_binance(s, "1d", start="2017-01-01", cache_dir=os.path.join(ROOT, "data")) for s in COINS}
    res = {}
    for s, df in data.items():
        t = TrendVolTarget().generate_signals(df).to_numpy(float)
        for name, st, tp in VARIANTS:
            res[(s, name)] = simulate(df, t, st, tp)
    out = ["# 止损 / 止盈对趋势策略的影响 (Binance 现货日线, 已扣成本)", "",
           "每行是 14 个币种的中位数, 以及 BTC、ETH 的单独结果。", "",
           "| 方案 | 夏普中位数 | 年化中位数 | 最大回撤中位数 | 最差单日中位数 | BTC 夏普 / 回撤 | ETH 夏普 / 回撤 | 每币平均触发次数 |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, _, _ in VARIANTS:
        rows = [res[(s, name)] for s in COINS]
        med = lambda k: statistics.median(r[k] for r in rows)  # noqa: E731
        trig = statistics.mean(r["stops"] + r["takes"] for r in rows)
        b, e = res[("BTCUSDT", name)], res[("ETHUSDT", name)]
        out.append(f"| {name} | {med('sharpe'):.2f} | {med('cagr') * 100:+.1f}% | {med('mdd') * 100:.1f}% | "
                   f"{med('worst_day') * 100:.1f}% | {b['sharpe']:.2f} / {b['mdd'] * 100:.0f}% | "
                   f"{e['sharpe']:.2f} / {e['mdd'] * 100:.0f}% | {trig:.0f} |")
    out += ["", "说明: 止损幅度是在这份数据上比较选出的, 对所选幅度略偏乐观; 历史结果不代表未来。"]
    with open(os.path.join(ROOT, "research", "RESULTS_STOPS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
