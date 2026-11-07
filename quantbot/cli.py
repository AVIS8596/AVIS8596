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


def cmd_signal(a) -> None:
    import os
    import time

    from .notify import load_env_file, send_telegram
    from .signal_job import default_state_path, run_signal_job

    a.state = a.state or default_state_path(a.symbol)

    if a.no_push:
        import tempfile

        # 预览模式: 用临时状态文件, 不影响真实状态
        state = os.path.expanduser(a.state)
        with tempfile.TemporaryDirectory() as d:
            tmp_state = os.path.join(d, "state.json")
            if os.path.exists(state):
                with open(state, encoding="utf-8") as src, open(tmp_state, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
            text = run_signal_job(a.symbol, tmp_state, None, a.capital, notify=False)
        print(text or "没有新的已收盘日线。")
        return
    try:
        text = run_signal_job(a.symbol, a.state, a.env, a.capital)
        print(text or "没有新的已收盘日线。")
    except Exception as exc:
        # 只在信号真正被耽误时报警 (下一根日线收盘后 3 小时仍未处理), 且 6 小时内最多一次;
        # 没有新日线的整点运行遇到临时网络故障只写日志, 下一小时自动重试。
        from .signal_job import load_state, signal_overdue

        marker = os.path.expanduser(a.state) + ".alert"
        recent = os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 6 * 3600
        st = load_state(os.path.expanduser(a.state))
        if not recent and signal_overdue(st.last_bar if st else None):
            env = load_env_file(os.path.expanduser(a.env))
            try:
                send_telegram(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""),
                              f"⚠️ quantbot 信号任务失败 ({a.symbol}): {exc}\n下次定时运行会自动重试。")
                os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
                with open(marker, "w") as f:
                    f.write(str(exc))
            except Exception:
                pass
        raise


def cmd_live(a) -> None:
    import os

    from .live import CcxtSpot, LiveConfig, flatten, run_live
    from .notify import load_env_file

    if a.mainnet and not a.i_understand_real_money:
        raise SystemExit("真实资金需要同时加上 --i-understand-real-money")
    net = "mainnet" if a.mainnet else "testnet"
    pair = a.symbol.replace("/", "").upper()
    keys_path = os.path.expanduser(a.keys or f"~/quantbot-data/binance-{net}.env")
    keys = load_env_file(keys_path)
    key, secret = keys.get("BINANCE_API_KEY", ""), keys.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        raise SystemExit(f"请在 {keys_path} 中写入 BINANCE_API_KEY=... 和 BINANCE_API_SECRET=...")
    cfg = LiveConfig(
        symbol=a.symbol.upper(), venue=f"binance-{net}", capital_cap=a.cap, max_order_usdt=a.max_order,
        stop_loss=a.stop_loss or None, dry_run=a.dry_run,
        state_path=a.state or f"~/quantbot-data/live_{pair}_{net}.json",
    )
    ex = CcxtSpot(cfg.symbol, key, secret, testnet=not a.mainnet)
    if a.flatten:
        print(flatten(cfg, ex))
        return
    if a.dry_run:  # 演练使用临时状态文件, 不影响正式状态
        import shutil
        import tempfile

        real = os.path.expanduser(cfg.state_path)
        with tempfile.TemporaryDirectory() as d:
            cfg.state_path = os.path.join(d, "live.json")
            if os.path.exists(real):
                shutil.copy(real, cfg.state_path)
            text = run_live(cfg, ex, a.env)
    else:
        try:
            text = run_live(cfg, ex, a.env)
        except Exception as exc:
            _alert_live_failure(cfg, a.env, exc)
            raise
    print(text or "没有新的已收盘日线, 对账正常。")


def _alert_live_failure(cfg, env_path: str, exc: Exception) -> None:
    """自动交易出错时报警: 当天日线收盘 3 小时后仍未处理, 或任何时候连续失败超过 3 小时; 6 小时内最多一次。"""
    import json
    import os
    import time

    from .notify import load_env_file, send_telegram
    from .signal_job import signal_overdue

    state_path = os.path.expanduser(cfg.state_path)
    marker = state_path + ".alert"
    first_fail = state_path + ".firstfail"
    now = time.time()
    if not os.path.exists(first_fail):
        with open(first_fail, "w") as f:
            f.write(str(now))
    with open(first_fail) as f:
        failing_for = now - float(f.read() or now)
    last_bar = None
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            last_bar = json.load(f).get("last_bar")
    recent = os.path.exists(marker) and now - os.path.getmtime(marker) < 6 * 3600
    if recent or not (signal_overdue(last_bar) or failing_for >= 3 * 3600):
        return
    env = load_env_file(os.path.expanduser(env_path))
    try:
        send_telegram(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""),
                      f"⚠️ 自动交易任务失败 ({cfg.symbol} @ {cfg.venue}): {exc}\n"
                      "已挂的止损单仍在交易所生效。下次定时运行会自动重试。")
        with open(marker, "w") as f:
            f.write(str(exc))
    except Exception:
        pass


def cmd_telegram_setup(a) -> None:
    import os

    from .notify import TelegramError, find_chat_ids, load_env_file, send_telegram

    path = os.path.expanduser(a.env)
    env = load_env_file(path)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit(f"请先在 {path} 中写入 TELEGRAM_BOT_TOKEN=...")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        try:
            chats = find_chat_ids(token)
        except TelegramError as e:
            raise SystemExit(str(e)) from None
        if not chats:
            raise SystemExit("没有找到聊天。请先在 Telegram 里打开你的机器人, 发送一条消息 (如 /start), 再运行本命令。")
        chat_id, name = chats[-1]
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\nTELEGRAM_CHAT_ID={chat_id}\n")
        print(f"找到聊天: {name} (ID {chat_id}), 已写入 {path}")
    try:
        send_telegram(token, chat_id, "✅ quantbot 已连接。以后每天日线收盘后会在这里推送信号。")
    except TelegramError as e:
        raise SystemExit(str(e)) from None
    print("测试消息已发送, 请查看 Telegram。")


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

    p = sub.add_parser("signal", help="每日信号: 处理新收盘日线, 模拟记账并推送 Telegram (不下单)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--state", default=None, help="状态文件, 默认 ~/quantbot-data/state_<SYMBOL>.json")
    p.add_argument("--env", default="~/quantbot-data/telegram.env", help="Telegram 配置文件")
    p.add_argument("--capital", type=float, default=10_000.0, help="模拟账户初始资金 (首次运行时生效)")
    p.add_argument("--no-push", action="store_true", help="只打印不推送, 也不保存状态")
    p.set_defaults(func=cmd_signal)

    p = sub.add_parser("telegram-setup", help="查找聊天 ID 并发送测试消息")
    p.add_argument("--env", default="~/quantbot-data/telegram.env")
    p.set_defaults(func=cmd_telegram_setup)

    p = sub.add_parser("live", help="自动交易 (默认币安测试网, 假钱)")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--cap", type=float, default=500.0, help="机器人最多管理的 USDT (首次运行时生效)")
    p.add_argument("--max-order", type=float, default=500.0, help="单笔下单金额上限 USDT")
    p.add_argument("--stop-loss", type=float, default=0.20, help="交易所止损幅度, 0 表示不挂止损")
    p.add_argument("--keys", default=None, help="API Key 文件, 默认 ~/quantbot-data/binance-testnet.env")
    p.add_argument("--env", default="~/quantbot-data/telegram.env", help="Telegram 配置文件")
    p.add_argument("--state", default=None)
    p.add_argument("--dry-run", action="store_true", help="演练: 只计算和推送, 不下单")
    p.add_argument("--flatten", action="store_true", help="撤销止损单并卖出机器人持有的全部币")
    p.add_argument("--mainnet", action="store_true", help="连接真实币安账户 (真钱)")
    p.add_argument("--i-understand-real-money", action="store_true", help="与 --mainnet 一起使用, 确认使用真钱")
    p.set_defaults(func=cmd_live)

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
