"""数据层：统一接口，底层用 GMGN（默认）或 Birdeye。

其他模块只调这里，不直接关心数据从哪来。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .chains import CHAINS, norm_addr
from .sources.gmgn import GmgnError
from .utils import dig, f

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.data")


class DataHub:
    def __init__(self, app: "App"):
        self.app = app
        self.provider = app.cfg.get("data.provider", "gmgn")
        self._px: dict[tuple[str, str], tuple[float, float]] = {}
        self._info: dict[tuple[str, str], tuple[float, dict]] = {}

    @property
    def gm(self):
        return self.app.gmgn

    @property
    def use_gmgn(self) -> bool:
        return self.provider == "gmgn" and self.app.gmgn is not None

    # ================================================================ 价格
    async def _gm_info(self, chain: str, token: str, max_age: float = 5) -> dict:
        k = (chain, token)
        hit = self._info.get(k)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        try:
            d = await self.gm.token_info(chain, token)
        except GmgnError as e:
            log.debug("GMGN token_info %s: %s", token, e)
            d = {}
        if d:
            self._info[k] = (time.time(), d)
            if len(self._info) > 5000:
                for kk in sorted(self._info, key=lambda x: self._info[x][0])[:1000]:
                    self._info.pop(kk, None)
        return d

    async def price(self, chain: str, token: str) -> float:
        if self.use_gmgn:
            return f(dig(await self._gm_info(chain, token), "price.price", "price"))
        return await self.app.birdeye.price(chain, token)

    async def prices(self, chain: str, tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        if not self.use_gmgn:
            return await self.app.birdeye.multi_price(chain, tokens)
        res = await asyncio.gather(*(self.price(chain, t) for t in tokens), return_exceptions=True)
        return {t: p for t, p in zip(tokens, res) if isinstance(p, float) and p > 0}

    async def native_usd(self, chain: str) -> float:
        info = CHAINS[chain]
        if self.use_gmgn:
            p = await self.price(chain, info.wrapped if chain == "solana" else info.wrapped.lower())
            if p > 0:
                return p
        if self.app.birdeye.key:
            return await self.app.birdeye.price(chain, info.wrapped)
        return 0.0

    # ================================================================ 代币
    async def overview(self, chain: str, token: str) -> dict:
        if not self.use_gmgn:
            ov = await self.app.birdeye.token_overview(chain, token)
            return {
                "symbol": str(dig(ov, "symbol", default="?")), "name": str(dig(ov, "name", default="")),
                "price": f(dig(ov, "price")), "liquidity": f(dig(ov, "liquidity")),
                "mcap": f(dig(ov, "marketCap", "mc", "fdv")), "holders": int(f(dig(ov, "holder"))),
                "volume_1h": f(dig(ov, "v1hUSD")), "buys_1h": int(f(dig(ov, "buy1h"))),
                "sells_1h": int(f(dig(ov, "sell1h"))), "change_1h": f(dig(ov, "priceChange1hPercent")),
                "created_ts": 0.0, "creator": "", "dev": {}, "stat": {}, "tags_stat": {},
            }
        d = await self._gm_info(chain, token, max_age=30)
        px = f(dig(d, "price.price"))
        p1h = f(dig(d, "price.price_1h"))
        supply = f(dig(d, "circulating_supply", "total_supply"))
        return {
            "symbol": str(d.get("symbol") or "?"), "name": str(d.get("name") or ""),
            "price": px, "liquidity": f(d.get("liquidity")), "mcap": px * supply,
            "holders": int(f(d.get("holder_count"))),
            "volume_1h": f(dig(d, "price.volume_1h")), "buys_1h": int(f(dig(d, "price.buys_1h"))),
            "sells_1h": int(f(dig(d, "price.sells_1h"))),
            "change_1h": (px / p1h - 1) * 100 if px and p1h else 0.0,
            "created_ts": f(d.get("creation_timestamp")) or f(d.get("open_timestamp")),
            "creator": str(dig(d, "dev.creator_address", default="") or ""),
            "decimals": int(f(d.get("decimals"))),
            "total_supply": f(d.get("total_supply")),
            "launchpad": d.get("launchpad_platform") or d.get("launchpad") or "",
            "dev": d.get("dev") or {}, "stat": d.get("stat") or {}, "tags_stat": d.get("wallet_tags_stat") or {},
        }

    async def gm_security(self, chain: str, token: str) -> dict:
        if not self.use_gmgn:
            return {}
        try:
            return await self.gm.token_security(chain, token)
        except GmgnError as e:
            log.debug("GMGN security %s: %s", token, e)
            return {}

    async def gm_holders(self, chain: str, token: str) -> list[dict]:
        """前 100 持币人，带 GMGN 标签（bundler / rat_trader / sniper ...）和首笔入金来源。"""
        if not self.use_gmgn:
            return []
        try:
            return await self.gm.top_holders(chain, token, 100)
        except GmgnError as e:
            log.debug("GMGN holders %s: %s", token, e)
            return []

    # ================================================================ 聪明钱
    async def smart_candidates(self, chain: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.use_gmgn:
            for name, fn in (("gmgn_smart", self.gm.smartmoney), ("gmgn_kol", self.gm.kol)):
                try:
                    for t in await fn(chain, 200):
                        if t.get("maker"):
                            out.setdefault(norm_addr(chain, t["maker"]), name)
                except GmgnError as e:
                    log.warning("GMGN %s %s: %s", name, chain, e)
            try:
                n_tok = int(self.app.cfg.get("smart_money.trending_tokens_per_chain", 20))
                for tok in (await self.gm.rank(chain, "1h", n_tok))[:n_tok]:
                    ta = tok.get("address")
                    if not ta:
                        continue
                    for tr in await self.gm.top_traders(chain, ta, 20, order_by="profit"):
                        a = tr.get("address")
                        if a and int(f(tr.get("addr_type"))) != 2:
                            out.setdefault(norm_addr(chain, a), f"top_trader:{tok.get('symbol', '?')}")
            except GmgnError as e:
                log.warning("GMGN 热门币交易者 %s: %s", chain, e)
            return out
        return await self.app.smart.birdeye_candidates(chain)

    async def wallet_pnl(self, chain: str, wallets: list[str], period: str = "30d") -> dict[str, dict]:
        """返回 {钱包: {realized_usd, win_rate, trades}}。拿不到的钱包不在结果里。"""
        out: dict[str, dict] = {}
        if not wallets:
            return out
        if self.use_gmgn:
            sem = asyncio.Semaphore(4)

            async def one(w: str) -> None:
                async with sem:
                    try:
                        r = await self.gm.wallet_stats(chain, w, period)
                    except GmgnError as e:
                        log.debug("GMGN wallet_stats %s: %s", w, e)
                        return
                if not r:
                    return
                ps = r.get("pnl_stat") or {}
                win = f(dig(ps, "winrate", default=r.get("winrate")))
                tx_count = int(f(dig(r, "buy", "buy_count")) + f(dig(r, "sell", "sell_count")))
                tokens = int(f(ps.get("token_num")))
                out[norm_addr(chain, w)] = {
                    "realized_usd": f(r.get("realized_profit")),
                    "win_rate": win / 100 if win > 1 else win,
                    # GMGN 的胜率按"代币"算，所以交易次数也按"交易过的代币数"算，口径一致；
                    # 交易笔数会被机器人拆单放大（实测聪明钱 30 天常有上万笔），不适合拿来过滤
                    "trades": tokens or tx_count,
                    "tx_count": tx_count,
                    "tokens": tokens,
                    "avg_hold_sec": f(ps.get("avg_holding_period")),
                    "tags": dig(r, "common.tags", default=[]) or [],
                }

            await asyncio.gather(*(one(w) for w in wallets))
            return out
        for w in wallets:
            p = await self.app.birdeye.pnl_summary(chain, w, period)
            if p:
                out[w] = p
        return out

    # ================================================================ 新币
    async def new_tokens(self, chain: str) -> list[dict]:
        min_liq = float(self.app.cfg.get("discovery.min_liquidity_usd", 5000))
        if self.use_gmgn:
            types = self.app.cfg.get("discovery.gmgn_trench_types", ["completed", "near_completion"])
            try:
                items = await self.gm.trenches(chain, types, 80, {"min_liquidity": min_liq})
            except GmgnError as e:
                log.warning("GMGN 新币 %s: %s", chain, e)
                return []
            return [{"address": norm_addr(chain, it["address"]), "liquidity": f(it.get("liquidity")),
                     "source": f"{it.get('launchpad_platform') or 'GMGN'}{it.get('_stage', '')}"}
                    for it in items if it.get("address")]
        items = await self.app.birdeye.new_listing(chain, 20)
        return [{"address": norm_addr(chain, it["address"]), "liquidity": f(it.get("liquidity")), "source": "新上 DEX"}
                for it in items if it.get("address")]

    # ================================================================ K 线（回测用）
    async def closes(self, chain: str, token: str, t_from: int, t_to: int) -> list[tuple[float, float]]:
        if self.use_gmgn:
            try:
                rows = await self.gm.kline(chain, token, "15m", t_from, t_to)
            except GmgnError:
                return []
            out = []
            for r in rows:
                t = f(r.get("time"))
                if t > 1e12:      # 有的返回毫秒
                    t /= 1000
                out.append((t, f(r.get("close"))))
            return out
        items = await self.app.birdeye.history_price(chain, token, t_from, t_to)
        return [(f(dig(i, "unixTime", "unix_time")), f(dig(i, "value", "price"))) for i in items]

    def usage_text(self) -> str:
        parts = []
        if self.app.gmgn:
            g = self.app.gmgn
            parts.append(f"GMGN 已用权重 {g.weight_used:,}")
            if g.paused_until > time.time():
                parts.append(f"⚠️ GMGN 限流暂停到 {time.strftime('%H:%M:%S', time.localtime(g.paused_until))}")
            if g.disabled:
                parts.append("GMGN 套餐不支持：" + ", ".join(sorted(g.disabled)))
        if self.app.birdeye.key:
            parts.append(f"Birdeye 今日 {self.app.birdeye.used_today:,} CU")
            if self.app.birdeye.disabled:
                parts.append("Birdeye 套餐不支持：" + ", ".join(sorted(self.app.birdeye.disabled)))
        return " | ".join(parts)

