"""辅助数据源：GoPlus（合约安全）和 Etherscan V2（EVM 资金溯源）。"""
from __future__ import annotations

import logging

from ..chains import CHAINS
from ..http import Http, PlanError
from ..utils import f

log = logging.getLogger("memebot.aux")


class GoPlus:
    """GoPlus 免费接口，不需要 key（有频率限制）。"""
    BASE = "https://api.gopluslabs.io/api/v1"

    def __init__(self, http: Http, rps: float):
        self.http = http
        http.set_rate("goplus", rps)

    async def token_security(self, chain: str, token: str) -> dict:
        info = CHAINS[chain]
        if chain != "solana" and not info.goplus_id:
            return {}   # GoPlus 不支持这条链，只用 GMGN 的安全检测
        if chain == "solana":
            url = f"{self.BASE}/solana/token_security"
        else:
            url = f"{self.BASE}/token_security/{info.goplus_id}"
        try:
            r = await self.http.get("goplus", url, params={"contract_addresses": token}, retries=2)
        except Exception as e:  # noqa: BLE001  网络问题也不能让整个分析失败
            log.debug("GoPlus %s: %s", token, e)
            return {}
        res = (r or {}).get("result") or {}
        return res.get(token) or res.get(token.lower()) or (next(iter(res.values())) if res else {})


class Etherscan:
    BASE = "https://api.etherscan.io/v2/api"

    def __init__(self, http: Http, key: str, rps: float):
        self.http = http
        self.key = key
        self.disabled_chains: set[str] = set()
        http.set_rate("etherscan", rps)

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    async def first_funder(self, chain: str, wallet: str) -> dict | None:
        """钱包的第一笔入金（普通转账或合约内部转账），返回出资地址和时间。"""
        if not self.key or chain in self.disabled_chains:
            return None
        cid = CHAINS[chain].chain_id
        best = None
        for action in ("txlist", "txlistinternal"):
            try:
                r = await self.http.get("etherscan", self.BASE, params={
                    "chainid": cid, "module": "account", "action": action, "address": wallet,
                    "startblock": 0, "endblock": 99999999, "page": 1, "offset": 10, "sort": "asc",
                    "apikey": self.key})
            except PlanError:
                self.disabled_chains.add(chain)
                return None
            except Exception as e:  # noqa: BLE001
                log.debug("etherscan %s: %s", wallet, e)
                continue
            res = (r or {}).get("result")
            if isinstance(res, str):
                if "not supported" in res.lower() or "upgrade" in res.lower() or "plan" in res.lower():
                    log.warning("Etherscan 当前套餐不支持 %s，已跳过该链资金溯源：%s", chain, res[:120])
                    self.disabled_chains.add(chain)
                    return None
                continue
            for tx in res or []:
                if tx.get("to", "").lower() == wallet.lower() and int(tx.get("value", "0")) > 0:
                    ts = f(tx.get("timeStamp"))
                    if best is None or ts < best["ts"]:
                        best = {"funder": tx.get("from", "").lower(), "ts": ts, "wei": int(tx["value"])}
                    break
        return best
