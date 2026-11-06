"""行情数据加载。统一输出: DatetimeIndex + 小写列 open/high/low/close/volume。"""

from __future__ import annotations

import numpy as np
import pandas as pd

_ALIASES = {
    "date": "timestamp",
    "datetime": "timestamp",
    "time": "timestamp",
    "日期": "timestamp",
    "时间": "timestamp",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "adj close": "adj_close",
    "vol": "volume",
}


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [_ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in df.columns]
    if "timestamp" in df.columns:
        ts = df["timestamp"]
        if pd.api.types.is_numeric_dtype(ts):
            unit = "ms" if ts.max() > 1e11 else "s"
            df["timestamp"] = pd.to_datetime(ts, unit=unit)
        else:
            df["timestamp"] = pd.to_datetime(ts)
        df = df.set_index("timestamp")
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if "close" not in df.columns:
        raise ValueError(f"数据缺少 close 列, 现有列: {list(df.columns)}")
    for col in ("open", "high", "low"):
        if col not in df.columns:
            df[col] = df["close"]
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["close"])


def load_csv(path: str) -> pd.DataFrame:
    return normalize(pd.read_csv(path))


def generate_synthetic(
    n: int = 1500,
    seed: int | None = 42,
    start_price: float = 100.0,
    start: str = "2020-01-01",
    freq: str = "D",
    annual_vol: float = 0.35,
) -> pd.DataFrame:
    """生成带趋势/震荡状态切换的合成 OHLCV 数据 (用于演示与测试)。"""
    rng = np.random.default_rng(seed)
    dt = 1 / 365
    sigma = annual_vol * np.sqrt(dt)
    drifts = np.array([0.8, -0.6, 0.0]) * dt  # 上涨 / 下跌 / 震荡
    trans = np.array([[0.985, 0.005, 0.01], [0.01, 0.975, 0.015], [0.01, 0.01, 0.98]])
    state = 2
    rets = np.empty(n)
    for i in range(n):
        state = rng.choice(3, p=trans[state])
        rets[i] = drifts[state] - 0.5 * sigma**2 + sigma * rng.standard_normal()
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]]) * np.exp(rng.normal(0, sigma * 0.2, n))
    wick = np.abs(rng.normal(0, sigma * 0.6, (2, n)))
    high = np.maximum(open_, close) * np.exp(wick[0])
    low = np.minimum(open_, close) * np.exp(-wick[1])
    volume = rng.lognormal(10, 0.5, n)
    idx = pd.date_range(start=start, periods=n, freq=freq, name="timestamp")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def fetch_ccxt(
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
    limit: int = 1000,
    exchange: str = "binance",
    drop_incomplete: bool = True,
) -> pd.DataFrame:
    """通过 ccxt 获取交易所 K 线 (需 `pip install ccxt`)。默认丢弃尚未收盘的最后一根 K 线。"""
    try:
        import ccxt  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError("需要安装 ccxt: pip install ccxt") from e
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = normalize(df)
    if drop_incomplete and len(df) > 1:
        tf_ms = ex.parse_timeframe(timeframe) * 1000
        last_open_ms = df.index[-1].value // 1_000_000
        if last_open_ms + tf_ms > ex.milliseconds():
            df = df.iloc[:-1]
    return df


def fetch_yfinance(ticker: str, period: str = "5y", interval: str = "1d") -> pd.DataFrame:
    """通过 yfinance 获取股票数据 (需 `pip install yfinance`)。"""
    try:
        import yfinance as yf  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError("需要安装 yfinance: pip install yfinance") from e
    df = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return normalize(df)


def load(source: str, **kwargs) -> pd.DataFrame:
    """统一入口:
    - "synthetic"                 合成数据
    - "path/to/file.csv"          本地 CSV
    - "ccxt:BTC/USDT"             交易所数据, 可选 exchange= timeframe= limit=
    - "yf:AAPL"                   yfinance 股票数据
    """
    if source == "synthetic":
        return generate_synthetic(**{k: v for k, v in kwargs.items() if k in ("n", "seed")})
    if source.startswith("ccxt:"):
        return fetch_ccxt(source[5:], **{k: v for k, v in kwargs.items() if k in ("timeframe", "limit", "exchange")})
    if source.startswith("yf:"):
        return fetch_yfinance(source[3:])
    return load_csv(source)
