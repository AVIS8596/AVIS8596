"""Solana RPC（Helius）。只用标准 JSON-RPC 方法，所有 Helius 付费档都可用。"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from ..http import Http
from ..utils import f

log = logging.getLogger("memebot.solana")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


class RpcError(Exception):
    pass


class SolanaRPC:
    def __init__(self, http: Http, helius_key: str, rps: float, trade_rps: float = 3):
        self.http = http
        self.url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        self.wss = f"wss://mainnet.helius-rpc.com/?api-key={helius_key}"
        # 两份额度：交易相关（查余额、确认）单独留一份，不会被分析、监听抢光，保证随时能卖
        http.set_rate("helius", rps)
        http.set_rate("helius_trade", trade_rps)
        self._id = 0

    async def call(self, method: str, params: list | None = None, retries: int = 3, svc: str = "helius") -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        r = await self.http.post(svc, self.url, json_body=body, retries=retries)
        if isinstance(r, dict) and r.get("error"):
            raise RpcError(f"{method}: {r['error']}")
        return r.get("result") if isinstance(r, dict) else None

    # ---------------- 读 ----------------
    async def get_transaction(self, sig: str) -> dict | None:
        for attempt in range(4):   # 刚确认的交易，RPC 节点可能还没同步到，稍等重试
            try:
                tx = await self.call("getTransaction", [sig, {
                    "encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
            except RpcError as e:
                log.debug("getTransaction %s: %s", sig, e)
                tx = None
            if tx:
                return tx
            await asyncio.sleep(0.8 * (attempt + 1))
        return None

    async def signatures(self, address: str, limit: int = 1000, before: str | None = None) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def oldest_signatures(self, address: str, n: int, max_pages: int = 3) -> list[dict] | None:
        """返回最早的 n 条签名（按时间正序）。历史太长（翻页超过上限）返回 None。"""
        before = None
        last_page: list[dict] = []
        for _ in range(max_pages):
            page = await self.signatures(address, 1000, before)
            if not page:
                break
            last_page = page
            if len(page) < 1000:
                return list(reversed(page))[:n]
            before = page[-1]["signature"]
        return None if last_page else []

    async def balance_lamports(self, address: str, svc: str = "helius") -> int:
        r = await self.call("getBalance", [address, {"commitment": "confirmed"}], svc=svc)
        return int(r.get("value", 0)) if isinstance(r, dict) else 0

    async def token_balance(self, owner: str, mint: str, svc: str = "helius") -> tuple[int, int]:
        """返回 (原始数量, 精度)。用 mint 过滤，Token 和 Token-2022 都能查到。"""
        r = await self.call("getTokenAccountsByOwner",
                            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                            retries=6, svc=svc)
        total, dec = 0, 0
        for acc in (r or {}).get("value", []):
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            ta = info.get("tokenAmount", {})
            total += int(ta.get("amount", "0"))
            dec = int(ta.get("decimals", dec))
        return total, dec

    async def token_supply(self, mint: str) -> tuple[int, int]:
        r = await self.call("getTokenSupply", [mint])
        v = (r or {}).get("value", {})
        return int(v.get("amount", "0")), int(v.get("decimals", 0))

    async def mint_info(self, mint: str) -> dict:
        """读取 mint 账户：铸币/冻结权限、精度、供应量、Token-2022 扩展（转账税等）。"""
        r = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        v = (r or {}).get("value") or {}
        parsed = (v.get("data") or {}).get("parsed") or {}
        info = parsed.get("info") or {}
        out = {
            "mintAuthority": info.get("mintAuthority"),
            "freezeAuthority": info.get("freezeAuthority"),
            "decimals": int(info.get("decimals", 0) or 0),
            "supply": int(info.get("supply", "0") or 0),
            "isToken2022": v.get("owner") == TOKEN_2022,
            "transferFeeBps": 0,
            "nonTransferable": False,
            "found": bool(info),
        }
        for ext in info.get("extensions", []) or []:
            name = ext.get("extension")
            st = ext.get("state") or {}
            if name == "transferFeeConfig":
                bps = max(int((st.get("newerTransferFee") or {}).get("transferFeeBasisPoints", 0) or 0),
                          int((st.get("olderTransferFee") or {}).get("transferFeeBasisPoints", 0) or 0))
                out["transferFeeBps"] = bps
            elif name == "nonTransferable":
                out["nonTransferable"] = True
        return out

    async def is_executable_or_program_owned(self, address: str) -> bool:
        r = await self.call("getAccountInfo", [address, {"encoding": "base64"}])
        v = (r or {}).get("value")
        if not v:
            return False
        return bool(v.get("executable")) or v.get("owner") != SYSTEM_PROGRAM

    # ---------------- 写 ----------------
    async def send_raw(self, tx_bytes: bytes) -> str:
        b64 = base64.b64encode(tx_bytes).decode()
        return await self.call("sendTransaction", [b64, {
            "encoding": "base64", "skipPreflight": True, "maxRetries": 3}], retries=1)

    async def confirm(self, sig: str, timeout: float = 60) -> bool | None:
        """True=成功，False=链上失败，None=超时未知。"""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            r = await self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            st = ((r or {}).get("value") or [None])[0]
            if st:
                if st.get("err"):
                    return False
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(1.5)
        return None

    # ---------------- 资金溯源（Birdeye 拿不到时的兜底） ----------------
    async def first_funder(self, wallet: str) -> dict | None:
        sigs = await self.oldest_signatures(wallet, 5, max_pages=2)
        if not sigs:
            return None
        for s in sigs:
            tx = await self.get_transaction(s["signature"])
            if not tx:
                continue
            for src, dst, lamports in iter_sol_transfers(tx):
                if dst == wallet and lamports > 0 and src != wallet:
                    return {"funder": src, "ts": f(tx.get("blockTime")), "lamports": lamports}
        return None


# ====================================================================== 解析
def account_keys(tx: dict) -> list[str]:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    out = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    meta = tx.get("meta") or {}
    la = meta.get("loadedAddresses") or {}
    out += la.get("writable", []) + la.get("readonly", [])
    return out


def signer(tx: dict) -> str:
    keys = account_keys(tx)
    return keys[0] if keys else ""


def iter_sol_transfers(tx: dict):
    """遍历交易里的 SOL 转账（包括内部指令）。"""
    msg = tx.get("transaction", {}).get("message", {})
    ixs = list(msg.get("instructions", []))
    for inner in (tx.get("meta") or {}).get("innerInstructions", []) or []:
        ixs += inner.get("instructions", [])
    for ix in ixs:
        p = ix.get("parsed")
        if ix.get("program") == "system" and isinstance(p, dict) and p.get("type") in ("transfer", "transferWithSeed"):
            info = p.get("info", {})
            yield info.get("source", ""), info.get("destination", ""), int(info.get("lamports", 0))


def wallet_deltas(tx: dict, wallet: str) -> tuple[float, dict[str, tuple[int, int]]]:
    """计算某钱包在这笔交易里的变化。

    返回 (SOL 变化量（单位 SOL，含 WSOL，已加回手续费）, {mint: (原始数量变化, 精度)})。
    """
    meta = tx.get("meta") or {}
    keys = account_keys(tx)
    sol_delta = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances", []), meta.get("postBalances", [])
        if i < len(pre) and i < len(post):
            lam = post[i] - pre[i]
            if i == 0:
                lam += int(meta.get("fee", 0))  # 手续费不算买入成本
            sol_delta = lam / 1e9

    tok: dict[str, list[int]] = {}

    def add(bals: list, sign: int) -> None:
        for b in bals or []:
            if b.get("owner") != wallet:
                continue
            mint = b.get("mint")
            ui = b.get("uiTokenAmount", {})
            amt = int(ui.get("amount", "0"))
            dec = int(ui.get("decimals", 0))
            cur = tok.setdefault(mint, [0, dec])
            cur[0] += sign * amt
            cur[1] = dec

    add(meta.get("preTokenBalances"), -1)
    add(meta.get("postTokenBalances"), +1)

    from ..chains import SOL_MINT
    if SOL_MINT in tok:   # WSOL 视同 SOL
        amt, dec = tok.pop(SOL_MINT)
        sol_delta += amt / 10 ** (dec or 9)
    return sol_delta, {m: (v[0], v[1]) for m, v in tok.items() if v[0] != 0}
