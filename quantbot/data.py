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


BINANCE_DATA_URL = "https://data-api.binance.vision/api/v3/klines"
RETRY_DELAYS = (5.0, 20.0)  # 网络临时故障 (如 DNS 解析失败) 时的重试间隔, 秒


def urlopen_retry(req, timeout: float, delays: tuple[float, ...] | None = None) -> bytes:
    """读取 URL; 遇到网络错误、超时、429 或 5xx 时按 delays 重试, 其他 HTTP 错误直接抛出。"""
    import time
    import urllib.error
    import urllib.request

    delays = RETRY_DELAYS if delays is None else delays
    for attempt in range(len(delays) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == len(delays):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == len(delays):
                raise
        time.sleep(delays[attempt])
    raise RuntimeError("unreachable")


def fetch_binance(
    symbol: str = "BTCUSDT",
    interval: str = "1d",
    start: str = "2017-01-01",
    end: str | None = None,
    base_url: str = BINANCE_DATA_URL,
    cache_dir: str | None = None,
) -> pd.DataFrame:
    """从 Binance 公开行情接口分页下载现货 K 线 (无需 API Key, 无需 ccxt)。

    只返回已收盘的 K 线。设置 cache_dir 时会把结果缓存为 CSV, 再次调用直接读取。
    """
    import json
    import os
    import time
    import urllib.parse
    import urllib.request

    symbol = symbol.replace("/", "").upper()
    if cache_dir:
        path = os.path.join(cache_dir, f"binance_{symbol}_{interval}_{start}_{end or 'latest'}.csv")
        if os.path.exists(path):
            return load_csv(path)

    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None
    now_ms = int(time.time() * 1000)
    rows: list = []
    while True:
        query = {"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": 1000}
        if end_ms:
            query["endTime"] = end_ms
        url = f"{base_url}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "quantbot"})
        batch = json.loads(urlopen_retry(req, timeout=30))
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        start_ms = batch[-1][0] + 1
    df = parse_binance_klines(rows, now_ms)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        df.reset_index().to_csv(path, index=False)
    return df


def parse_binance_klines(rows: list, now_ms: int | None = None) -> pd.DataFrame:
    """解析 Binance K 线数组; 丢弃收盘时间晚于 now_ms 的未完成 K 线。"""
    if now_ms is not None:
        rows = [r for r in rows if int(r[6]) < now_ms]
    df = pd.DataFrame(
        [r[:6] for r in rows], columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    return normalize(df)


BINANCE_ARCHIVE_URL = "https://data.binance.vision/data/futures/um"


def _archive_csv_rows(url: str) -> list[list[str]] | None:
    """下载 data.binance.vision 的单个 zip, 返回 CSV 行 (去掉表头); 404 返回 None。"""
    import csv
    import io
    import urllib.error
    import urllib.request
    import zipfile

    req = urllib.request.Request(url, headers={"User-Agent": "quantbot"})
    try:
        payload = urlopen_retry(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0].strip().lstrip("-").isdigit():
        rows = rows[1:]  # 新格式带表头
    return rows


def fetch_binance_futures_archive(
    symbol: str = "BTCUSDT",
    start: str = "2019-09",
    cache_dir: str | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """从 Binance 官方历史数据站下载 USDT 本位永续合约日线与资金费率。

    返回 (日线 OHLCV, 按 UTC 日期汇总的资金费率之和)。K 线使用月度归档加当月日度归档;
    资金费率只有月度归档, 当月尚未归档的天数不包含在内。
    """
    import os

    symbol = symbol.replace("/", "").upper()
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    if cache_dir:
        kpath = os.path.join(cache_dir, f"binance_um_{symbol}_1d_{today.date()}.csv")
        fpath = os.path.join(cache_dir, f"binance_um_{symbol}_funding_{today.date()}.csv")
        if os.path.exists(kpath) and os.path.exists(fpath):
            fund = pd.read_csv(fpath, index_col=0, parse_dates=True).iloc[:, 0]
            return load_csv(kpath), fund

    months = pd.period_range(pd.Period(start, "M"), (today - pd.offsets.MonthBegin(1)).to_period("M"), freq="M")
    krows: list = []
    frows: list = []
    for m in months:
        tag = f"{m.year}-{m.month:02d}"
        rows = _archive_csv_rows(f"{BINANCE_ARCHIVE_URL}/monthly/klines/{symbol}/1d/{symbol}-1d-{tag}.zip")
        krows += rows or []
        rows = _archive_csv_rows(f"{BINANCE_ARCHIVE_URL}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{tag}.zip")
        frows += rows or []
    for day in pd.date_range(today.replace(day=1), today - pd.Timedelta(days=1), freq="D"):
        rows = _archive_csv_rows(
            f"{BINANCE_ARCHIVE_URL}/daily/klines/{symbol}/1d/{symbol}-1d-{day.date()}.zip"
        )
        krows += rows or []
    if not krows:
        raise ValueError(f"{symbol}: 历史数据站没有永续合约日线")
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    df = parse_binance_klines([[int(r[0]), *r[1:6], int(r[6])] for r in krows], now_ms)
    fund = pd.Series(dtype=float)
    if frows:
        f = pd.DataFrame({"t": [int(r[0]) for r in frows], "rate": [float(r[2]) for r in frows]})
        f["day"] = pd.to_datetime(f["t"], unit="ms").dt.normalize()
        fund = f.groupby("day")["rate"].sum()
    fund.name = "funding_rate"
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        df.reset_index().to_csv(kpath, index=False)
        fund.to_csv(fpath)
    return df, fund


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
    - "binance:BTCUSDT"           Binance 公开行情 (无需 ccxt), 可选 timeframe=
    """
    if source.startswith("binance:"):
        return fetch_binance(source[8:], interval=kwargs.get("timeframe", "1d"))
    if source == "synthetic":
        return generate_synthetic(**{k: v for k, v in kwargs.items() if k in ("n", "seed")})
    if source.startswith("ccxt:"):
        return fetch_ccxt(source[5:], **{k: v for k, v in kwargs.items() if k in ("timeframe", "limit", "exchange")})
    if source.startswith("yf:"):
        return fetch_yfinance(source[3:])
    return load_csv(source)
