"""按 research/CRITERIA.md 事先确定的方法与及格线, 在 Binance 真实日线上检验唐奇安策略。

运行: python research/evaluate_donchian.py   (需能访问 data-api.binance.vision; 数据缓存在 data/)
输出: research/RESULTS.md
"""

from __future__ import annotations

import itertools
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from integrations.quantdinger import mock_runtime  # noqa: E402
from quantbot.data import fetch_binance  # noqa: E402

SRC = open(os.path.join(ROOT, "integrations/quantdinger/donchian_breakout_v2.py"), encoding="utf-8").read()
TUNE = ["BTCUSDT", "ETHUSDT"]
HOLDOUT = ["BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
COMMISSION, SLIPPAGE = 0.001, 0.0005
GRID = [
    {"entry_period": e, "exit_period": x, "stop_atr": s}
    for e, x, s in itertools.product([10, 20, 30, 40, 55], [5, 10, 20], [0.0, 2.0, 3.0])
]
DEFAULT = {"entry_period": 20, "exit_period": 10, "stop_atr": 2.0}
NO_HALT = {"max_drawdown_halt": 0.0}
PPY = 365  # 加密货币全年交易
TRAIN_DAYS, TEST_DAYS, WARMUP_DAYS = 730, 365, 100


def load(symbol: str) -> pd.DataFrame:
    return fetch_binance(symbol, "1d", start="2017-01-01", cache_dir=os.path.join(ROOT, "data"))


def _run(job):
    symbol, params, cost_mult = job
    df = load(symbol)
    res = mock_runtime.run(
        SRC, df, {**params, **NO_HALT}, commission=COMMISSION * cost_mult, slippage=SLIPPAGE * cost_mult
    )
    trades = sum(1 for f in res["fills"] if f["qty"] > 0)
    return job, res["equity"].pct_change().fillna(0.0), trades


def stats(rets: pd.Series) -> dict:
    rets = rets.dropna()
    eq = (1 + rets).cumprod()
    years = len(rets) / PPY
    std = rets.std(ddof=1)
    return {
        "return": float(eq.iloc[-1] - 1),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0,
        "sharpe": float(rets.mean() / std * math.sqrt(PPY)) if std > 0 else 0.0,
        "mdd": float((1 - eq / eq.cummax()).max()),
    }


def walk_forward(curves: dict, index: pd.DatetimeIndex) -> tuple[pd.Series, list]:
    """curves: {grid_idx: daily returns}. 训练 2 年选夏普最高, 检验随后 1 年, 拼接样本外收益。"""
    oos, picks = [], []
    start = WARMUP_DAYS
    while start + TRAIN_DAYS < len(index):
        tr = slice(start, start + TRAIN_DAYS)
        te = slice(start + TRAIN_DAYS, min(start + TRAIN_DAYS + TEST_DAYS, len(index)))
        best = max(curves, key=lambda k: stats(curves[k].iloc[tr])["sharpe"])
        oos.append(curves[best].iloc[te])
        picks.append((index[te.start].date(), index[te.stop - 1].date(), GRID[best]))
        start += TEST_DAYS
    return pd.concat(oos), picks


def drawdown_halt_date(symbol: str) -> str:
    df = load(symbol)
    res = mock_runtime.run(SRC, df, {}, commission=COMMISSION, slippage=SLIPPAGE)
    halts = [f["time"] for f in res["fills"] if f["reason"] == "drawdown_halt_exit"]
    if res["state"].halted:
        return str(halts[0].date()) if halts else "已触发 (当时空仓)"
    return "未触发"


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def main() -> None:
    for s in TUNE + HOLDOUT:
        load(s)  # 先下载并缓存, 子进程直接读缓存
    jobs = [(s, g, m) for s in TUNE for g in GRID for m in (1, 2)]
    jobs += [(s, DEFAULT, 1) for s in HOLDOUT]
    with ProcessPoolExecutor() as pool:
        results = list(pool.map(_run, jobs, chunksize=4))
    curve = {}
    ntrades = {}
    for (s, p, m), rets, n in results:
        key = (s, GRID.index(p), m)
        curve[key] = rets
        ntrades[key] = n

    out = ["# 唐奇安策略真实数据检验结果", ""]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out += [f"生成时间: {now}。方法与及格线见 [CRITERIA.md](CRITERIA.md) (在看结果前提交)。", ""]
    out += ["数据 (Binance 现货日线):", ""]
    for s in TUNE + HOLDOUT:
        df = load(s)
        out.append(f"- {s}: {df.index[0].date()} ~ {df.index[-1].date()}, {len(df)} 根")
    out.append("")

    verdict = {}
    di = GRID.index(DEFAULT)

    # ---- C1 / C2 / C4: walk-forward ----
    out += ["## 滚动窗口样本外结果 (C1、C2、C4)", ""]
    out += ["| 币种 | 区间 | 策略总收益 | 策略年化 | 策略夏普 | 策略最大回撤 | 买入持有总收益 | 买入持有夏普 | 买入持有最大回撤 | 成本翻倍后策略夏普 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    c1 = c2 = c4 = True
    picks_txt = []
    for s in TUNE:
        df = load(s)
        curves1 = {i: curve[(s, i, 1)] for i in range(len(GRID))}
        curves2 = {i: curve[(s, i, 2)] for i in range(len(GRID))}
        oos, picks = walk_forward(curves1, df.index)
        oos2, _ = walk_forward(curves2, df.index)
        bh = df["close"].pct_change().fillna(0.0).loc[oos.index]
        so, sb, so2 = stats(oos), stats(bh), stats(oos2)
        out.append(
            f"| {s} | {oos.index[0].date()} ~ {oos.index[-1].date()} | {pct(so['return'])} | {pct(so['cagr'])} | "
            f"{so['sharpe']:.2f} | {so['mdd'] * 100:.1f}% | {pct(sb['return'])} | {sb['sharpe']:.2f} | "
            f"{sb['mdd'] * 100:.1f}% | {so2['sharpe']:.2f} |"
        )
        c1 &= so["return"] > 0 and so["sharpe"] >= 0.8 * sb["sharpe"]
        c2 &= so["mdd"] <= 0.5 * sb["mdd"]
        c4 &= so2["sharpe"] > 0
        picks_txt.append(f"- {s}: " + "; ".join(
            f"{a}~{b}: {p['entry_period']}/{p['exit_period']}/{p['stop_atr']:g}" for a, b, p in picks))
    verdict["C1"], verdict["C2"], verdict["C4"] = c1, c2, c4
    out += ["", "每个检验年份选中的参数 (entry/exit/stop_atr):", ""] + picks_txt + [""]

    # ---- C3: full-period default + grid robustness ----
    out += ["## 全期参数敏感性 (C3)", ""]
    out += ["| 币种 | 默认参数夏普 | 默认参数总收益 | 默认参数最大回撤 | 开仓次数 | 夏普>0 的组合占比 | 全期买入持有夏普 |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    c3 = True
    for s in TUNE:
        df = load(s)
        sd = stats(curve[(s, di, 1)])
        frac = np.mean([stats(curve[(s, i, 1)])["sharpe"] > 0 for i in range(len(GRID))])
        sb = stats(df["close"].pct_change().fillna(0.0))
        out.append(f"| {s} | {sd['sharpe']:.2f} | {pct(sd['return'])} | {sd['mdd'] * 100:.1f}% | "
                   f"{ntrades[(s, di, 1)]} | {frac * 100:.0f}% | {sb['sharpe']:.2f} |")
        c3 &= sd["sharpe"] > 0 and frac >= 0.7
    verdict["C3"] = c3
    out.append("")

    # ---- C5: held-out assets ----
    out += ["## 未参与调参的币种, 默认参数全期 (C5)", ""]
    out += ["| 币种 | 策略夏普 | 策略总收益 | 策略最大回撤 | 开仓次数 | 买入持有总收益 | 买入持有夏普 | 买入持有最大回撤 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    positive = 0
    for s in HOLDOUT:
        df = load(s)
        sd = stats(curve[(s, di, 1)])
        sb = stats(df["close"].pct_change().fillna(0.0))
        positive += sd["sharpe"] > 0
        out.append(f"| {s} | {sd['sharpe']:.2f} | {pct(sd['return'])} | {sd['mdd'] * 100:.1f}% | "
                   f"{ntrades[(s, di, 1)]} | {pct(sb['return'])} | {sb['sharpe']:.2f} | {sb['mdd'] * 100:.1f}% |")
    verdict["C5"] = positive >= 3
    out += ["", f"夏普 > 0 的币种: {positive} / {len(HOLDOUT)}", ""]

    # ---- drawdown halt ----
    out += ["## 回撤熔断 (默认 30%) 会在何时触发", ""]
    for s in TUNE + HOLDOUT:
        out.append(f"- {s}: {drawdown_halt_date(s)}")
    out.append("")

    # ---- verdict ----
    names = {
        "C1": "样本外收益 > 0 且夏普 ≥ 0.8 × 买入持有",
        "C2": "样本外最大回撤 ≤ 买入持有的一半",
        "C3": "默认参数夏普 > 0 且 ≥70% 组合夏普 > 0",
        "C4": "成本翻倍后样本外夏普 > 0",
        "C5": "≥3/4 未参与调参的币种夏普 > 0",
    }
    out += ["## 结论", "", "| # | 标准 | 结果 |", "| --- | --- | --- |"]
    for k in sorted(names):
        out.append(f"| {k} | {names[k]} | {'通过' if verdict[k] else '**未通过**'} |")
    passed = all(verdict.values())
    out += ["", "**全部通过: 可以进入 QuantDinger 信号模式观察。**" if passed
            else "**未全部通过: 按事先约定, 不建议投入真实资金。**", ""]
    out += ["说明: 回测假设按日线开盘价成交、不计成交量限制; 滚动窗口在每年切换参数时不计额外换仓成本; "
            "历史结果不代表未来。"]

    path = os.path.join(ROOT, "research", "RESULTS.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
