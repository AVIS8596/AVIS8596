"""通用工具：日志、限流、去重、数字格式化、任务守护。"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

log = logging.getLogger("memebot")


# ------------------------------------------------------------------ 日志
class _SecretFilter(logging.Filter):
    """把日志里出现的 API key / 私钥替换成 ***，防止泄露。"""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        changed = False
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, "***")
                changed = True
        if changed:
            record.msg, record.args = msg, ()
        return True


def setup_logging(log_dir: Path, level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    flt = _SecretFilter(secrets)
    fh = RotatingFileHandler(log_dir / "memebot.log", maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(flt)
        root.addHandler(h)
    for noisy in ("websockets", "aiohttp.access", "web3", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ 限流
class RateLimiter:
    """令牌桶：每秒最多 rate 次。"""

    def __init__(self, rate: float, burst: int | None = None):
        self.rate = max(rate, 0.01)
        self.capacity = burst or max(1, int(math.ceil(rate)))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


# ------------------------------------------------------------------ 去重
class TTLSet:
    """带过期时间的集合，用于交易签名去重（断线重连会重复推送）。"""

    def __init__(self, ttl_sec: float = 3600, max_items: int = 200_000):
        self.ttl = ttl_sec
        self.max = max_items
        self._d: OrderedDict[str, float] = OrderedDict()

    def add_if_new(self, key: str) -> bool:
        now = time.time()
        while self._d:
            k, t = next(iter(self._d.items()))
            if now - t > self.ttl or len(self._d) > self.max:
                self._d.popitem(last=False)
            else:
                break
        if key in self._d:
            return False
        self._d[key] = now
        return True


# ------------------------------------------------------------------ 数字
def f(x: Any, default: float = 0.0) -> float:
    """安全转 float：None / 空串 / 非法值返回 default。"""
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def dig(d: Any, *paths: str, default: Any = None) -> Any:
    """按多个候选路径取值（接口返回字段名有时不一致）。路径用点号分隔。"""
    for p in paths:
        cur = d
        ok = True
        for part in p.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def as_list(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("items", "list", "tokens", "data", "results"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def fmt_usd(v: float) -> str:
    v = f(v)
    a = abs(v)
    if a >= 1e9:
        s = f"{a/1e9:.2f}B"
    elif a >= 1e6:
        s = f"{a/1e6:.2f}M"
    elif a >= 1e3:
        s = f"{a/1e3:.1f}K"
    else:
        s = f"{a:.2f}"
    return ("-$" if v < 0 else "$") + s


def fmt_price(v: float) -> str:
    v = f(v)
    if v == 0:
        return "0"
    if v >= 1:
        return f"{v:.4f}"
    s = f"{v:.12f}".rstrip("0")
    m = re.match(r"0\.(0+)(\d{1,4})", s)
    if m and len(m.group(1)) >= 4:
        return f"0.0{{{len(m.group(1))}}}{m.group(2)}"
    return f"{v:.8f}".rstrip("0")


def short(addr: str, n: int = 4) -> str:
    return addr if not addr or len(addr) <= 2 * n + 3 else f"{addr[:n]}…{addr[-n:]}"


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ 任务守护
async def run_forever(name: str, factory: Callable[[], Awaitable[Any]],
                      on_error: Callable[[str, BaseException], Awaitable[None]] | None = None,
                      min_backoff: float = 2, max_backoff: float = 120) -> None:
    """任务挂了自动重启（指数退避），不会因为一个模块报错拖垮整个机器人。"""
    backoff = min_backoff
    while True:
        started = time.monotonic()
        try:
            await factory()
            log.warning("任务 %s 退出，%.0fs 后重启", name, backoff)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("任务 %s 异常: %s", name, e)
            if on_error:
                try:
                    await on_error(name, e)
                except Exception:  # noqa: BLE001
                    pass
        if time.monotonic() - started > 300:
            backoff = min_backoff  # 稳定运行过一段时间，重置退避
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
