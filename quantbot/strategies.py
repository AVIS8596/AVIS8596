"""交易策略。

每个策略实现 ``generate_signals(df)``, 返回与 ``df`` 同索引的目标仓位序列,
取值范围 [-1, 1]: 1 = 满仓做多, 0 = 空仓, -1 = 满仓做空。

约定: 第 t 根 K 线的信号只能使用截至第 t 根 K 线收盘的数据。
回测引擎会在第 t+1 根 K 线开盘时执行该信号, 因此不存在未来函数。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from . import indicators as ind


class Strategy(ABC):
    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    default_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: 未知参数 {sorted(unknown)}; 可用参数 {sorted(self.default_params)}")
        self.params: dict[str, Any] = {**self.default_params, **params}
        self.validate()

    def validate(self) -> None:
        """子类可覆盖以校验参数。"""

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        ...

    @property
    def warmup(self) -> int:
        """计算指标所需的最少 K 线数量。"""
        return max([v for v in self.params.values() if isinstance(v, int) and not isinstance(v, bool)] or [1]) + 1

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{type(self).__name__}({args})"

    # ---- 工具函数 ----
    @staticmethod
    def _state_machine(
        index: pd.Index,
        long_entry: pd.Series,
        long_exit: pd.Series,
        short_entry: pd.Series | None = None,
        short_exit: pd.Series | None = None,
    ) -> pd.Series:
        """根据进出场条件生成持仓状态 (1 / 0 / -1)。"""
        le = long_entry.fillna(False).to_numpy(bool)
        lx = long_exit.fillna(False).to_numpy(bool)
        se = short_entry.fillna(False).to_numpy(bool) if short_entry is not None else np.zeros(len(index), bool)
        sx = short_exit.fillna(False).to_numpy(bool) if short_exit is not None else np.zeros(len(index), bool)
        out = np.zeros(len(index))
        state = 0.0
        for i in range(len(index)):
            if state > 0 and lx[i]:
                state = 0.0
            elif state < 0 and sx[i]:
                state = 0.0
            if state == 0:
                if le[i]:
                    state = 1.0
                elif se[i]:
                    state = -1.0
            elif state > 0 and se[i]:
                state = -1.0
            elif state < 0 and le[i]:
                state = 1.0
            out[i] = state
        return pd.Series(out, index=index)


class BuyAndHold(Strategy):
    name = "buy_hold"
    description = "买入并持有 (基准)"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=df.index)


class SmaCross(Strategy):
    name = "sma_cross"
    description = "双均线交叉: 快线上穿慢线做多, 下穿平仓 (可选做空)"
    default_params = {"fast": 20, "slow": 50, "allow_short": False}

    def validate(self) -> None:
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("fast 必须小于 slow")

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = ind.sma(df["close"], self.params["fast"])
        slow = ind.sma(df["close"], self.params["slow"])
        short_val = -1.0 if self.params["allow_short"] else 0.0
        sig = np.where(fast > slow, 1.0, short_val)
        sig = np.where(slow.isna(), 0.0, sig)
        return pd.Series(sig, index=df.index)


class EmaCross(SmaCross):
    name = "ema_cross"
    description = "双指数均线交叉"
    default_params = {"fast": 12, "slow": 26, "allow_short": False}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = ind.ema(df["close"], self.params["fast"])
        slow = ind.ema(df["close"], self.params["slow"])
        short_val = -1.0 if self.params["allow_short"] else 0.0
        sig = np.where(fast > slow, 1.0, short_val)
        sig = np.where(slow.isna(), 0.0, sig)
        return pd.Series(sig, index=df.index)


class RsiReversion(Strategy):
    name = "rsi_reversion"
    description = "RSI 均值回归: RSI 低于超卖线买入, 回到中枢平仓"
    default_params = {"period": 14, "lower": 30.0, "upper": 70.0, "exit": 50.0, "allow_short": False}

    def validate(self) -> None:
        p = self.params
        if not p["lower"] < p["exit"] < p["upper"]:
            raise ValueError("需要 lower < exit < upper")

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        r = ind.rsi(df["close"], p["period"])
        short = p["allow_short"]
        return self._state_machine(
            df.index,
            long_entry=r < p["lower"],
            long_exit=r > p["exit"],
            short_entry=(r > p["upper"]) if short else None,
            short_exit=(r < p["exit"]) if short else None,
        )


class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    description = "布林带均值回归: 收盘跌破下轨买入, 回到中轨平仓"
    default_params = {"period": 20, "num_std": 2.0, "allow_short": False}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        mid, upper, lower = ind.bollinger(df["close"], p["period"], p["num_std"])
        c = df["close"]
        short = p["allow_short"]
        return self._state_machine(
            df.index,
            long_entry=c < lower,
            long_exit=c >= mid,
            short_entry=(c > upper) if short else None,
            short_exit=(c <= mid) if short else None,
        )


class MacdMomentum(Strategy):
    name = "macd"
    description = "MACD 动量: 柱状图为正做多"
    default_params = {"fast": 12, "slow": 26, "signal": 9, "allow_short": False}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        _, _, hist = ind.macd(df["close"], p["fast"], p["slow"], p["signal"])
        short_val = -1.0 if p["allow_short"] else 0.0
        sig = np.where(hist > 0, 1.0, short_val)
        sig = np.where(hist.isna(), 0.0, sig)
        return pd.Series(sig, index=df.index)


class DonchianBreakout(Strategy):
    name = "donchian"
    description = "唐奇安通道突破 (海龟式趋势跟踪): 突破 N 日高点做多, 跌破 M 日低点平仓"
    default_params = {"entry": 20, "exit": 10, "allow_short": False}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        up_in, lo_in = ind.donchian(df, p["entry"])
        up_out, lo_out = ind.donchian(df, p["exit"])
        c = df["close"]
        short = p["allow_short"]
        return self._state_machine(
            df.index,
            long_entry=c > up_in,
            long_exit=c < lo_out,
            short_entry=(c < lo_in) if short else None,
            short_exit=(c > up_out) if short else None,
        )


class TrendVolTarget(Strategy):
    """多周期趋势 + 波动率目标仓位 (只做多), 与 QuantDinger 版
    integrations/quantdinger/trend_vol_target_v2.py 的目标仓位计算完全一致。

    趋势得分 = 三个回看周期中 "收盘价高于 N 根之前收盘价" 的比例 (0, 1/3, 2/3, 1);
    目标仓位 = 得分 × min(max_weight, target_vol / 近 vol_window 日年化波动率)。
    调仓阈值属于执行规则, 在 quantbot.signal_job 中处理。
    """

    name = "trend_vol"
    description = "多周期趋势 + 波动率目标仓位 (只做多, 输出连续目标仓位)"
    default_params = {
        "short_lookback": 20,
        "mid_lookback": 60,
        "long_lookback": 120,
        "vol_window": 30,
        "target_vol": 0.5,
        "max_weight": 0.95,
    }

    def components(self, df: pd.DataFrame) -> pd.DataFrame:
        """逐根 K 线的中间结果: 各周期投票、得分、年化波动率、仓位上限和目标仓位。"""
        p = self.params
        close = df["close"].astype(float)
        lookbacks = [p["short_lookback"], p["mid_lookback"], p["long_lookback"]]
        out = pd.DataFrame(index=df.index)
        for lb in lookbacks:
            past = close.shift(lb)
            out[f"up_{lb}"] = ((past > 0) & (close > past)).astype(float)
        out["score"] = out[[f"up_{lb}" for lb in lookbacks]].sum(axis=1) / len(lookbacks)
        vol = np.log(close / close.shift(1)).rolling(p["vol_window"], min_periods=p["vol_window"]).std() * np.sqrt(365)
        out["vol"] = vol
        size = np.minimum(p["max_weight"], p["target_vol"] / vol.where(vol > 0))
        out["size"] = size.fillna(0.0)
        need = max(max(lookbacks), p["vol_window"]) + 1
        ready = pd.Series(np.arange(len(df)) >= need - 1, index=df.index)
        out["target"] = (out["score"] * out["size"]).where(ready, 0.0)
        return out

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        return self.components(df)["target"]


class Ensemble(Strategy):
    """多个策略信号的平均 (投票), 输出 [-1, 1] 之间的连续仓位。"""

    name = "ensemble"
    description = "组合策略: 对多个子策略信号取平均"
    default_params = {"members": ["sma_cross", "macd", "donchian"]}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        sigs = [get_strategy(m).generate_signals(df) for m in self.params["members"]]
        return pd.concat(sigs, axis=1).mean(axis=1)

    @property
    def warmup(self) -> int:
        return max(get_strategy(m).warmup for m in self.params["members"])


STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (
        BuyAndHold,
        SmaCross,
        EmaCross,
        RsiReversion,
        BollingerReversion,
        MacdMomentum,
        DonchianBreakout,
        TrendVolTarget,
        Ensemble,
    )
}


def get_strategy(name: str, **params: Any) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"未知策略 '{name}', 可选: {', '.join(STRATEGIES)}") from None
    return cls(**params)
