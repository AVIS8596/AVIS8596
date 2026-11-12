"""链上能查到、但以前没用上的三类"背景信息"（11-12 加）：钻石手敢拿住，常常是因为知道这些。

1. 项目方历史战绩：他以前发过多少个币、最好的一个涨到多少市值、有几个做到 $50 万以上（GMGN created_tokens）。
   发过一堆、个个归零的是割韭菜的；以前做出过大币的，这次也更可能认真做。
2. 大户质量：前 20 大持有人（去掉池子 / 交易所）里——
   有钱的大户（钱包里 ≥ 100 SOL / 10 BNB）几个、被 GMGN 标成聪明钱 / KOL 的几个、新钱包几个、
   同一个出资人打钱的最大一伙有几个（团伙）。
3. 社区：Telegram 公开群 / 频道的人数，以及最近的增长速度（用我们自己的 Telegram 机器人查，免费）。

这些都存进分析报告和结果档案，学来的打法 / 钻石手打法 / 选币模型都会拿来学。字段取不到就留空，不当成 0。
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter, deque
from typing import TYPE_CHECKING, Any

from ..utils import dig, f

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.intel")

INTEL_KEYS = ("dev_tokens_n", "dev_best_mcap", "dev_good_n", "whale_n", "smart_hold_n", "fresh_top_n",
              "same_funder_n", "tg_members", "tg_growth_h")
WHALE_NATIVE = {"solana": 100.0, "bsc": 10.0, "ethereum": 5.0, "base": 5.0}


def _native(v: Any, chain: str) -> float:
    """GMGN 的原生币余额：有的接口给最小单位（lamports / wei），有的给整数单位，按大小判断
    （普通钱包不可能有 10 万 SOL / 10 亿 BNB，超过就是最小单位）。"""
    x = f(v)
    if chain == "solana" and x > 1e5:
        return x / 1e9
    if chain != "solana" and x > 1e9:
        return x / 1e18
    return x


def holder_quality(holders: list[dict], chain: str, top: int = 20) -> dict:
    """前 20 大持有人（去掉池子 / 交易所）的成色。"""
    hs = [h for h in (holders or []) if h.get("address") and int(f(h.get("addr_type"))) != 2][:top]
    if not hs:
        return {}
    whale = smart = fresh = 0
    funders: Counter = Counter()
    for h in hs:
        tags = " ".join(str(t).lower() for t in (h.get("maker_token_tags") or []) + (h.get("tags") or []))
        nb = h.get("native_balance", h.get("sol_balance", h.get("balance_native")))
        if nb is not None and _native(nb, chain) >= WHALE_NATIVE.get(chain, 10):
            whale += 1
        if any(w in tags for w in ("smart", "renowned", "kol")):
            smart += 1
        if "fresh" in tags or h.get("is_new") in (True, 1, "1", "true"):
            fresh += 1
        nt = h.get("native_transfer") or {}
        src = dig(nt, "address", "from_address", "from", default="") if isinstance(nt, dict) else ""
        if src:
            funders[src] += 1
    return {"whale_n": whale, "smart_hold_n": smart, "fresh_top_n": fresh,
            "same_funder_n": max(funders.values()) if funders else 0}


MCAP_KEYS = ("ath_market_cap", "ath_mc", "max_market_cap", "history_highest_market_cap", "highest_market_cap",
             "market_cap", "usd_market_cap", "mc", "marketcap")


def dev_stats(rows: list[dict]) -> dict:
    """项目方发过的币 → 发过几个、最好的一个涨到多少市值、有几个做到 $50 万以上。"""
    best, good = 0.0, 0
    for r in rows:
        m = max((f(r.get(k)) for k in MCAP_KEYS), default=0.0)
        best = max(best, m)
        good += int(m >= 500_000)
    return {"dev_tokens_n": len(rows), "dev_best_mcap": best, "dev_good_n": good}


class Intel:
    def __init__(self, app: "App"):
        self.app = app
        self._dev: dict[str, tuple[float, dict]] = {}
        self._tg: dict[str, deque] = {}
        self._calls: dict[str, deque] = {"dev": deque(), "tg": deque()}
        self._logged = False

    def _budget(self, kind: str, per_hour: int) -> bool:
        q, now = self._calls[kind], time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return False
        q.append(now)
        return True

    async def dev(self, chain: str, creator: str) -> dict:
        if not creator or not getattr(self.app, "gmgn", None):
            return {}
        key = f"{chain}:{creator}"
        hit = self._dev.get(key)
        if hit and time.time() - hit[0] < 24 * 3600:
            return hit[1]
        if not self._budget("dev", int(self.app.cfg.get("intel.dev_per_hour", 400) or 400)):
            return {}
        try:
            d = await self.app.gmgn.created_tokens(chain, creator)
        except Exception as e:  # noqa: BLE001
            log.debug("项目方历史 %s: %s", creator, e)
            return {}
        from ..sources.gmgn import _rows
        rows = _rows(d) if not isinstance(d, dict) or not isinstance(d.get("tokens"), list) else d["tokens"]
        if rows and not self._logged:
            self._logged = True
            log.info("GMGN 项目方发币记录字段：%s", sorted(rows[0].keys())[:40])
        out = dev_stats(rows) if isinstance(d, (dict, list)) else {}
        if len(self._dev) > 20000:
            self._dev.clear()
        self._dev[key] = (time.time(), out)
        return out

    async def tg(self, link: str) -> dict:
        """Telegram 公开群 / 频道人数和最近的增长速度（人 / 小时）。私密邀请链接查不了。"""
        m = re.search(r"(?:t\.me/|telegram\.me/|^@)([A-Za-z0-9_]{5,32})/?$", (link or "").strip())
        tg = getattr(self.app, "tg", None)
        if not m or not tg or not getattr(tg, "base", ""):
            return {}
        handle = m.group(1)
        hist = self._tg.setdefault(handle.lower(), deque(maxlen=50))
        now = time.time()
        if not hist or now - hist[-1][0] > 1800:
            if not self._budget("tg", int(self.app.cfg.get("intel.tg_per_hour", 300) or 300)):
                return self._tg_out(hist, now)
            try:
                r = await self.app.http.get("tg", f"{tg.base}/getChatMemberCount", params={"chat_id": f"@{handle}"},
                                            retries=0)
                if isinstance(r, dict) and r.get("ok"):
                    hist.append((now, int(r["result"])))
            except Exception as e:  # noqa: BLE001
                log.debug("TG 人数 %s: %s", handle, e)
        return self._tg_out(hist, now)

    @staticmethod
    def _tg_out(hist: deque, now: float) -> dict:
        if not hist:
            return {}
        t1, n1 = hist[-1]
        out: dict[str, Any] = {"tg_members": n1}
        old = [(t, n) for t, n in hist if t1 - t >= 1800]
        if old:
            t0, n0 = old[-1]
            out["tg_growth_h"] = round((n1 - n0) / ((t1 - t0) / 3600), 1)
        return out

    async def enrich(self, rep: dict, ov: dict) -> None:
        """分析报告补上项目方战绩和社区数据（大户质量在各链的分析里已经算好）。"""
        rep.update(await self.dev(rep["chain"], str(rep.get("creator") or "")))
        link = (ov.get("link") or {}).get("telegram") or ""
        if link:
            rep.update(await self.tg(str(link)))
