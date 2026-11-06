"""在本地模拟 QuantDinger Strategy API V2 的一个最小子集, 用于离线检查移植后的策略。

这不是 QuantDinger 的官方运行时, 只按其文档实现了本策略用到的接口与时序:
- handle_data 在每根已完成 K 线收盘后调用, 订单在下一根 K 线开盘成交;
- 开仓附带的 stop_loss_pct / trailing_stop_pct 在盘中检查, 跳空按开盘价成交,
  同一根 K 线同时触发时止损优先;
- 保护性离场原因可通过 consume_last_exit_reason 在同一根 K 线收盘时读取。
最终请务必在 QuantDinger 中用 /api/strategies/verify 和回测再验证一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd


@dataclass
class _Position:
    symbol: str
    amount: float = 0.0
    avg_cost: float = 0.0


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg) -> None:
        self.lines.append(str(msg))

    warning = info


def parse_params(source: str) -> dict:
    out = {}
    for name, typ, default in re.findall(r"^# @param (\w+) (\w+) (\S+)", source, re.M):
        out[name] = {"int": int, "float": float}.get(typ, lambda v: v.lower() == "true")(default)
    return out


def run(
    source: str,
    df: pd.DataFrame,
    params: dict | None = None,
    initial_cash: float = 100_000.0,
    commission: float = 0.0,
    slippage: float = 0.0,
) -> dict:
    symbol_holder: dict = {}
    g = SimpleNamespace()
    state = {"i": 0, "cash": initial_cash, "pos": None, "orders": [], "exit_reason": {}, "prot": None}
    fills: list[dict] = []
    log = _Log()

    def get_history(count, frequency, field, symbol):
        fields = [field] if isinstance(field, str) else list(field)
        end = state["i"] + 1
        return df.iloc[max(0, end - int(count)) : end][fields]

    def get_position(symbol):
        p = state["pos"]
        return p if p is not None else _Position(symbol)

    def order_target_percent(symbol, percent, reason="", **protection):
        state["orders"].append((symbol, float(percent), reason, protection))
        return None

    def consume_last_exit_reason(symbol):
        return state["exit_reason"].pop(symbol, "")

    class _Ctx:
        def __init__(self) -> None:
            self.params = {**parse_params(source), **(params or {})}
            self.portfolio = SimpleNamespace(total_value=initial_cash)
            self.meta: dict = {}

        def set_universe(self, symbols):
            symbol_holder["symbol"] = symbols[0]

        def subscribe(self, **kw):
            pass

        def set_warmup(self, n):
            pass

        def set_benchmark(self, s):
            pass

        def set_metadata(self, **kw):
            self.meta.update(kw)

    ctx = _Ctx()
    ns = {
        "g": g,
        "log": log,
        "get_history": get_history,
        "get_position": get_position,
        "order_target_percent": order_target_percent,
        "consume_last_exit_reason": consume_last_exit_reason,
    }
    exec(compile(source, "<strategy>", "exec"), ns)
    ns["initialize"](ctx)

    def fill(t, qty, raw_price, reason):
        # QuantDinger: 成交价 = 参考价 × (1 ± 滑点), 手续费 = 成交额 × 费率
        price = raw_price * (1 + slippage if qty > 0 else 1 - slippage)
        fee = abs(qty * price) * commission
        state["cash"] -= qty * price + fee
        fills.append({"time": t, "qty": qty, "price": price, "fee": fee, "reason": reason})
        return price

    equity_curve = []
    weights = []
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    for i in range(len(df)):
        state["i"] = i
        t = df.index[i]
        # 1. 上一根收盘排队的订单在本根开盘成交
        for sym, pct, reason, prot in state["orders"]:
            cur = state["pos"].amount if state["pos"] else 0.0
            equity = state["cash"] + cur * o[i]
            target_value = equity * pct
            if target_value > 0:
                target_value /= (1 + slippage) * (1 + commission)
            target = target_value / o[i]
            delta = target - cur
            if abs(delta) > 1e-12:
                px = fill(t, delta, o[i], reason)
                if cur == 0 and target > 0:
                    state["pos"] = _Position(sym, target, px)
                    state["prot"] = {
                        "stop": px * (1 - prot.get("stop_loss_pct", 0.0)) if prot.get("stop_loss_pct") else None,
                        "trail_pct": prot.get("trailing_stop_pct", 0.0),
                        "highest": px,
                    }
                elif target <= 1e-12:
                    state["pos"], state["prot"] = None, None
                else:
                    state["pos"].amount = target
        state["orders"] = []
        # 2. 盘中保护 (止损优先, 再移动止损)
        pos, prot = state["pos"], state["prot"]
        if pos is not None and prot is not None:
            trail = prot["highest"] * (1 - prot["trail_pct"]) if prot["trail_pct"] > 0 else None
            hit = None
            if prot["stop"] is not None and l[i] <= prot["stop"]:
                hit = ("stop_loss", prot["stop"])
            elif trail is not None and l[i] <= trail:
                hit = ("trailing_stop", trail)
            if hit:
                reason, level = hit
                fill(t, -pos.amount, min(o[i], level), reason)
                state["exit_reason"][pos.symbol] = reason
                state["pos"], state["prot"] = None, None
            else:
                prot["highest"] = max(prot["highest"], h[i])
        # 3. 收盘: 更新权益并调用 handle_data
        amount = state["pos"].amount if state["pos"] else 0.0
        ctx.portfolio.total_value = state["cash"] + amount * c[i]
        equity_curve.append(ctx.portfolio.total_value)
        weights.append(amount * c[i] / ctx.portfolio.total_value if ctx.portfolio.total_value > 0 else 0.0)
        ns["handle_data"](ctx, None)

    return {
        "fills": fills,
        "equity": pd.Series(equity_curve, index=df.index),
        "weight": pd.Series(weights, index=df.index),
        "metadata": ctx.meta,
        "symbol": symbol_holder.get("symbol"),
        "log": log.lines,
        "state": g,
    }
