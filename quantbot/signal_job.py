"""每日信号任务: 日线收盘后计算目标仓位, 维护模拟账户, 推送 Telegram。

执行语义与 QuantDinger Strategy API V2 (以及 integrations/quantdinger/mock_runtime.py) 一致:
    1. 第 t 根日线收盘后计算目标仓位, 按规则决定是否下单 (挂单);
    2. 挂单在第 t+1 根日线开盘价成交 (加滑点与手续费);
    3. 调仓规则: 目标为 0 且有仓位 -> 清仓; 否则目标与当前仓位差超过阈值 -> 调到目标。

任务是可恢复的: 状态保存在 JSON 文件, 每次运行只处理上次之后新收盘的日线,
重复运行不会重复处理。推送成功后才写入状态, 推送失败时下次运行会重新计算并补发。
这里只做模拟记账与通知, 不会向任何交易所下单。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from .data import fetch_binance
from .notify import load_env_file, send_telegram
from .strategies import TrendVolTarget


@dataclass
class PaperState:
    symbol: str
    capital: float
    cash: float
    qty: float = 0.0
    last_bar: str | None = None  # 最后处理的日线日期 (UTC, YYYY-MM-DD)
    pending: dict | None = None  # {"target": float, "reason": str, "decided_bar": str}
    fills: list = field(default_factory=list)
    started_bar: str | None = None
    peak_equity: float = 0.0

    @classmethod
    def new(cls, symbol: str, capital: float) -> "PaperState":
        return cls(symbol=symbol, capital=capital, cash=capital, peak_equity=capital)


@dataclass
class Event:
    bar: str
    close: float
    target: float
    weight: float  # 收盘时的实际仓位比例
    equity: float
    action: str  # entry / rebalance / exit / hold
    filled: dict | None  # 本根开盘时成交的上一笔挂单


class SignalEngine:
    def __init__(self, commission: float = 0.001, slippage: float = 0.0005, threshold: float = 0.1) -> None:
        self.commission = commission
        self.slippage = slippage
        self.threshold = threshold

    def _fill(self, st: PaperState, target_pct: float, open_price: float, bar: str, reason: str) -> dict | None:
        equity = st.cash + st.qty * open_price
        target_value = equity * target_pct
        if target_value > 0:
            target_value /= (1 + self.slippage) * (1 + self.commission)
        target_qty = target_value / open_price
        delta = target_qty - st.qty
        if abs(delta) <= 1e-12:
            return None
        price = open_price * (1 + self.slippage if delta > 0 else 1 - self.slippage)
        fee = abs(delta * price) * self.commission
        st.cash -= delta * price + fee
        st.qty = 0.0 if abs(target_qty) <= 1e-12 else target_qty
        fill = {"bar": bar, "qty": delta, "price": price, "fee": fee, "reason": reason}
        st.fills.append(fill)
        return fill

    def step(self, st: PaperState, bar: str, open_price: float, close: float, target: float) -> Event:
        filled = None
        if st.pending:
            filled = self._fill(st, st.pending["target"], open_price, bar, st.pending["reason"])
            st.pending = None
        equity = st.cash + st.qty * close
        st.peak_equity = max(st.peak_equity, equity)
        current = st.qty * close / equity if equity > 0 else 0.0
        holding = current > 0.001
        action = "hold"
        if target <= 0:
            if holding:
                action = "exit"
                st.pending = {"target": 0.0, "reason": "trend_exit", "decided_bar": bar}
        elif abs(target - current) > self.threshold:
            action = "rebalance" if holding else "entry"
            st.pending = {
                "target": float(target),
                "reason": "trend_rebalance" if holding else "trend_entry",
                "decided_bar": bar,
            }
        st.last_bar = bar
        if st.started_bar is None:
            st.started_bar = bar
        return Event(bar, close, float(target), current, equity, action, filled)


def replay(df: pd.DataFrame, targets: pd.Series, engine: SignalEngine, capital: float) -> PaperState:
    """从头到尾回放 (用于与 QuantDinger 版逐笔对比)。"""
    st = PaperState.new("REPLAY", capital)
    for t in df.index:
        engine.step(st, str(t.date()), float(df.at[t, "open"]), float(df.at[t, "close"]), float(targets.at[t]))
    return st


def load_state(path: str) -> PaperState | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return PaperState(**json.load(f))


def save_state(path: str, st: PaperState) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(st), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


ACTION_TEXT = {
    "entry": "买入至 {pct}",
    "rebalance": "调整仓位至 {pct}",
    "exit": "卖出清仓",
    "hold": "保持不变",
}


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def format_message(st: PaperState, events: list[Event], comps: pd.DataFrame, strategy: TrendVolTarget) -> str:
    last = events[-1]
    p = strategy.params
    row = comps.loc[pd.Timestamp(last.bar)]
    arrows = " ".join(
        f"{lb}日{'↑' if row[f'up_{lb}'] > 0 else '↓'}"
        for lb in (p["short_lookback"], p["mid_lookback"], p["long_lookback"])
    )
    base = st.symbol.replace("USDT", "")
    lines = [f"📊 {base}/USDT 趋势信号 · {last.bar} 日线收盘 (UTC)"]
    for ev in events[:-1]:  # 补处理的漏掉日期, 简要列出
        lines.append(f"(补算 {ev.bar}: 目标 {_pct(ev.target)}, {ACTION_TEXT[ev.action].format(pct=_pct(ev.target))})")
    for ev in events:
        if ev.filled:
            f = ev.filled
            side = "买入" if f["qty"] > 0 else "卖出"
            lines.append(f"✅ 已按 {ev.bar} 开盘价模拟成交: {side} {abs(f['qty']):.5f} {base} @ {f['price']:,.2f}")
    lines += [
        f"收盘价 {last.close:,.2f}",
        f"趋势: {arrows} → 得分 {int(round(row['score'] * 3))}/3",
        f"30日年化波动率 {row['vol'] * 100:.0f}% → 仓位上限 {_pct(row['size'])}",
        f"目标仓位 {_pct(last.target)} (当前 {_pct(last.weight)})",
    ]
    if last.action == "hold":
        lines.append("👉 操作: 保持不变")
    else:
        lines.append(f"👉 操作: 现在{ACTION_TEXT[last.action].format(pct=_pct(last.target))}")
    ret = last.equity / st.capital - 1
    dd = 1 - last.equity / st.peak_equity if st.peak_equity > 0 else 0.0
    lines.append(f"模拟账户 {last.equity:,.2f} USDT ({ret * 100:+.2f}%, 距高点 -{dd * 100:.1f}%)")
    lines.append("仅信号与模拟记账, 不会下单。")
    return "\n".join(lines)


def run_signal_job(
    symbol: str = "BTCUSDT",
    state_path: str = "~/quantbot-data/state_BTCUSDT.json",
    env_path: str | None = "~/quantbot-data/telegram.env",
    capital: float = 10_000.0,
    notify: bool = True,
    fetcher: Callable[..., pd.DataFrame] | None = None,
    sender: Callable[[str, str, str], None] | None = None,
    now: datetime | None = None,
) -> str | None:
    """处理新收盘的日线并推送。返回推送的文本; 没有新日线时返回 None。"""
    state_path = os.path.expanduser(state_path)
    env = load_env_file(os.path.expanduser(env_path)) if env_path else {}
    fetcher = fetcher or fetch_binance
    sender = sender or send_telegram
    now = now or datetime.now(timezone.utc)
    strategy = TrendVolTarget()
    engine = SignalEngine()

    start = (now - timedelta(days=500)).strftime("%Y-%m-%d")
    df = fetcher(symbol, interval="1d", start=start)
    if len(df) < strategy.warmup:
        raise RuntimeError(f"历史数据不足: {len(df)} 根 < {strategy.warmup}")
    comps = strategy.components(df)
    targets = comps["target"]

    st = load_state(state_path) or PaperState.new(symbol, capital)
    if st.last_bar is None:
        todo = [df.index[-1]]  # 第一次运行: 只处理最近一根已收盘日线
    else:
        todo = [t for t in df.index if t > pd.Timestamp(st.last_bar)]
    if not todo:
        return None

    events = [
        engine.step(st, str(t.date()), float(df.at[t, "open"]), float(df.at[t, "close"]), float(targets.at[t]))
        for t in todo
    ]
    text = format_message(st, events, comps, strategy)
    if notify:
        sender(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), text)
    save_state(state_path, st)  # 推送成功后才保存, 推送失败下次会重算并补发
    return text
