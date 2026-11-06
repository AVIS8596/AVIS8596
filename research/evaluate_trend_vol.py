"""按 research/CRITERIA_TREND_VOL.md 事先确定的及格线, 在 Binance 真实日线上检验多周期趋势 + 波动率目标策略。

运行: python research/evaluate_trend_vol.py   (需能访问 data-api.binance.vision; 数据缓存在 data/)
输出: research/RESULTS_TREND_VOL.md
"""

from __future__ import annotations

import itertools
import math
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from integrations.quantdinger import mock_runtime  # noqa: E402
from quantbot.data import fetch_binance  # noqa: E402

SRC = open(os.path.join(ROOT, "integrations/quantdinger/trend_vol_target_v2.py"), encoding="utf-8").read()
UNSEEN = ["LTCUSDT", "TRXUSDT", "ETCUSDT", "XLMUSDT", "LINKUSDT", "DOGEUSDT", "BCHUSDT", "ATOMUSDT"]
SEEN = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
COMMISSION, SLIPPAGE = 0.001, 0.0005
EVAL_START = 150
PPY = 365
DEFAULT: dict = {}
VARIANTS = [
    {"short_lookback": 20 * k // 2, "mid_lookback": 60 * k // 2, "long_lookback": 120 * k // 2,
     "target_vol": tv, "rebalance_threshold": th}
    for k, tv, th in itertools.product([1, 2, 4], [0.3, 0.5, 0.7], [0.05, 0.1, 0.2])
]


def load(symbol: str) -> pd.DataFrame:
    return fetch_binance(symbol, "1d", start="2017-01-01", cache_dir=os.path.join(ROOT, "data"))


def _run(job):
    symbol, params, cost_mult = job
    df = load(symbol)
    res = mock_runtime.run(SRC, df, params, commission=COMMISSION * cost_mult, slippage=SLIPPAGE * cost_mult)
    eq = res["equity"].iloc[EVAL_START - 1:]
    fills = res["fills"]
    halted_on = None
    if params.get("max_drawdown_halt"):
        halts = [f["time"] for f in fills if f["reason"] == "drawdown_halt_exit"]
        halted_on = str(halts[0].date()) if halts else ("已触发 (当时空仓)" if res["state"].halted else "未触发")
    return {
        "job": job,
        "rets": eq.pct_change().dropna(),
        "weight": float(res["weight"].iloc[EVAL_START:].mean()),
        "trades": len(fills),
        "halted_on": halted_on,
    }


def bh_rets(symbol: str) -> pd.Series:
    close = load(symbol)["close"].iloc[EVAL_START - 1:]
    return close.pct_change().dropna()


def stats(rets: pd.Series) -> dict:
    eq = (1 + rets).cumprod()
    years = len(rets) / PPY
    std = rets.std(ddof=1)
    return {
        "return": float(eq.iloc[-1] - 1),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0,
        "sharpe": float(rets.mean() / std * math.sqrt(PPY)) if std > 0 else 0.0,
        "mdd": float((1 - eq / eq.cummax()).max()),
    }


def yearly(rets: pd.Series) -> pd.Series:
    return (1 + rets).groupby(rets.index.year).prod() - 1


def without_best_year(rets: pd.Series) -> float:
    y = yearly(rets)
    return float((1 + y.drop(y.idxmax())).prod() - 1)


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def main() -> None:
    for s in UNSEEN + SEEN:
        load(s)
    jobs = [(s, DEFAULT, m) for s in UNSEEN + SEEN for m in (1, 2)]
    jobs += [(s, v, 1) for s in ("BTCUSDT", "ETHUSDT") for v in VARIANTS]
    jobs += [(s, {"max_drawdown_halt": 0.35}, 1) for s in UNSEEN + SEEN]
    with ProcessPoolExecutor() as pool:
        results = list(pool.map(_run, jobs, chunksize=2))

    def get(symbol, params, mult=1):
        return next(r for r in results if r["job"] == (symbol, params, mult))

    out = ["# 多周期趋势 + 波动率目标策略: 真实数据检验结果", ""]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out += [f"生成时间: {now}。及格线见 [CRITERIA_TREND_VOL.md](CRITERIA_TREND_VOL.md) (与策略代码一起在看结果前提交)。",
            "所有数字均已扣除手续费 0.1% 与滑点 0.05% (每边)。", ""]

    header = ["| 币种 | 区间 | 策略总收益 | 策略年化 | 策略夏普 | 策略最大回撤 | 平均仓位 | 成交笔数 | "
              "买入持有总收益 | 买入持有年化 | 买入持有夏普 | 买入持有最大回撤 | 成本翻倍夏普 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    table = {}
    for group, title in ((UNSEEN, "未看过的币种 (主要检验)"), (SEEN, "上次已看过的币种")):
        out += [f"## {title}", ""] + header
        for s in group:
            r1, r2 = get(s, DEFAULT, 1), get(s, DEFAULT, 2)
            st, st2, bh = stats(r1["rets"]), stats(r2["rets"]), stats(bh_rets(s))
            table[s] = (st, st2, bh, r1)
            idx = r1["rets"].index
            out.append(
                f"| {s} | {idx[0].date()} ~ {idx[-1].date()} | {pct(st['return'])} | {pct(st['cagr'])} | "
                f"{st['sharpe']:.2f} | {st['mdd'] * 100:.1f}% | {r1['weight'] * 100:.0f}% | {r1['trades']} | "
                f"{pct(bh['return'])} | {pct(bh['cagr'])} | {bh['sharpe']:.2f} | {bh['mdd'] * 100:.1f}% | {st2['sharpe']:.2f} |"
            )
        out.append("")

    v = {}
    u = [table[s] for s in UNSEEN]
    n_pos = sum(st["sharpe"] > 0 for st, _, _, _ in u)
    n_beat = sum(st["sharpe"] > bh["sharpe"] for st, _, bh, _ in u)
    all_lower_dd = all(st["mdd"] < bh["mdd"] for st, _, bh, _ in u)
    med_dd = statistics.median(st["mdd"] for st, _, _, _ in u)
    med_bh_dd = statistics.median(bh["mdd"] for _, _, bh, _ in u)
    v["C1"] = (n_pos >= 6, f"{n_pos}/8 夏普 > 0")
    v["C2"] = (n_beat >= 6, f"{n_beat}/8 夏普 > 买入持有")
    v["C3"] = (all_lower_dd and med_dd <= 0.6 * med_bh_dd,
               f"全部回撤更低: {'是' if all_lower_dd else '否'}; 回撤中位数 {med_dd * 100:.1f}% vs 买入持有 {med_bh_dd * 100:.1f}% "
               f"(上限 {0.6 * med_bh_dd * 100:.1f}%)")
    n_seen = sum(table[s][0]["sharpe"] >= table[s][2]["sharpe"] for s in SEEN)
    cagr_ok = {s: table[s][0]["cagr"] >= 0.4 * table[s][2]["cagr"] for s in ("BTCUSDT", "ETHUSDT")}
    v["C4"] = (n_seen >= 5 and all(cagr_ok.values()),
               f"{n_seen}/6 夏普 ≥ 买入持有; " + "; ".join(
                   f"{s[:3]} 年化 {table[s][0]['cagr'] * 100:.1f}% vs 买入持有 40% = {0.4 * table[s][2]['cagr'] * 100:.1f}%"
                   for s in ("BTCUSDT", "ETHUSDT")))
    n_pos2 = sum(st2["sharpe"] > 0 for _, st2, _, _ in u)
    c5_seen = {s: table[s][1]["sharpe"] >= 0.8 * table[s][2]["sharpe"] for s in ("BTCUSDT", "ETHUSDT")}
    v["C5"] = (n_pos2 >= 6 and all(c5_seen.values()),
               f"未看过币种 {n_pos2}/8 夏普 > 0; " + "; ".join(
                   f"{s[:3]} {table[s][1]['sharpe']:.2f} vs 0.8×买入持有 {0.8 * table[s][2]['sharpe']:.2f}"
                   for s in ("BTCUSDT", "ETHUSDT")))

    out += ["## 参数敏感性 (C6): 27 组参数的夏普", ""]
    out += ["| 回看周期 | 目标波动 | 调仓阈值 | BTC 夏普 | ETH 夏普 |", "| --- | --- | --- | --- | --- |"]
    frac = {}
    for s in ("BTCUSDT", "ETHUSDT"):
        frac[s] = sum(stats(get(s, p)["rets"])["sharpe"] > 0 for p in VARIANTS) / len(VARIANTS)
    for p in VARIANTS:
        out.append(f"| {p['short_lookback']}/{p['mid_lookback']}/{p['long_lookback']} | {p['target_vol']:.0%} | "
                   f"{p['rebalance_threshold']:.0%} | {stats(get('BTCUSDT', p)['rets'])['sharpe']:.2f} | "
                   f"{stats(get('ETHUSDT', p)['rets'])['sharpe']:.2f} |")
    v["C6"] = (all(f >= 0.8 for f in frac.values()),
               "; ".join(f"{s[:3]} {f * 100:.0f}% 组合夏普 > 0" for s, f in frac.items()))
    out.append("")

    wb = {s: without_best_year(table[s][3]["rets"]) for s in UNSEEN + SEEN}
    med_wb = statistics.median(wb[s] for s in UNSEEN)
    v["C7"] = (wb["BTCUSDT"] > 0 and wb["ETHUSDT"] > 0 and med_wb > 0,
               f"BTC {pct(wb['BTCUSDT'])}; ETH {pct(wb['ETHUSDT'])}; 未看过币种中位数 {pct(med_wb)}")

    out += ["## 逐年收益 (策略 / 买入持有)", ""]
    years = sorted({y for s in UNSEEN + SEEN for y in yearly(table[s][3]["rets"]).index})
    out += ["| 币种 | " + " | ".join(str(y) for y in years) + " | 去掉最好一年后 |",
            "| --- | " + " | ".join("---" for _ in years) + " | --- |"]
    for s in UNSEEN + SEEN:
        ys, yb = yearly(table[s][3]["rets"]), yearly(bh_rets(s))
        cells = [f"{ys[y] * 100:+.0f}% / {yb[y] * 100:+.0f}%" if y in ys.index else "" for y in years]
        out.append(f"| {s} | " + " | ".join(cells) + f" | {pct(wb[s])} |")
    out.append("")

    out += ["## 35% 回撤熔断会在何时触发 (仅报告)", ""]
    for s in UNSEEN + SEEN:
        out.append(f"- {s}: {get(s, {'max_drawdown_halt': 0.35})['halted_on']}")
    out.append("")

    names = {
        "C1": "未看过币种 ≥6/8 夏普 > 0",
        "C2": "未看过币种 ≥6/8 夏普 > 买入持有",
        "C3": "未看过币种回撤全部更低, 中位数 ≤ 买入持有的 60%",
        "C4": "看过币种 ≥5/6 夏普 ≥ 买入持有; BTC/ETH 年化 ≥ 买入持有 40%",
        "C5": "成本翻倍后仍成立",
        "C6": "27 组参数中 ≥80% 夏普 > 0 (BTC、ETH)",
        "C7": "去掉最好一年后仍盈利",
    }
    out += ["## 结论", "", "| # | 标准 | 实际 | 结果 |", "| --- | --- | --- | --- |"]
    for k in sorted(names):
        ok, detail = v[k]
        out.append(f"| {k} | {names[k]} | {detail} | {'通过' if ok else '**未通过**'} |")
    passed = all(ok for ok, _ in v.values())
    out += ["", "**全部通过: 可以进入 QuantDinger 信号模式观察。**" if passed
            else "**未全部通过: 按事先约定, 不建议投入真实资金。**", ""]
    out += ["说明: 按日线开盘价成交、不计成交量限制; 只含今天仍在交易的币种 (幸存者偏差); 历史结果不代表未来。"]

    with open(os.path.join(ROOT, "research", "RESULTS_TREND_VOL.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
