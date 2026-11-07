"""实盘执行器测试: 用模拟交易所检查记账、风控上限、止损单、停机与紧急开关。"""

import os
from datetime import datetime, timezone

import pandas as pd
import pytest

from quantbot.data import generate_synthetic
from quantbot.live import LiveConfig, _load, flatten, run_live
from quantbot.strategies import TrendVolTarget


class FakeExchange:
    """现货交易所: 买入手续费按基础币扣 (与币安默认一致), 卖出按 USDT 扣; 止损单冻结基础币。"""

    def __init__(self, usdt=2000.0, base=0.0, price=100.0, fee=0.001):
        self.usdt, self.base, self.px, self.fee = usdt, base, price, fee
        self.orders, self.stops, self.calls = [], {}, []
        self._n = 0

    def price(self):
        return self.px

    def balances(self):
        return self.usdt, self.base

    def amount_precision(self, a):  # 与 ccxt 默认一致: 向下截断到 6 位
        import math
        return math.floor(a * 1e6 + 1e-9) / 1e6 if a > 0 else 0.0

    def min_notional(self):
        return 5.0

    floor_mult = 0.0
    fail_stops = 0

    def stop_floor(self, price):
        return price * self.floor_mult * 1.02 if self.floor_mult else 0.0

    fail_orders = 0

    def market_order(self, side, amount):
        if self.fail_orders > 0:
            self.fail_orders -= 1
            raise RuntimeError("binance Account has insufficient balance")
        locked = sum(s["amount"] for s in self.stops.values() if s["status"] == "open")
        if side == "buy":
            cost = amount * self.px
            assert cost <= self.usdt + 1e-9, "余额不足"
            self.usdt -= cost
            self.base += amount * (1 - self.fee)
        else:
            assert amount <= self.base - locked + 1e-9, "可用币不足 (被止损单冻结?)"
            self.base -= amount
            self.usdt += amount * self.px * (1 - self.fee)
        self.orders.append((side, amount))
        return {"id": str(len(self.orders))}

    def place_stop(self, amount, stop_price):
        if self.fail_stops > 0:
            self.fail_stops -= 1
            raise RuntimeError("binance Filter failure: PERCENT_PRICE_BY_SIDE")
        self._n += 1
        oid = f"s{self._n}"
        self.stops[oid] = {"amount": amount, "stop": stop_price, "status": "open", "filled": 0.0, "cost": 0.0}
        return oid

    fill_on_cancel = False

    def cancel(self, oid):
        s = self.stops[oid]
        if s["status"] == "open" and self.fill_on_cancel:  # 撤单前一瞬间已成交
            s["status"], s["filled"], s["cost"] = "closed", s["amount"], s["amount"] * s["stop"]
            self.base -= s["amount"]
            self.usdt += s["cost"] * (1 - self.fee)
            return
        if s["status"] == "open":
            s["status"] = "canceled"

    def order_info(self, oid):
        s = self.stops[oid]
        return {"status": s["status"], "filled": s["filled"], "cost": s["cost"], "fee_quote": s["cost"] * self.fee}

    def crash_to(self, low):
        """盘中价格跌到 low: 触发所有止损单。"""
        for s in self.stops.values():
            if s["status"] == "open" and low <= s["stop"]:
                s["status"] = "closed"
                s["filled"] = s["amount"]
                s["cost"] = s["amount"] * s["stop"]
                self.base -= s["amount"]
                self.usdt += s["cost"] * (1 - self.fee)
        self.px = low


def _bars_with_target(min_target=0.3):
    df = generate_synthetic(n=600, seed=21)
    t = TrendVolTarget().generate_signals(df)
    i = next(k for k in range(200, 600) if t.iloc[k] > min_target)
    return df, i


def _fetcher(df):
    return lambda symbol, interval="1d", start=None: df


def _cfg(tmp_path, **kw):
    base = dict(state_path=str(tmp_path / "live.json"), kill_switch=str(tmp_path / "STOP"))
    base.update(kw)
    return LiveConfig(**base)


def test_first_run_caps_capital_buys_target_and_places_stop(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(usdt=2000, price=float(df["close"].iloc[i]))
    text = run_live(_cfg(tmp_path), ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st = _load(str(tmp_path / "live.json"))
    assert st.capital == 500  # 账户有 2000, 机器人只管 500
    target = float(TrendVolTarget().generate_signals(df).iloc[i])
    spent = 2000 - ex.usdt
    assert spent == pytest.approx(target * 500, rel=0.01)
    assert st.qty == pytest.approx(ex.base)
    assert st.stop_order_id and st.stop_price == pytest.approx(float(df["close"].iloc[i]) * 0.8)
    assert "已市价买入" in text and "止损单" in text


def test_same_bar_rerun_does_not_trade_again(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    cfg = _cfg(tmp_path)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    n = len(ex.orders)
    assert run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None) is None
    assert len(ex.orders) == n


def test_stop_trigger_is_detected_and_booked(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    cfg = _cfg(tmp_path)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st = _load(cfg.state_path)
    ex.crash_to(st.stop_price * 0.97)
    text = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st2 = _load(cfg.state_path)
    assert "止损单已触发" in text
    assert st2.qty == 0 and st2.stop_order_id is None
    assert st2.cash == pytest.approx(ex.usdt - 1500, rel=1e-6)  # 机器人的 USDT = 账户 USDT − 不归它管的 1500


def test_never_sells_coins_it_does_not_own_and_halts_on_mismatch(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(usdt=600, base=3.0, price=float(df["close"].iloc[i]))  # 用户自己有 3 个币
    cfg = _cfg(tmp_path, stop_loss=None)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st = _load(cfg.state_path)
    assert ex.base == pytest.approx(3.0 + st.qty)
    flatten(cfg, ex)
    assert ex.base == pytest.approx(3.0, abs=1e-6)  # 平仓只卖机器人自己的
    # 账户里的币被手动转走 -> 停机且不再下单
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 2]), sender=lambda *a: None)
    ex.base = 0.0
    st = _load(cfg.state_path)
    if st.qty > 0:
        n = len(ex.orders)
        text = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 3]), sender=lambda *a: None)
        assert "已停机" in text and len(ex.orders) == n


def test_kill_switch_and_dry_run_place_no_orders(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    (tmp_path / "STOP").write_text("")
    text = run_live(_cfg(tmp_path), ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    assert ex.orders == [] and ex.stops == {} and "紧急停止" in text
    os.remove(tmp_path / "STOP")
    ex2 = FakeExchange(price=float(df["close"].iloc[i]))
    text = run_live(_cfg(tmp_path, state_path=str(tmp_path / "d.json"), dry_run=True), ex2, None,
                    fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    assert ex2.orders == [] and "[演练]" in text


def test_max_order_cap_limits_each_buy(tmp_path):
    df, i = _bars_with_target(0.5)
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    run_live(_cfg(tmp_path, max_order_usdt=50), ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    assert 2000 - ex.usdt <= 50 + 1e-6


def test_follows_strategy_through_many_days(tmp_path):
    """连续 120 天逐日运行: 持仓始终 ≥0, 资金不超过上限, 每天都有止损单保护持仓。"""
    df = generate_synthetic(n=700, seed=4)
    cfg = _cfg(tmp_path)
    ex = FakeExchange(usdt=800)
    for k in range(500, 620):
        ex.px = float(df["open"].iloc[k + 1])  # 收盘后约等于下一根开盘
        run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: k + 1]), sender=lambda *a: None)
        st = _load(cfg.state_path)
        assert st.qty >= 0 and st.qty <= ex.base + 1e-9
        assert ex.usdt >= 300 - 1e-6  # 不归机器人管的 300 USDT 从未被动用
        if st.qty * ex.px >= 5.0:  # 低于交易所最小金额的 "粉尘" 无法也无需挂止损
            assert st.stop_order_id is not None


def test_halt_notification_is_rate_limited(tmp_path):
    from datetime import timedelta

    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    cfg = _cfg(tmp_path, stop_loss=None)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    ex.base = 0.0  # 币被转走 -> 停机
    t0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    first = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None, now=t0)
    again = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None,
                     now=t0 + timedelta(hours=1))
    later = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None,
                     now=t0 + timedelta(hours=7))
    assert "已停机" in first and again is None and "已停机" in later


def test_stop_is_clamped_to_exchange_price_band(tmp_path):
    df, i = _bars_with_target()
    px = float(df["close"].iloc[i])
    ex = FakeExchange(price=px)
    ex.floor_mult = 0.85  # 测试网: 卖单价不得低于 85% 均价
    text = run_live(_cfg(tmp_path), ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st = _load(str(tmp_path / "live.json"))
    assert st.stop_price == pytest.approx(px * 0.85 * 1.02)
    assert "上调" in text


def test_stop_failure_still_notifies_and_retries_next_run(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    ex.fail_stops = 1
    cfg = _cfg(tmp_path)
    text = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st = _load(cfg.state_path)
    assert "已市价买入" in text and "止损单挂单失败" in text
    assert st.qty > 0 and st.stop_order_id is None
    text2 = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st2 = _load(cfg.state_path)
    assert st2.stop_order_id is not None and "补挂成功" in text2


def _first_bar_where(pred, start=200):
    df = generate_synthetic(n=600, seed=21)
    t = TrendVolTarget().generate_signals(df)
    return df, t, next(k for k in range(start, 599) if pred(t, k))


def test_stop_filled_just_before_cancel_is_booked(tmp_path):
    df, t, i = _first_bar_where(lambda t, k: t.iloc[k] > 0.3)
    j = next(k for k in range(i + 1, 599) if t.iloc[k] <= 0 or abs(t.iloc[k] - t.iloc[i]) > 0.15)
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    cfg = _cfg(tmp_path)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    for k in range(i + 1, j):
        run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: k + 1]), sender=lambda *a: None)
    ex.fill_on_cancel = True
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: j + 1]), sender=lambda *a: None)
    st = _load(cfg.state_path)
    assert st.halted is None  # 记账正确, 不会误判为对不上
    assert ex.base == pytest.approx(st.base_baseline + st.qty, abs=1e-6)


def test_extra_coins_appearing_also_halts(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    cfg = _cfg(tmp_path, stop_loss=None)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    ex.base += 0.5  # 成交记录丢失 / 手动买入
    n = len(ex.orders)
    text = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 2]), sender=lambda *a: None)
    assert "已停机" in text and "相差 +0.5" in text and len(ex.orders) == n


def test_order_failure_notifies_keeps_stop_and_retries(tmp_path):
    df, i = _bars_with_target()
    ex = FakeExchange(price=float(df["close"].iloc[i]))
    ex.fail_orders = 1
    cfg = _cfg(tmp_path)
    text = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st = _load(cfg.state_path)
    assert "下单失败" in text and st.last_bar is None and ex.orders == []
    text2 = run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    st2 = _load(cfg.state_path)
    assert "已市价买入" in text2 and st2.last_bar is not None and st2.stop_order_id


def test_old_state_without_baseline_is_upgraded(tmp_path):
    import json

    df, i = _bars_with_target()
    ex = FakeExchange(base=1.0, price=float(df["close"].iloc[i]))
    cfg = _cfg(tmp_path, stop_loss=None)
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 1]), sender=lambda *a: None)
    data = json.loads(open(cfg.state_path).read())
    data.pop("base_baseline")
    data.pop("trade_error")
    open(cfg.state_path, "w").write(json.dumps(data))
    run_live(cfg, ex, None, fetcher=_fetcher(df.iloc[: i + 2]), sender=lambda *a: None)
    st = _load(cfg.state_path)
    assert st.halted is None and st.base_baseline == pytest.approx(1.0, abs=1e-6)
