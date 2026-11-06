"""命令行入口: python -m quantbot <command> ..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .backtest import Backtester, BacktestConfig
from .bot import CcxtFeed, ReplayFeed, TradingBot
from .broker import CcxtBroker, PaperBroker
from .data import load
from .metrics import format_metrics
from .optimize import grid_search, walk_forward
from .risk import RiskConfig, RiskManager
from .strategies import STRATEGIES, get_strategy


def _parse_value(v: str) -> Any:
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def _parse_params(items: list[str] | None) -> dict[str, Any]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"参数格式应为 key=value: {item}")
        k, v = item.split("=", 1)
        out[k] = _parse_value(v)
    return out


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", "-s", default="sma_cross", choices=sorted(STRATEGIES))
    p.add_argument("--param", "-p", action="append", metavar="KEY=VALUE", help="策略参数, 可重复, 如 -p fast=10")
    p.add_argument("--data", "-d", default="synthetic", help="synthetic | file.csv | ccxt:BTC/USDT | yf:AAPL")
    p.add_argument("--timeframe", default="1d", help="ccxt K 线周期")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--limit", type=int, default=1000, help="ccxt 获取的 K 线数量")
    p.add_argument("--bars", type=int, default=1500, help="合成数据长度")
    p.add_argument("--seed", type=int, default=42, help="合成数据随机种子")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--commission", type=float, default=0.001)
    p.add_argument("--slippage", type=float, default=0.0005)
    p.add_argument("--max-position", type=float, default=1.0, help="最大仓位占权益比例")
    p.add_argument("--risk-per-trade", type=float, default=None, help="每笔风险占权益比例, 如 0.01")
    p.add_argument("--stop-atr", type=float, default=None, help="止损 ATR 倍数, 如 2")
    p.add_argument("--take-atr", type=float, default=None, help="止盈 ATR 倍数, 如 4")
    p.add_argument("--trailing", action="store_true", help="启用移动止损")
    p.add_argument("--max-dd", type=float, default=None, help="回撤熔断阈值, 如 0.3")


def _risk(a) -> RiskManager:
    return RiskManager(
        RiskConfig(
            max_position_pct=a.max_position,
            risk_per_trade=a.risk_per_trade,
            stop_loss_atr=a.stop_atr,
            take_profit_atr=a.take_atr,
            trailing_stop=a.trailing,
            max_drawdown_halt=a.max_dd,
        )
    )


def _data(a):
    return load(a.data, n=a.bars, seed=a.seed, timeframe=a.timeframe, limit=a.limit, exchange=a.exchange)


def cmd_list(a) -> None:
    for name, cls in STRATEGIES.items():
        print(f"{name:<22}{cls.description}")
        if cls.default_params:
            print(f"{'':<22}默认参数: {cls.default_params}")


def cmd_backtest(a) -> None:
    df = _data(a)
    strat = get_strategy(a.strategy, **_parse_params(a.param))
    cfg = BacktestConfig(initial_cash=a.cash, commission=a.commission, slippage=a.slippage)
    res = Backtester(strat, _risk(a), cfg).run(df)
    print(f"数据: {a.data}  {df.index[0]} ~ {df.index[-1]}  共 {len(df)} 根 K 线")
    print(res.summary())
    if a.trades:
        print(res.trades_df().to_string())
    if a.plot:
        print(f"图表已保存: {res.plot(a.plot)}")
    if a.output:
        res.equity.to_csv(a.output)
        print(f"权益曲线已保存: {a.output}")


def cmd_compare(a) -> None:
    df = _data(a)
    cfg = BacktestConfig(initial_cash=a.cash, commission=a.commission, slippage=a.slippage)
    rows = []
    for name in STRATEGIES:
        m = Backtester(get_strategy(name), _risk(a), cfg).run(df).metrics
        rows.append((name, m))
    print(f"{'策略':<22}{'总收益':>10}{'年化':>10}{'夏普':>8}{'最大回撤':>10}{'交易数':>8}")
    for name, m in sorted(rows, key=lambda x: -x[1]["sharpe"]):
        print(f"{name:<22}{m['total_return']*100:>9.1f}%{m['cagr']*100:>9.1f}%{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']*100:>9.1f}%{m['num_trades']:>8d}")


def cmd_optimize(a) -> None:
    df = _data(a)
    grid = {k: v if isinstance(v, list) else [v] for k, v in _parse_params(a.grid).items()}
    if not grid:
        raise SystemExit("请用 --grid 指定参数网格, 如 --grid fast=[5,10,20] --grid slow=[50,100]")
    cfg = BacktestConfig(initial_cash=a.cash, commission=a.commission, slippage=a.slippage)
    if a.walk_forward:
        res = walk_forward(a.strategy, grid, df, n_splits=a.walk_forward, metric=a.metric, risk=_risk(a), config=cfg)
    else:
        res = grid_search(a.strategy, grid, df, metric=a.metric, train_frac=a.train_frac, risk=_risk(a), config=cfg)
    with __import__("pandas").option_context("display.width", 200, "display.max_columns", 20):
        print(res.head(a.top).to_string() if not res.empty else "没有有效的参数组合")


def cmd_trade(a) -> None:
    strat = get_strategy(a.strategy, **_parse_params(a.param))
    risk = _risk(a)
    if a.live:
        if not a.data.startswith("ccxt:"):
            raise SystemExit("实盘模式需要 --data ccxt:SYMBOL")
        symbol = a.data[5:]
        feed = CcxtFeed(symbol, a.timeframe, a.exchange, a.limit)
        broker = CcxtBroker(a.exchange, dry_run=not a.confirm_live)
        if a.confirm_live:
            logging.warning("!!! 实盘模式: 将使用真实资金下单 !!!")
    else:
        df = _data(a)
        symbol = a.data[5:] if a.data.startswith("ccxt:") else "ASSET"
        feed = ReplayFeed(df, start=max(strat.warmup, risk.config.atr_period) + 1)
        broker = PaperBroker(cash=a.cash, commission=a.commission, slippage=a.slippage, state_file=a.state)
    bot = TradingBot(strat, broker, feed, symbol, risk, cost_rate=a.commission + a.slippage)
    bot.run(interval=a.interval if a.live else 0, max_steps=a.max_steps)
    if not a.live:
        price = float(feed.df["close"].iloc[-1])
        print(f"模拟结束: 成交 {len(bot.state.fills)} 笔, 期末权益 {broker.equity({symbol: price}):,.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="quantbot", description="量化交易机器人")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有策略").set_defaults(func=cmd_list)

    p = sub.add_parser("backtest", help="回测单个策略")
    _add_common(p)
    p.add_argument("--plot", help="保存图表到 PNG 文件")
    p.add_argument("--output", help="保存权益曲线到 CSV")
    p.add_argument("--trades", action="store_true", help="打印逐笔交易")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("compare", help="用默认参数对比所有策略")
    _add_common(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("optimize", help="参数网格搜索")
    _add_common(p)
    p.add_argument("--grid", "-g", action="append", metavar="KEY=[V1,V2]", help="参数网格, 如 -g fast=[5,10,20]")
    p.add_argument("--metric", default="sharpe")
    p.add_argument("--train-frac", type=float, default=0.7)
    p.add_argument("--walk-forward", type=int, default=0, metavar="N", help="使用 N 段滚动窗口验证")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_optimize)

    p = sub.add_parser("trade", help="运行交易机器人 (默认: 历史回放模拟盘)")
    _add_common(p)
    p.add_argument("--live", action="store_true", help="连接交易所实时数据 (默认 dry-run 不下真实订单)")
    p.add_argument("--confirm-live", action="store_true", help="确认使用真实资金下单 (危险)")
    p.add_argument("--interval", type=float, default=60.0, help="实盘轮询间隔秒数")
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--state", help="模拟盘状态持久化 JSON 文件")
    p.set_defaults(func=cmd_trade)

    a = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if a.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        a.func(a)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
