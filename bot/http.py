"""共享 HTTP 客户端：限流、超时、429/5xx 自动重试。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import aiohttp

from .utils import RateLimiter

log = logging.getLogger("memebot.http")


class ApiError(Exception):
    def __init__(self, service: str, status: int, body: str):
        super().__init__(f"{service} HTTP {status}: {body[:300]}")
        self.service, self.status, self.body = service, status, body


class PlanError(ApiError):
    """401/403：key 无效或当前套餐不包含这个接口。不要重试。"""


class QuotaError(ApiError):
    """429 + "max usage reached"：套餐额度用完了，要等计费周期重置或升级。不要重试。"""


class Http:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._limiters: dict[str, RateLimiter] = {}

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=8),
                headers={"User-Agent": "memebot/1.0", "Accept": "application/json"},
                connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def set_rate(self, service: str, rps: float) -> None:
        self._limiters[service] = RateLimiter(rps)

    async def request(self, service: str, method: str, url: str, *, params: dict | None = None,
                      json_body: Any = None, headers: dict | None = None, retries: int = 4,
                      timeout: float | None = None, raw: bool = False, binary: bool = False) -> Any:
        await self.start()
        assert self._session is not None
        lim = self._limiters.get(service)
        if params:
            params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            if lim:
                await lim.acquire()
            try:
                kw: dict[str, Any] = {"params": params, "headers": headers}
                if json_body is not None:
                    kw["json"] = json_body
                if timeout:
                    kw["timeout"] = aiohttp.ClientTimeout(total=timeout)
                async with self._session.request(method, url, **kw) as resp:
                    if binary and resp.status == 200:
                        return await resp.read()
                    text = await resp.text(errors="replace")
                    if resp.status == 429 and "max usage" in text.lower():
                        # 这个月的额度用完了（Helius 等）：不是一时的限流，重试没用，马上报错让上层走兜底
                        raise QuotaError(service, resp.status, text)
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() \
                            else min(2 ** attempt + random.random(), 30)
                        last_exc = ApiError(service, resp.status, text)
                        log.debug("%s %s -> %s, %.1fs 后重试", service, url, resp.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status in (401, 403):
                        raise PlanError(service, resp.status, text)
                    if resp.status >= 400:
                        raise ApiError(service, resp.status, text)
                    if raw:
                        return text
                    try:
                        return json.loads(text) if text else None
                    except json.JSONDecodeError as e:
                        raise ApiError(service, resp.status, f"非JSON响应: {text[:200]}") from e
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                await asyncio.sleep(min(2 ** attempt + random.random(), 20))
        raise last_exc or RuntimeError(f"{service} 请求失败")

    async def get(self, service: str, url: str, **kw: Any) -> Any:
        return await self.request(service, "GET", url, **kw)

    async def post(self, service: str, url: str, **kw: Any) -> Any:
        return await self.request(service, "POST", url, **kw)
