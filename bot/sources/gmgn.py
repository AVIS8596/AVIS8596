"""GMGN OpenAPI 客户端（https://openapi.gmgn.ai）。

鉴权方式照搬官方 gmgn-cli 源码：
  普通接口：Header X-APIKEY + query 参数 timestamp（秒）+ client_id（UUID）
  交易接口：再加 Header X-Signature = 用你的 Ed25519 私钥签名
           签名原文 = "{路径}:{按 key 排序的 query 串}:{请求体}:{timestamp}"

限流：GMGN 按套餐用"漏桶"限流，每个接口有权重；触发后会封禁 5 分钟，
封禁期间继续请求会延长封禁。所以这里一旦收到限流/封禁，所有 GMGN 请求统一暂停到解封时间。
GMGN 只支持 IPv4，这里强制用 IPv4 连接。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import time
import uuid
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

log = logging.getLogger("memebot.gmgn")
HOST = "https://openapi.gmgn.ai"
CHAIN_MAP = {"solana": "sol", "bsc": "bsc", "ethereum": "eth", "base": "base"}
PLAN_RATE = {"free": 5, "plus": 20, "pro": 50}

# 各接口权重（来自官方 gmgn-cli 1.7.1 文档）
WEIGHTS = {
    "/v1/token/info": 1, "/v1/token/security": 1, "/v1/token/pool_info": 1,
    "/v1/market/token_top_holders": 5, "/v1/market/token_top_traders": 5,
    "/v1/market/token_kline": 2, "/v1/market/rank": 1, "/v1/trenches": 3,
    "/v1/user/smartmoney": 1, "/v1/user/kol": 1, "/v1/user/wallet_stats": 3,
    "/v1/user/info": 2, "/v1/user/created_tokens": 3,
    "/v1/trade/swap": 10, "/v1/trade/query_order": 5, "/v1/trade/gas_price": 1, "/v1/trade/quote": 10,
}
SIGNED = {"/v1/trade/swap", "/v1/trade/query_order", "/v1/trade/multi_swap",
          "/v1/trade/strategy/create", "/v1/trade/strategy/cancel", "/v1/trade/strategy/orders",
          "/v1/user/wallet_holdings", "/v1/trade/follow_wallet"}


class GmgnError(Exception):
    def __init__(self, path: str, status: int, code: Any, error: str, message: str, reset_at: float = 0):
        super().__init__(f"GMGN {path} HTTP {status} code={code} {error} {message}".strip())
        self.path, self.status, self.code, self.error, self.message, self.reset_at = \
            path, status, code, error or "", message or "", reset_at


def _enc(s: Any) -> str:
    """等价于 JavaScript 的 encodeURIComponent。"""
    return quote(str(s), safe="-_.!~*'()")


def build_message(sub_path: str, query: dict, body: str, timestamp: int) -> str:
    parts = []
    for k in sorted(query):
        v = query[k]
        if isinstance(v, (list, tuple)):
            parts += [f"{_enc(k)}={_enc(x)}" for x in sorted(str(i) for i in v)]
        else:
            parts.append(f"{_enc(k)}={_enc(v)}")
    return f"{sub_path}:{'&'.join(parts)}:{body}:{timestamp}"


def load_private_key(pem: str):
    from cryptography.hazmat.primitives import serialization
    pem = pem.replace("\\n", "\n").strip()
    return serialization.load_pem_private_key(pem.encode(), password=None)


def sign(message: str, key) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    data = message.encode("utf-8")
    if isinstance(key, Ed25519PrivateKey):
        return base64.b64encode(key.sign(data)).decode()
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return base64.b64encode(sig).decode()
    raise ValueError("GMGN 私钥只支持 Ed25519 或 RSA")


class _Bucket:
    """按权重扣减的漏桶。"""

    def __init__(self, rate: float):
        self.rate = rate
        self.capacity = rate
        self.tokens = rate
        self.t = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, w: float) -> None:
        w = min(w, self.capacity)
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= w:
                    self.tokens -= w
                    return
                await asyncio.sleep((w - self.tokens) / self.rate)


class Gmgn:
    def __init__(self, api_key: str, private_key_pem: str = "", plan: str = "plus", usage: float = 0.8):
        self.key = api_key
        self.pk = load_private_key(private_key_pem) if private_key_pem else None
        rate = PLAN_RATE.get(plan.lower(), 5) * usage
        if plan.lower() == "free":
            rate = min(rate, 1.5)   # 实测免费版实际额度比文档写的紧，保守一点，避免被封
        self.bucket = _Bucket(rate)
        self.paused_until = 0.0
        self.disabled: set[str] = set()                    # 当前套餐用不了的接口
        self.weight_used = 0
        self.bans = 0
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=8),
                connector=aiohttp.TCPConnector(family=socket.AF_INET, limit=50, ttl_dns_cache=300),
                headers={"User-Agent": "memebot/1.0", "Content-Type": "application/json"})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, query: dict | None = None, body: Any = None,
                      retries: int = 2) -> Any:
        if path in self.disabled:
            return None
        signed = path in SIGNED
        if signed and not self.pk:
            raise GmgnError(path, 0, None, "NO_PRIVATE_KEY", "交易类接口需要 GMGN_PRIVATE_KEY")
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        sess = await self._sess()
        for attempt in range(retries + 1):
            wait = self.paused_until - time.time()
            if wait > 0:
                await asyncio.sleep(wait + 0.5)
            await self.bucket.take(WEIGHTS.get(path, 2))
            self.weight_used += WEIGHTS.get(path, 2)
            ts = int(time.time())
            q = {k: v for k, v in (query or {}).items() if v is not None}
            q.update(timestamp=ts, client_id=str(uuid.uuid4()))
            headers = {"X-APIKEY": self.key}
            if signed:
                headers["X-Signature"] = sign(build_message(path, q, body_str, ts), self.pk)
            url = f"{HOST}{path}?{urlencode(q, doseq=True)}"
            try:
                async with sess.request(method, url, headers=headers,
                                        data=body_str.encode() if body_str else None) as resp:
                    text = await resp.text()
                    reset_hdr = resp.headers.get("X-RateLimit-Reset", "")
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt >= retries or path == "/v1/trade/swap":   # 下单绝不盲目重发
                    raise GmgnError(path, 0, None, "NETWORK", str(e)) from e
                await asyncio.sleep(1 + attempt)
                continue
            try:
                js = json.loads(text)
            except json.JSONDecodeError:
                if status >= 500 and attempt < retries and path != "/v1/trade/swap":
                    await asyncio.sleep(1 + attempt)
                    continue
                raise GmgnError(path, status, None, "NON_JSON", text[:200])
            if js.get("code") == 0:
                return js.get("data")
            err = str(js.get("error") or "")
            reset_at = float(js.get("reset_at") or (reset_hdr if reset_hdr.isdigit() else 0) or 0)
            if err.startswith("RATE_LIMIT") or status == 429:
                # 全局暂停到解封时间，期间任何请求都会延长封禁
                fallback = 60 if "BAN" in err else 5
                self.paused_until = max(self.paused_until, (reset_at or time.time() + fallback) + 1)
                self.bucket.tokens = 0          # 解封后从零开始慢慢恢复，不要一下子把积压的请求全发出去
                self.bans += 1
                log.warning("GMGN 限流（%s），所有 GMGN 请求暂停到 %s", err or status,
                            time.strftime("%H:%M:%S", time.localtime(self.paused_until)))
                if attempt < retries and path != "/v1/trade/swap":
                    continue
            if err in ("PLAN_UPGRADE_REQUIRED", "PRO_PLAN_REQUIRED"):
                self.disabled.add(path)
                log.error("GMGN 接口 %s 当前套餐不可用，已停用：%s", path, js.get("upgrade_message", ""))
            raise GmgnError(path, status, js.get("code"), err, str(js.get("message") or ""), reset_at)
        return None

    # ================================================================ 读接口
    async def token_info(self, chain: str, token: str) -> dict:
        return await self.request("GET", "/v1/token/info", {"chain": CHAIN_MAP[chain], "address": token}) or {}

    async def token_security(self, chain: str, token: str) -> dict:
        return await self.request("GET", "/v1/token/security", {"chain": CHAIN_MAP[chain], "address": token}) or {}

    async def top_holders(self, chain: str, token: str, limit: int = 100, tag: str | None = None,
                          order_by: str = "amount_percentage") -> list[dict]:
        d = await self.request("GET", "/v1/market/token_top_holders", {
            "chain": CHAIN_MAP[chain], "address": token, "limit": limit, "order_by": order_by,
            "direction": "desc", "tag": tag})
        return _rows(d)

    async def top_traders(self, chain: str, token: str, limit: int = 100, tag: str | None = None,
                          order_by: str = "profit") -> list[dict]:
        d = await self.request("GET", "/v1/market/token_top_traders", {
            "chain": CHAIN_MAP[chain], "address": token, "limit": limit, "order_by": order_by,
            "direction": "desc", "tag": tag})
        return _rows(d)

    async def kline(self, chain: str, token: str, resolution: str, t_from: int, t_to: int) -> list[dict]:
        # 注意：K 线接口的 from/to 是毫秒（官方 CLI 里把秒 ×1000 后发送）
        d = await self.request("GET", "/v1/market/token_kline", {
            "chain": CHAIN_MAP[chain], "address": token, "resolution": resolution,
            "from": int(t_from) * 1000, "to": int(t_to) * 1000})
        return _rows(d)

    async def rank(self, chain: str, interval: str = "1h", limit: int = 20) -> list[dict]:
        d = await self.request("GET", "/v1/market/rank", {"chain": CHAIN_MAP[chain], "interval": interval,
                                                          "limit": limit})
        return _rows(d)

    async def trenches(self, chain: str, types: list[str], limit: int = 80, filters: dict | None = None) -> list[dict]:
        section = {"filters": ["offchain", "onchain"], "launchpad_platform_v2": True, "limit": limit, **(filters or {})}
        body: dict[str, Any] = {"version": "v2"}
        for t in types:
            body[t] = dict(section)
        d = await self.request("POST", "/v1/trenches", {"chain": CHAIN_MAP[chain]}, body)
        out: list[dict] = []
        if isinstance(d, dict):
            for key, label in (("completed", "已毕业"), ("pump", "即将毕业"), ("near_completion", "即将毕业"),
                               ("new_creation", "新创建")):
                for it in d.get(key) or []:
                    if isinstance(it, dict):
                        it["_stage"] = label
                        out.append(it)
        return out

    async def smartmoney(self, chain: str, limit: int = 200) -> list[dict]:
        return _rows(await self.request("GET", "/v1/user/smartmoney", {"chain": CHAIN_MAP[chain], "limit": limit}))

    async def kol(self, chain: str, limit: int = 200) -> list[dict]:
        return _rows(await self.request("GET", "/v1/user/kol", {"chain": CHAIN_MAP[chain], "limit": limit}))

    async def wallet_stats(self, chain: str, wallet: str, period: str = "30d") -> dict:
        """单个钱包的统计。实测：传多个 wallet_address 只会返回第一个，所以逐个查询。

        关键字段：realized_profit（美元）、buy / sell（交易笔数）、pnl_stat.winrate（按代币算的胜率）、
        pnl_stat.token_num（交易过的代币数）、pnl_stat.avg_holding_period（平均持仓秒数）。
        """
        d = await self.request("GET", "/v1/user/wallet_stats", {
            "chain": CHAIN_MAP[chain], "wallet_address": wallet, "period": period})
        if isinstance(d, list):
            d = d[0] if d else {}
        return d or {}

    async def user_info(self) -> Any:
        return await self.request("GET", "/v1/user/info")

    async def gas_price(self, chain: str) -> Any:
        return await self.request("GET", "/v1/trade/gas_price", {"chain": CHAIN_MAP[chain]})

    # ================================================================ 交易接口
    async def swap(self, params: dict) -> dict:
        return await self.request("POST", "/v1/trade/swap", {}, params, retries=0) or {}

    async def query_order(self, chain: str, order_id: str) -> dict:
        return await self.request("GET", "/v1/trade/query_order",
                                  {"order_id": order_id, "chain": CHAIN_MAP[chain]}) or {}


def _rows(d: Any) -> list[dict]:
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("list", "holders", "traders", "rank", "items", "data"):
            v = d.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("list"), list):
                return v["list"]
    return []
