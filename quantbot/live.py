"""实盘 / 测试网自动执行: trend_vol 策略的现货执行器。

每次运行 (建议 cron 每小时一次):
    1. 对账: 读取交易所余额; 检查保护性止损单是否已触发; 机器人持仓与交易所不一致时停机报警。
    2. 有新收盘日线时: 计算目标仓位, 按 10% 阈值规则用市价单调仓, 然后在交易所重挂止损单
       (前一日收盘价 × (1 − 止损幅度))。
    3. 没有新日线时: 只对账, 发现持仓却没有止损单就补挂。

安全规则 (硬编码, 不能被策略突破):
    - 机器人只管理自己的资金: 初始资金 = min(资金上限, 账户可用 USDT); 之后的资金只来自自己的成交盈亏。
    - 只卖机器人自己买入的数量, 不会动账户里的其他持仓。
    - 单笔下单金额不超过 max_order_usdt; 买入不超过账户可用 USDT。
    - 紧急停止: 存在 kill switch 文件时不下任何新单 (已挂的止损单保留)。
    - 同一时间只允许一个实例运行 (文件锁)。
    - 每次与交易所交互后立即保存状态, 推送失败不会导致重复下单。
    - 默认使用币安测试网; 真实资金必须显式传入 --mainnet 和 --i-understand-real-money。
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import pandas as pd

from .data import fetch_binance
from .notify import load_env_file, send_telegram
from .strategies import TrendVolTarget


# --------------------------------------------------------------------------- 交易所接口
class SpotExchange(Protocol):
    def price(self) -> float: ...
    def balances(self) -> tuple[float, float]: ...  # (可用 USDT, 基础币总余额)
    def market_order(self, side: str, amount: float) -> dict: ...
    def place_stop(self, amount: float, stop_price: float) -> str: ...
    def cancel(self, order_id: str) -> None: ...
    def order_info(self, order_id: str) -> dict: ...  # {status, filled, cost, fee_quote}
    def amount_precision(self, amount: float) -> float: ...
    def min_notional(self) -> float: ...
    def stop_floor(self, price: float) -> float: ...  # 交易所允许的最低卖出挂单价 (没有限制时返回 0)


class CcxtSpot:
    """ccxt 币安现货。testnet=True 时连接 testnet.binance.vision (假钱)。"""

    def __init__(self, symbol: str, api_key: str, secret: str, testnet: bool = True) -> None:
        import ccxt  # type: ignore

        self.symbol = symbol
        self.ex = ccxt.binance({"apiKey": api_key, "secret": secret, "enableRateLimit": True,
                                "options": {"defaultType": "spot"}})
        if testnet:
            self.ex.set_sandbox_mode(True)
        self.market = self.ex.load_markets()[symbol]
        self.base, self.quote = self.market["base"], self.market["quote"]
        order_types = set((self.market.get("info") or {}).get("orderTypes") or [])
        self.market_stop = "STOP_LOSS" in order_types

    def price(self) -> float:
        return float(self.ex.fetch_ticker(self.symbol)["last"])

    def balances(self) -> tuple[float, float]:
        b = self.ex.fetch_balance()
        return float(b.get("free", {}).get(self.quote) or 0.0), float(b.get("total", {}).get(self.base) or 0.0)

    def market_order(self, side: str, amount: float) -> dict:
        return self.ex.create_order(self.symbol, "market", side, self.amount_precision(amount))

    def place_stop(self, amount: float, stop_price: float) -> str:
        amount = self.amount_precision(amount)
        stop = float(self.ex.price_to_precision(self.symbol, stop_price))
        if self.market_stop:
            order = self.ex.create_order(self.symbol, "market", "sell", amount, None, {"stopLossPrice": stop})
        else:  # 部分交易对没有市价止损, 用限价止损, 限价比触发价低 2%
            limit = float(self.ex.price_to_precision(self.symbol, stop * 0.98))
            order = self.ex.create_order(self.symbol, "limit", "sell", amount, limit, {"stopLossPrice": stop})
        return str(order["id"])

    def cancel(self, order_id: str) -> None:
        try:
            self.ex.cancel_order(order_id, self.symbol)
        except Exception as exc:  # 已成交或已撤销
            if self.order_info(order_id)["status"] == "open":
                raise exc

    def order_info(self, order_id: str) -> dict:
        o = self.ex.fetch_order(order_id, self.symbol)
        fee = o.get("fee") or {}
        fee_quote = float(fee.get("cost") or 0.0) if fee.get("currency") == self.quote else 0.0
        return {"status": str(o.get("status") or ""), "filled": float(o.get("filled") or 0.0),
                "cost": float(o.get("cost") or 0.0), "fee_quote": fee_quote}

    def amount_precision(self, amount: float) -> float:
        return float(self.ex.amount_to_precision(self.symbol, amount))

    def min_notional(self) -> float:
        cost = (self.market.get("limits") or {}).get("cost") or {}
        return float(cost.get("min") or 5.0)

    def stop_floor(self, price: float) -> float:
        """币安价格保护 (PERCENT_PRICE_BY_SIDE / PERCENT_PRICE): 卖单价格不得低于 均价 × 下限倍数。
        用当前价近似均价, 并留 2% 余量。测试网的限制通常比实盘严。"""
        floor = 0.0
        for f in (self.market.get("info") or {}).get("filters") or []:
            if f.get("filterType") == "PERCENT_PRICE_BY_SIDE":
                floor = max(floor, float(f.get("askMultiplierDown") or 0))
            elif f.get("filterType") == "PERCENT_PRICE":
                floor = max(floor, float(f.get("multiplierDown") or 0))
        return price * floor * 1.02 if floor > 0 else 0.0


# --------------------------------------------------------------------------- 状态
@dataclass
class LiveState:
    symbol: str
    venue: str
    capital: float
    cash: float
    qty: float = 0.0
    last_bar: str | None = None
    stop_order_id: str | None = None
    stop_error: str | None = None  # 最近一次挂止损失败的原因 (相同原因不重复通知)
    stop_price: float | None = None
    halted: str | None = None
    halt_notified_at: str | None = None  # 停机通知最多每 6 小时一次
    fills: list = field(default_factory=list)
    peak_equity: float = 0.0


def _save(path: str, st: LiveState) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(st), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load(path: str) -> LiveState | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return LiveState(**json.load(f))


@dataclass
class LiveConfig:
    symbol: str = "BTC/USDT"
    venue: str = "binance-testnet"
    capital_cap: float = 500.0
    max_order_usdt: float = 500.0
    stop_loss: float | None = 0.20
    threshold: float = 0.1
    dry_run: bool = False
    state_path: str = "~/quantbot-data/live_BTCUSDT_testnet.json"
    kill_switch: str = "~/quantbot-data/STOP"


def fear_greed() -> str | None:
    """加密货币恐惧与贪婪指数 (alternative.me), 仅作参考信息, 不参与交易决策。"""
    import urllib.request

    try:
        req = urllib.request.Request("https://api.alternative.me/fng/?limit=1", headers={"User-Agent": "quantbot"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())["data"][0]
        return f"{d['value']} ({d['value_classification']})"
    except Exception:
        return None


# --------------------------------------------------------------------------- 执行
class LiveTrader:
    def __init__(self, cfg: LiveConfig, ex: SpotExchange, fetcher: Callable[..., pd.DataFrame] | None = None,
                 now: datetime | None = None) -> None:
        self.cfg = cfg
        self.ex = ex
        self.fetcher = fetcher or fetch_binance
        self.now = now or datetime.now(timezone.utc)
        self.path = os.path.expanduser(cfg.state_path)
        self.notes: list[str] = []

    # ---- 交易所交互, 每次都用余额变化记账 ----
    def _apply_balance_delta(self, st: LiveState, before: tuple[float, float], reason: str, price_hint: float) -> dict:
        time.sleep(0.5)
        after = self.ex.balances()
        dq, db = after[0] - before[0], after[1] - before[1]
        st.cash += dq
        st.qty = max(0.0, st.qty + db)
        if abs(st.qty) < 1e-12:
            st.qty = 0.0
        fill = {"time": self.now.isoformat(timespec="seconds"), "base": db, "quote": dq,
                "avg_price": abs(dq / db) if db else price_hint, "reason": reason}
        if abs(db) > 0:
            st.fills.append(fill)
        _save(self.path, st)
        return fill

    def _trade(self, st: LiveState, side: str, amount: float, reason: str, price: float) -> dict | None:
        if side == "sell":  # 只卖机器人自己的币, 且不超过交易所实际余额 (防止精度取整超卖)
            _, base_total = self.ex.balances()
            amount = min(amount, st.qty, base_total)
            amount = self.ex.amount_precision(amount)
            while amount > min(st.qty, base_total) and amount > 0:
                amount = self.ex.amount_precision(amount * (1 - 1e-6))
        amount = self.ex.amount_precision(amount)
        if amount <= 0 or amount * price < self.ex.min_notional():
            self.notes.append(f"下单金额 {amount * price:.2f} USDT 低于交易所最小金额, 跳过")
            return None
        if self.cfg.dry_run:
            self.notes.append(f"[演练] 本应市价{'买入' if side == 'buy' else '卖出'} {amount:.6f} ({reason})")
            return None
        before = self.ex.balances()
        try:
            self.ex.market_order(side, amount)
        finally:  # 即使下单接口报错, 也按真实余额变化记账
            fill = self._apply_balance_delta(st, before, reason, price)
        return fill

    def _cancel_stop(self, st: LiveState) -> None:
        if st.stop_order_id and not self.cfg.dry_run:
            self.ex.cancel(st.stop_order_id)
        st.stop_order_id, st.stop_price = None, None
        _save(self.path, st)

    def _place_stop(self, st: LiveState, ref_close: float, price: float) -> None:
        if not self.cfg.stop_loss or st.qty <= 0 or self.cfg.dry_run:
            return
        stop_price = ref_close * (1 - self.cfg.stop_loss)
        if stop_price >= price * 0.995:
            self.notes.append("⚠️ 当前价格已接近或低于止损价, 本次未挂止损单")
            return
        floor = self.ex.stop_floor(price)
        if stop_price < floor:
            self.notes.append(f"ℹ️ 交易所限制挂单价不能低于 {floor:,.2f}, 止损价由 {stop_price:,.2f} 上调至该价")
            stop_price = floor
            if stop_price >= price * 0.995:
                self.notes.append("⚠️ 交易所价格限制过紧, 本次未挂止损单")
                return
        amount = self.ex.amount_precision(st.qty)
        if amount * stop_price < self.ex.min_notional():
            return
        try:
            st.stop_order_id = self.ex.place_stop(amount, stop_price)
        except Exception as exc:  # 挂止损失败不影响已完成的交易和通知, 下次运行重试
            reason = str(exc)[:200]
            if reason != st.stop_error:
                self.notes.append(f"⚠️ 止损单挂单失败, 下次运行重试: {reason}")
            st.stop_error = reason
            _save(self.path, st)
            return
        if st.stop_error:
            self.notes.append("🛡 止损单已补挂成功")
        st.stop_error = None
        st.stop_price = stop_price
        _save(self.path, st)

    # ---- 主流程 ----
    def run(self) -> str | None:
        cfg = self.cfg
        st = _load(self.path)
        free_quote, base_total = self.ex.balances()
        if st is None:
            capital = min(cfg.capital_cap, free_quote)
            if capital < self.ex.min_notional():
                raise RuntimeError(f"账户可用 USDT 只有 {free_quote:.2f}, 无法开始")
            st = LiveState(cfg.symbol, cfg.venue, capital, capital, peak_equity=capital)
            _save(self.path, st)
            self.notes.append(f"🆕 新建实盘账户: 管理资金 {capital:.2f} USDT (上限 {cfg.capital_cap:.0f})")
        if st.symbol != cfg.symbol or st.venue != cfg.venue:
            raise RuntimeError(f"状态文件属于 {st.symbol}@{st.venue}, 不是 {cfg.symbol}@{cfg.venue}")

        # 1. 止损单是否已触发
        events: list[str] = []
        if st.stop_order_id and not cfg.dry_run:
            info = self.ex.order_info(st.stop_order_id)
            status = info["status"]
            if status == "closed":
                sold = min(info["filled"], st.qty)
                self._record_stop_fill(st, sold, info["cost"] - info["fee_quote"])
                events.append(f"🛑 止损单已触发 (触发价 {st.stop_price:,.2f}), 卖出 {sold:.6f}, 收回 {info['cost']:,.2f} USDT")
                st.stop_order_id, st.stop_price = None, None
                _save(self.path, st)
                free_quote, base_total = self.ex.balances()
            elif status in ("canceled", "cancelled", "expired", "rejected"):
                st.stop_order_id, st.stop_price = None, None
                _save(self.path, st)
                events.append("⚠️ 止损单被撤销, 将重新挂单")

        # 2. 持仓一致性: 交易所上的币少于机器人记录 -> 有人手动卖出或出错, 停机
        if base_total + 1e-9 < st.qty * 0.995 and not cfg.dry_run:
            st.halted = f"交易所余额 {base_total:.6f} 少于机器人持仓 {st.qty:.6f}"
            _save(self.path, st)
        if st.halted:
            last = pd.Timestamp(st.halt_notified_at) if st.halt_notified_at else None
            if last is not None and self.now - last.to_pydatetime() < timedelta(hours=6) and not events:
                return None
            st.halt_notified_at = self.now.isoformat(timespec="seconds")
            _save(self.path, st)
            return self._message(st, None, events + [f"⛔ 已停机: {st.halted}。请检查账户后联系维护者。"], force=True)

        killed = os.path.exists(os.path.expanduser(cfg.kill_switch))

        # 3. 新收盘日线
        df = self.fetcher(cfg.symbol.replace("/", ""), interval="1d",
                          start=(self.now - timedelta(days=500)).strftime("%Y-%m-%d"))
        bar = df.index[-1]
        price = self.ex.price()
        new_bar = st.last_bar is None or bar > pd.Timestamp(st.last_bar)
        decision = None
        if new_bar:
            strat = TrendVolTarget()
            comps = strat.components(df)
            target = float(comps["target"].iloc[-1])
            equity = st.cash + st.qty * price
            current = st.qty * price / equity if equity > 0 else 0.0
            action = "hold"
            if target <= 0 and current > 0.001:
                action = "exit"
            elif target > 0 and abs(target - current) > cfg.threshold:
                action = "buy" if target > current else "sell"
            decision = {"bar": str(bar.date()), "target": target, "current": current, "action": action,
                        "row": comps.iloc[-1], "close": float(df["close"].iloc[-1])}
            if killed:
                events.append("⏸ 紧急停止开关已打开, 本次不下单")
            elif action != "hold":
                self._cancel_stop(st)  # 先撤止损单, 释放被冻结的币
                if action == "exit":
                    fill = self._trade(st, "sell", st.qty, "trend_exit", price)
                else:
                    delta_value = target * equity - st.qty * price
                    if delta_value > 0:
                        fq, _ = self.ex.balances()
                        delta_value = min(delta_value, cfg.max_order_usdt, fq * 0.995, st.cash * 0.998)
                        fill = self._trade(st, "buy", delta_value / price, "trend_buy", price)
                    else:
                        amount = min(-delta_value, cfg.max_order_usdt) / price
                        fill = self._trade(st, "sell", min(amount, st.qty), "trend_sell", price)
                if fill:
                    side = "买入" if fill["base"] > 0 else "卖出"
                    events.append(f"✅ 已市价{side} {abs(fill['base']):.6f} @ 约 {fill['avg_price']:,.2f}")
            st.last_bar = str(bar.date())
            _save(self.path, st)

        # 4. 止损单: 有持仓就保证有一张 (新日线时按新收盘价重挂)
        if not killed and st.qty > 0 and not cfg.dry_run:
            ref = float(df["close"].iloc[-1])
            if new_bar and st.stop_order_id and decision and decision["action"] == "hold":
                self._cancel_stop(st)  # 每天按最新收盘价上移/重挂
            if not st.stop_order_id:
                self._place_stop(st, ref, price)
        equity = st.cash + st.qty * price
        st.peak_equity = max(st.peak_equity, equity)
        _save(self.path, st)
        if decision is None and not events and not self.notes:
            return None
        return self._message(st, decision, events, price=price)

    def _record_stop_fill(self, st: LiveState, sold: float, proceeds: float) -> None:
        """止损单在两次运行之间成交: 按交易所返回的成交数量与金额记账。"""
        st.cash += proceeds
        st.qty = max(0.0, st.qty - sold)
        if st.qty * (st.stop_price or 0) < 1.0:
            st.qty = 0.0
        st.fills.append({"time": self.now.isoformat(timespec="seconds"), "base": -sold, "quote": proceeds,
                         "avg_price": proceeds / sold if sold else st.stop_price, "reason": "stop_loss"})

    def _message(self, st: LiveState, d: dict | None, events: list[str], price: float | None = None,
                 force: bool = False) -> str:
        cfg = self.cfg
        base = cfg.symbol.split("/")[0]
        venue = "币安测试网 (假钱)" if "testnet" in cfg.venue else "币安实盘 (真钱)"
        tag = " [演练模式]" if cfg.dry_run else ""
        lines = [f"🤖 {base}/USDT 自动交易 · {venue}{tag}"]
        if d:
            row = d["row"]
            lbs = [k for k in row.index if str(k).startswith("up_")]
            arrows = " ".join(f"{k[3:]}日{'↑' if row[k] > 0 else '↓'}" for k in lbs)
            lines += [f"{d['bar']} 日线收盘 {d['close']:,.2f}",
                      f"趋势: {arrows} | 波动率 {row['vol'] * 100:.0f}%",
                      f"目标仓位 {d['target'] * 100:.0f}% (调仓前 {d['current'] * 100:.0f}%)"]
        lines += self.notes + events
        if price is not None:
            equity = st.cash + st.qty * price
            ret = equity / st.capital - 1
            lines.append(f"账户: {st.qty:.6f} {base} + {st.cash:,.2f} USDT = {equity:,.2f} ({ret * 100:+.2f}%)")
        if st.stop_price:
            lines.append(f"🛡 交易所止损单: {st.stop_price:,.2f}")
        fg = fear_greed() if d else None
        if fg:
            lines.append(f"市场情绪 (恐惧贪婪指数, 仅参考): {fg}")
        return "\n".join(lines)


def run_live(cfg: LiveConfig, ex: SpotExchange, env_path: str | None, notify: bool = True,
             fetcher: Callable[..., pd.DataFrame] | None = None, sender: Callable[[str, str, str], None] | None = None,
             now: datetime | None = None) -> str | None:
    lock_path = os.path.expanduser(cfg.state_path) + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None  # 另一个实例正在运行
        text = LiveTrader(cfg, ex, fetcher, now).run()
        try:
            os.remove(os.path.expanduser(cfg.state_path) + ".firstfail")
        except FileNotFoundError:
            pass
    if text and notify:
        env = load_env_file(os.path.expanduser(env_path)) if env_path else {}
        try:
            (sender or send_telegram)(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), text)
        except Exception as exc:  # 交易已经完成并保存, 推送失败只记录
            print(f"推送失败: {exc}")
    return text


def flatten(cfg: LiveConfig, ex: SpotExchange) -> str:
    """撤销止损单并卖出机器人持有的全部币 (手动应急用)。"""
    path = os.path.expanduser(cfg.state_path)
    st = _load(path)
    if st is None:
        return "没有实盘状态, 无需平仓。"
    tr = LiveTrader(cfg, ex)
    tr._cancel_stop(st)
    price = ex.price()
    if st.qty > 0:
        tr._trade(st, "sell", st.qty, "manual_flatten", price)
    return f"已平仓。剩余持仓 {st.qty:.6f}, 资金 {st.cash:,.2f} USDT。" + (" " + " ".join(tr.notes) if tr.notes else "")


__all__ = ["CcxtSpot", "LiveConfig", "LiveState", "LiveTrader", "flatten", "run_live"]
