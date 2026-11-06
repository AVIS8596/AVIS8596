"""按 research/CRITERIA_SWAP.md 事先确定的及格线, 检验永续合约多空版是否优于现货只做多版。

运行: python research/evaluate_swap.py   (需能访问 data.binance.vision 与 data-api.binance.vision; 数据缓存在 data/)
输出: research/RESULTS_SWAP.md
"""

from __future__ import annotations

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
from quantbot.data import fetch_binance, fetch_binance_futures_archive  # noqa: E402

SWAP_SRC = open(os.path.join(ROOT, "integrations/quantdinger/trend_vol_target_swap_v2.py"), encoding="utf-8").read()
SPOT_SRC = open(os.path.join(ROOT, "integrations/quantdinger/trend_vol_target_v2.py"), encoding="utf-8").read()
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LTCUSDT",
         "TRXUSDT", "ETCUSDT", "XLMUSDT", "LINKUSDT", "DOGEUSDT", "BCHUSDT", "ATOMUSDT"]
COMMISSION, SLIPPAGE = 0.001, 0.0005
EVAL_START_BAR = 150
PPY = 365
CACHE = os.path.join(ROOT, "data")


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


def evaluate(symbol: str) -> dict:
    perp, funding = fetch_binance_futures_archive(symbol, cache_dir=CACHE)
    spot = fetch_binance(symbol, "1d", start="2017-01-01", cache_dir=CACHE)
    start = perp.index[EVAL_START_BAR]
    end = min(perp.index[-1], spot.index[-1])
    perp, spot = perp.loc[:end], spot.loc[:end]

    def rets(res):
        eq = res["equity"].loc[start:]
        return eq.pct_change().dropna()

    swap = mock_runtime.run(SWAP_SRC, perp, initial_cash=100_000, commission=COMMISSION, slippage=SLIPPAGE,
                            start=str(start.date()), funding=funding)
    stress = mock_runtime.run(SWAP_SRC, perp, initial_cash=100_000, commission=COMMISSION * 2,
                              slippage=SLIPPAGE * 2, start=str(start.date()), funding=funding * 1.5)
    spot_res = mock_runtime.run(SPOT_SRC, spot, initial_cash=100_000, commission=COMMISSION, slippage=SLIPPAGE,
                                start=str(start.date()))
    r_swap, r_spot = rets(swap), rets(spot_res)
    w = swap["weight"].loc[start:]
    prev_w = w.shift(1).reindex(r_swap.index)
    cur_w = w.reindex(r_swap.index)
    short_days = (prev_w < -0.001) | (cur_w < -0.001)
    long_days = (prev_w > 0.001) | (cur_w > 0.001)
    short_leg = float((1 + r_swap[short_days]).prod() - 1)
    long_leg = float((1 + r_swap[long_days]).prod() - 1)
    bh = perp["close"].loc[start:].pct_change().dropna()
    yearly = pd.DataFrame({
        "swap": (1 + r_swap).groupby(r_swap.index.year).prod() - 1,
        "spot": (1 + r_spot).groupby(r_spot.index.year).prod() - 1,
        "bh": (1 + bh).groupby(bh.index.year).prod() - 1,
    })
    return {
        "symbol": symbol, "start": start.date(), "end": end.date(),
        "swap": stats(r_swap), "spot": stats(r_spot), "stress": stats(rets(stress)), "bh": stats(bh),
        "short_leg": short_leg, "long_leg": long_leg,
        "short_share": float((w < -0.001).mean()), "long_share": float((w > 0.001).mean()),
        "funding_pct": swap["funding_paid"] / 100_000, "fills": len(swap["fills"]), "yearly": yearly,
    }


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def main() -> None:
    with ProcessPoolExecutor() as pool:
        res = {r["symbol"]: r for r in pool.map(evaluate, COINS)}

    out = ["# 永续合约多空版: 真实数据检验结果", ""]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out += [f"生成时间: {now}。及格线见 [CRITERIA_SWAP.md](CRITERIA_SWAP.md) (与策略代码一起在看结果前提交)。",
            "已扣除手续费 0.1%、滑点 0.05% (每边) 与真实资金费 (当月尚未归档的几天资金费按 0 计)。不加杠杆。", ""]
    out += ["## 合约多空版 vs 现货只做多版 (同一区间)", "",
            "| 币种 | 区间 | 合约总收益 | 合约夏普 | 合约最大回撤 | 现货版总收益 | 现货版夏普 | 现货版最大回撤 | 买入持有夏普 | 压力测试夏普 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in COINS:
        r = res[s]
        out.append(f"| {s} | {r['start']} ~ {r['end']} | {pct(r['swap']['return'])} | {r['swap']['sharpe']:.2f} | "
                   f"{r['swap']['mdd'] * 100:.1f}% | {pct(r['spot']['return'])} | {r['spot']['sharpe']:.2f} | "
                   f"{r['spot']['mdd'] * 100:.1f}% | {r['bh']['sharpe']:.2f} | {r['stress']['sharpe']:.2f} |")
    out += ["", "## 多空拆分与资金费", "",
            "| 币种 | 持多天数占比 | 持空天数占比 | 多头部分累计 | 空头部分累计 | 累计资金费 (占初始资金, 正=支付) | 成交笔数 |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for s in COINS:
        r = res[s]
        out.append(f"| {s} | {r['long_share'] * 100:.0f}% | {r['short_share'] * 100:.0f}% | {pct(r['long_leg'])} | "
                   f"{pct(r['short_leg'])} | {pct(r['funding_pct'])} | {r['fills']} |")
    out.append("")

    v = {}
    v["C1"] = (all(res[s]["swap"]["sharpe"] >= res[s]["spot"]["sharpe"] for s in ("BTCUSDT", "ETHUSDT")),
               "; ".join(f"{s[:3]} {res[s]['swap']['sharpe']:.2f} vs {res[s]['spot']['sharpe']:.2f}" for s in ("BTCUSDT", "ETHUSDT")))
    n2 = sum(res[s]["swap"]["sharpe"] > res[s]["spot"]["sharpe"] for s in COINS)
    v["C2"] = (n2 >= 9, f"{n2}/14 合约版夏普更高")
    n3 = sum(res[s]["swap"]["sharpe"] > 0 for s in COINS)
    v["C3"] = (n3 >= 11, f"{n3}/14 夏普 > 0")
    med_short = statistics.median(res[s]["short_leg"] for s in COINS)
    v["C4"] = (res["BTCUSDT"]["short_leg"] > 0 and res["ETHUSDT"]["short_leg"] > 0 and med_short > 0,
               f"BTC {pct(res['BTCUSDT']['short_leg'])}; ETH {pct(res['ETHUSDT']['short_leg'])}; 中位数 {pct(med_short)}")
    med_dd_swap = statistics.median(res[s]["swap"]["mdd"] for s in COINS)
    med_dd_spot = statistics.median(res[s]["spot"]["mdd"] for s in COINS)
    v["C5"] = (res["BTCUSDT"]["swap"]["mdd"] <= 0.6 and res["ETHUSDT"]["swap"]["mdd"] <= 0.6
               and med_dd_swap <= med_dd_spot + 0.10,
               f"BTC {res['BTCUSDT']['swap']['mdd'] * 100:.1f}%; ETH {res['ETHUSDT']['swap']['mdd'] * 100:.1f}%; "
               f"中位数 {med_dd_swap * 100:.1f}% vs 现货版 {med_dd_spot * 100:.1f}% + 10")
    n6 = sum(res[s]["stress"]["sharpe"] > 0 for s in COINS)
    v["C6"] = (res["BTCUSDT"]["stress"]["sharpe"] > 0 and res["ETHUSDT"]["stress"]["sharpe"] > 0 and n6 >= 9,
               f"BTC {res['BTCUSDT']['stress']['sharpe']:.2f}; ETH {res['ETHUSDT']['stress']['sharpe']:.2f}; {n6}/14 夏普 > 0")

    out += ["## 逐年收益 (合约多空版 / 现货只做多版 / 买入持有)", ""]
    years = sorted({y for s in COINS for y in res[s]["yearly"].index})
    out += ["| 币种 | " + " | ".join(str(y) for y in years) + " |", "| --- | " + " | ".join("---" for _ in years) + " |"]
    for s in COINS:
        y = res[s]["yearly"]
        out.append(f"| {s} | " + " | ".join(
            f"{y.loc[yr, 'swap'] * 100:+.0f} / {y.loc[yr, 'spot'] * 100:+.0f} / {y.loc[yr, 'bh'] * 100:+.0f}" if yr in y.index else ""
            for yr in years) + " |")
    out.append("")

    names = {
        "C1": "BTC、ETH 合约版夏普 ≥ 现货版",
        "C2": "≥9/14 币种合约版夏普更高",
        "C3": "≥11/14 币种合约版夏普 > 0",
        "C4": "空头部分本身赚钱",
        "C5": "回撤不显著变大",
        "C6": "成本压力测试后仍成立",
    }
    out += ["## 结论", "", "| # | 标准 | 实际 | 结果 |", "| --- | --- | --- | --- |"]
    for k in sorted(names):
        ok, detail = v[k]
        out.append(f"| {k} | {names[k]} | {detail} | {'通过' if ok else '**未通过**'} |")
    passed = all(ok for ok, _ in v.values())
    out += ["", "**全部通过: 合约多空版可以进入 QuantDinger 信号模式观察。**" if passed
            else "**未全部通过: 按事先约定, 不建议使用永续合约版, 继续只用现货版。**", ""]
    out += ["说明: 按日线开盘价成交、不计成交量限制与强平; 资金费按日汇总近似; 幸存者偏差; 历史结果不代表未来。"]
    with open(os.path.join(ROOT, "research", "RESULTS_SWAP.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
