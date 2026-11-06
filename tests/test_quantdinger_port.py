"""离线检查 QuantDinger 版唐奇安策略与 quantbot 原策略的一致性。"""

import ast
import re
from pathlib import Path

import pytest

from integrations.quantdinger import mock_runtime
from quantbot.backtest import Backtester, BacktestConfig
from quantbot.data import generate_synthetic
from quantbot.strategies import get_strategy

SRC = (Path(__file__).resolve().parents[1] / "integrations/quantdinger/donchian_breakout_v2.py").read_text()

# QuantDinger 编译器的硬性约束 (摘自其 strategy_v2/contract.py 与开发指南)
RUNTIME_GLOBALS = {
    "factor", "get_factors", "get_fundamentals", "get_history", "get_index_stocks", "get_position",
    "get_positions", "get_order_status", "consume_last_exit_reason", "cancel_order",
    "get_universe_stocks", "history", "indicator", "is_trade", "log", "order", "order_target",
    "order_target_percent", "order_target_value", "order_value", "run_daily", "run_monthly",
    "run_weekly", "set_default_protection",
}
SAFE_BUILTINS = {"bool", "int", "float", "str", "list", "tuple", "dict", "set", "range", "abs", "round",
                 "min", "max", "sum", "len", "enumerate", "zip", "sorted", "any", "all", "isinstance"}
ALLOWED_IMPORTS = {"numpy", "pandas", "math", "json", "datetime", "time", "collections", "functools",
                   "itertools", "statistics", "decimal", "fractions", "copy"}


@pytest.fixture(scope="module")
def df():
    return generate_synthetic(n=1500, seed=42)


def test_source_follows_strategy_v2_contract():
    tree = ast.parse(SRC)
    assert isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value.value, str)  # docstring
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "initialize" in funcs and "handle_data" in funcs
    for node in tree.body:  # 顶层只允许函数、导入、字面量赋值和 docstring
        assert isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.Assign, ast.Expr))
        if isinstance(node, ast.Assign):
            assert isinstance(node.value, ast.Constant)
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in ALLOWED_IMPORTS for a in node.names)
    # initialize 中不能读取 context.params
    assert not any(isinstance(n, ast.Attribute) and n.attr == "params" for n in ast.walk(funcs["initialize"]))
    # 全局函数调用必须是运行时 API、安全内置函数或本文件定义的函数
    defined = set(funcs) | RUNTIME_GLOBALS | SAFE_BUILTINS
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            assert n.func.id in defined, n.func.id
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "get_history":
            assert "fields" not in {k.arg for k in n.keywords}
    # @param 默认值与代码回退值一致
    params = mock_runtime.parse_params(SRC)
    fallbacks = dict(re.findall(r'context\.params\.get\("(\w+)", ([^)]+)\)', SRC))
    assert set(fallbacks) == set(params)
    for name, default in params.items():
        assert ast.literal_eval(fallbacks[name]) == default, name


def test_signal_timing_matches_quantbot_without_stops(df):
    """关闭止损/风险定仓/熔断后, 进出场时间应与 quantbot 的 donchian 完全一致。"""
    res = mock_runtime.run(SRC, df, {"stop_atr": 0.0, "risk_per_trade": 0.0, "max_position_pct": 1.0,
                                     "max_drawdown_halt": 0.0})
    qd_entries = [f["time"] for f in res["fills"] if f["qty"] > 0]
    qd_exits = [f["time"] for f in res["fills"] if f["qty"] < 0]

    qb = Backtester(get_strategy("donchian"), config=BacktestConfig(commission=0, slippage=0)).run(df)
    qb_trades = [t for t in qb.trades if t.exit_reason != "end_of_data"]
    assert qd_entries[: len(qb.trades)] == [t.entry_time for t in qb.trades]
    assert qd_exits == [t.exit_time for t in qb_trades]
    assert res["metadata"]["direction_mode"] == "long_only"
    assert res["symbol"] == "Crypto:BTC/USDT@spot"


def test_default_risk_settings_run_and_respect_rules(df):
    res = mock_runtime.run(SRC, df)
    reasons = [f["reason"] for f in res["fills"]]
    assert "donchian_long_entry" in reasons
    assert any(r in ("stop_loss", "atr_trailing_exit") for r in reasons)
    # 每笔开仓权重不超过 max_position_pct
    eq = res["equity"]
    for f in res["fills"]:
        if f["qty"] > 0:
            assert f["qty"] * f["price"] <= eq.loc[f["time"]] * 0.95 * 1.2  # 开盘价与收盘价权益存在差异
    # 保护离场后, 直到通道离场信号出现前不会再次入场
    for a, b in zip(res["fills"], res["fills"][1:]):
        if a["reason"] in ("stop_loss", "atr_trailing_exit"):
            assert b["reason"] == "donchian_long_entry"
            assert b["time"] > a["time"]


def test_drawdown_halt_stops_trading(df):
    res = mock_runtime.run(SRC, df, {"max_drawdown_halt": 0.02, "stop_atr": 0.0})
    assert res["state"].halted
    last = res["fills"][-1]
    assert last["reason"] in ("drawdown_halt_exit", "donchian_long_exit")


def test_mock_short_accounting_and_funding_sign():
    """空头: 价格下跌赚钱; 资金费率为正时空头收钱、多头付钱。"""
    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    df = pd.DataFrame({"open": [100, 100, 80, 80], "high": [100, 100, 80, 80],
                       "low": [100, 100, 80, 80], "close": [100, 100, 80, 80], "volume": 1.0}, index=idx)

    def strat(pct):
        return (
            "def initialize(context):\n"
            "    context.set_universe(['Crypto:X/USDT@swap'])\n"
            "def handle_data(context, data):\n"
            f"    order_target_percent('Crypto:X/USDT@swap', {pct}, reason='t')\n"
        )

    short = mock_runtime.run(strat(-0.5), df, initial_cash=1000)
    # 第 1 根开盘 100 做空 5 个单位, 收盘 80 时盈利 100
    assert short["equity"].iloc[-1] == pytest.approx(1100)
    rate = pd.Series(0.01, index=idx)
    short_f = mock_runtime.run(strat(-0.5), df, initial_cash=1000, funding=rate)
    long_f = mock_runtime.run(strat(0.5), df, initial_cash=1000, funding=rate)
    assert short_f["funding_paid"] < 0 < long_f["funding_paid"]
    assert short_f["equity"].iloc[-1] > short["equity"].iloc[-1]
