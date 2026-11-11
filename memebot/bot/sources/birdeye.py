"""Birdeye 数据接口（多链）。

注意：Birdeye 各接口返回字段命名不完全统一（有的驼峰、有的下划线），
所以解析时统一用 utils.dig 尝试多个字段名，避免字段名变动导致整体崩溃。
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from typing import Any

from ..http import ApiError, Http, PlanError
from ..utils import as_list, dig, f

log = logging.getLogger("memebot.birdeye")
BASE = "https://public-api.birdeye.so"

# 每个接口大约消耗的计算单元（用于每日预算保护，数值来自 Birdeye 官方 CU 表，偏保守估计）
CU_COST = {
    "/defi/price": 10, "/defi/multi_price": 30, "/defi/token_overview": 20,
    "/defi/token_security": 40, "/defi/token_creation_info": 20,
    "/defi/v2/tokens/new_listing": 80, "/defi/token_trending": 50,
    "/defi/v2/tokens/top_traders": 30, "/trader/gainers-losers": 30,
    "/wallet/v2/pnl/summary": 30, "/wallet/v2/tx/first-funded": 400,
    "/token/v1/first-buyers": 25, "/defi/v3/token/holder": 50,
    "/defi/history_price": 60,
}


class BudgetExceeded(Exception):
    pass


class Birdeye:
    def __init__(self, http: Http, api_key: str, daily_cu: int, rps: float):
        self.http = http
        self.key = api_key
        self.daily_cu = daily_cu
        http.set_rate("birdeye", rps)
        self._day = ""
        self._used = 0
        self.disabled: set[str] = set()   # 当前套餐无权限的接口，记住后不再请求

    # ---------------- 预算 ----------------
    def _charge(self, path: str, priority: bool) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._day:
            self._day, self._used = day, 0
        cost = CU_COST.get(path, 30)
        # 交易相关（价格查询）优先级高，超预算也放行，避免持仓没法止损
        if not priority and self._used + cost > self.daily_cu:
            raise BudgetExceeded(f"Birdeye 今日 CU 预算已用完（{self._used}/{self.daily_cu}）")
        self._used += cost

    @property
    def used_today(self) -> int:
        return self._used

    async def _call(self, method: str, path: str, chain: str, *, params: dict | None = None,
                    body: Any = None, priority: bool = False) -> Any:
        if path in self.disabled or not self.key:
            return None
        self._charge(path, priority)
        headers = {"X-API-KEY": self.key, "x-chain": chain, "accept": "application/json"}
        try:
            r = await self.http.request("birdeye", method, BASE + path, params=params,
                                        json_body=body, headers=headers)
        except PlanError as e:
            self.disabled.add(path)
            log.error("Birdeye 接口 %s 无权限（套餐不支持或 key 错误），已停用该功能: %s", path, e.body[:200])
            return None
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Birdeye %s %s 失败: %s", chain, path, e)
            return None
        if isinstance(r, dict) and r.get("success") is False:
            log.debug("Birdeye %s 返回失败: %s", path, str(r)[:300])
            return None
        return r.get("data") if isinstance(r, dict) and "data" in r else r

    # ---------------- 价格 ----------------
    async def price(self, chain: str, token: str, priority: bool = True) -> float:
        d = await self._call("GET", "/defi/price", chain, params={"address": token}, priority=priority)
        return f(dig(d, "value", "price"))

    async def multi_price(self, chain: str, tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        d = await self._call("GET", "/defi/multi_price", chain,
                             params={"list_address": ",".join(tokens[:100])}, priority=True)
        out: dict[str, float] = {}
        if isinstance(d, dict):
            for k, v in d.items():
                p = f(dig(v, "value", "price")) if isinstance(v, dict) else 0.0
                if p > 0:
                    out[k] = p
                    out[k.lower()] = p
        return out

    # ---------------- 代币 ----------------
    async def token_overview(self, chain: str, token: str) -> dict:
        return await self._call("GET", "/defi/token_overview", chain, params={"address": token}) or {}

    async def token_security(self, chain: str, token: str) -> dict:
        return await self._call("GET", "/defi/token_security", chain, params={"address": token}) or {}

    async def creation_info(self, chain: str, token: str) -> dict:
        return await self._call("GET", "/defi/token_creation_info", chain, params={"address": token}) or {}

    async def holders(self, chain: str, token: str, limit: int = 100) -> list[dict]:
        d = await self._call("GET", "/defi/v3/token/holder", chain,
                             params={"address": token, "offset": 0, "limit": limit})
        return as_list(d)

    async def new_listing(self, chain: str, limit: int = 20) -> list[dict]:
        d = await self._call("GET", "/defi/v2/tokens/new_listing", chain,
                             params={"limit": limit, "meme_platform_enabled": chain == "solana"})
        return as_list(d)

    async def trending(self, chain: str, limit: int = 20) -> list[dict]:
        d = await self._call("GET", "/defi/token_trending", chain,
                             params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": min(limit, 20)})
        return as_list(d)

    async def top_traders(self, chain: str, token: str, limit: int = 10) -> list[dict]:
        d = await self._call("GET", "/defi/v2/tokens/top_traders", chain,
                             params={"address": token, "time_frame": "24h", "sort_by": "volume",
                                     "sort_type": "desc", "offset": 0, "limit": min(limit, 10)})
        return as_list(d)

    async def first_buyers(self, token: str, limit: int = 100) -> list[dict]:
        """仅 Solana：最早的买家，带 insider/bundler/sniper 等标签和当前持仓状态。"""
        d = await self._call("GET", "/token/v1/first-buyers", "solana",
                             params={"token_address": token, "offset": 0, "limit": min(limit, 100)})
        return as_list(d)

    async def history_price(self, chain: str, token: str, t_from: int, t_to: int, interval: str = "15m") -> list[dict]:
        d = await self._call("GET", "/defi/history_price", chain,
                             params={"address": token, "address_type": "token", "type": interval,
                                     "time_from": t_from, "time_to": t_to})
        return as_list(d)

    # ---------------- 钱包 ----------------
    async def gainers(self, chain: str, period: str, limit: int = 10, offset: int = 0) -> list[dict]:
        d = await self._call("GET", "/trader/gainers-losers", chain,
                             params={"type": period, "sort_by": "PnL", "sort_type": "desc",
                                     "offset": offset, "limit": min(limit, 10)})
        return as_list(d)

    async def pnl_summary(self, chain: str, wallet: str, duration: str = "30d") -> dict | None:
        d = await self._call("GET", "/wallet/v2/pnl/summary", chain,
                             params={"wallet": wallet, "duration": duration})
        if not d:
            return None
        s = d.get("summary", d) if isinstance(d, dict) else {}
        win = f(dig(s, "counts.win_rate", "win_rate"))
        if win > 1:          # 有的接口返回百分数（如 65），统一成 0.65
            win /= 100
        return {
            "realized_usd": f(dig(s, "pnl.realized_profit_usd", "realized_profit_usd")),
            "total_usd": f(dig(s, "pnl.total_usd", "total_usd")),
            "win_rate": win,
            "trades": int(f(dig(s, "counts.total_trade", "total_trade"))),
            "invested_usd": f(dig(s, "cashflow_usd.total_invested", "total_invested")),
        }

    async def first_funded(self, wallets: list[str]) -> dict[str, dict]:
        """仅 Solana：每个钱包的首笔入金（出资人、时间）。用于识别同一出资人的捆绑钱包。"""
        out: dict[str, dict] = {}
        for i in range(0, len(wallets), 20):
            chunk = wallets[i:i + 20]
            d = await self._call("POST", "/wallet/v2/tx/first-funded", "solana", body={"wallets": chunk})
            if not d:
                continue
            items = d.items() if isinstance(d, dict) and not as_list(d) else \
                ((dig(x, "wallet", "address", default=""), x) for x in as_list(d))
            for w, info in items:
                if not isinstance(info, dict):
                    continue
                funder = dig(info, "funder", "from", "source", "fundedBy", "funded_by",
                             "tx.from", "transfer.from", default="")
                ts = f(dig(info, "block_unix_time", "blockUnixTime", "block_time", "blockTime",
                           "tx.block_unix_time", "timestamp"))
                if w:
                    out[w] = {"funder": funder, "ts": ts, "raw": info}
        return out
