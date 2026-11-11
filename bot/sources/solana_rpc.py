"""Solana RPC：Helius + 任意多个备用节点（Chainstack / Alchemy / QuickNode / 官方公共节点……），只用标准 JSON-RPC 方法。

按顺序用：排在前面的节点额度用完（429 max usage / 402 / quota……）就自动换下一个，10 分钟后再回头试；
某个节点临时出错（5xx、超时）这一次请求也换下一个试。.env 里：
  HELIUS_API_KEY=...                         （可选，有就排第一）
  SOLANA_RPC_URLS=https://节点1,https://节点2  （WebSocket 地址自动用 wss:// 同一个地址；
                                              不一样的话写成 https://...|wss://...）
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from ..http import ApiError, Http, PlanError, QuotaError, is_quota_text
from ..utils import f

log = logging.getLogger("memebot.solana")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
PUBLIC_HOSTS = ("api.mainnet.solana.com", "api.mainnet-beta.solana.com")


class RpcError(Exception):
    pass


OUT_SEC = 600          # 节点额度用完后，多久再试一次
BAD_KEY_SEC = 3600     # key 无效（401 / 403）多久再试


class QuotaOut(RpcError):
    """所有 Solana 节点都用不了（额度用完 / key 无效）：直接报错，不去敲节点。"""


def provider_name(url: str) -> str:
    host = (urlparse(url).hostname or url).lower()
    for key, name in (("helius", "Helius"), ("chainstack", "Chainstack"), ("alchemy", "Alchemy"),
                      ("quiknode", "QuickNode"), ("quicknode", "QuickNode"), ("ankr", "Ankr"), ("drpc", "dRPC"),
                      ("shyft", "Shyft"), ("syndica", "Syndica"), ("triton", "Triton")):
        if key in host:
            return name
    if host in PUBLIC_HOSTS:
        return "Solana 公共节点"
    return host


def parse_urls(raw: str) -> list[tuple[str, str]]:
    """"https://a,https://b|wss://b2" → [(http, wss), ...]。"""
    out = []
    for part in (raw or "").replace("\n", ",").replace(" ", ",").split(","):
        part = part.strip().strip('"').strip("'")
        if not part:
            continue
        h, _, w = part.partition("|")
        if not h.startswith(("https://", "http://")):
            continue
        out.append((h, w or "ws" + h[4:]))
    return out


@dataclass
class Endpoint:
    name: str
    http: str
    wss: str
    svc: str
    out_until: float = 0.0       # HTTP 用不了（额度用完）到什么时候
    out_since: float = 0.0
    out_why: str = ""
    ws_out_until: float = 0.0    # WebSocket 被拒到什么时候
    ws_why: str = ""
    calls: int = 0               # 成功的请求数（启动以来）

    @property
    def out(self) -> bool:
        return time.time() < self.out_until


class SolanaRPC:
    def __init__(self, http: Http, helius_key: str, rps: float, trade_rps: float = 3,
                 extra_urls: list[tuple[str, str]] | None = None):
        self.http = http
        self.eps: list[Endpoint] = []
        if helius_key:
            self.eps.append(Endpoint("Helius", f"https://mainnet.helius-rpc.com/?api-key={helius_key}",
                                     f"wss://mainnet.helius-rpc.com/?api-key={helius_key}", "helius"))
        for i, (h, w) in enumerate(extra_urls or []):
            self.eps.append(Endpoint(provider_name(h), h, w, f"solrpc{i + 1}"))
        for ep in self.eps:
            # 两份额度：交易相关（查余额、确认）单独留一份，不会被分析、监听抢光，保证随时能卖
            public = (urlparse(ep.http).hostname or "") in PUBLIC_HOSTS
            http.set_rate(ep.svc, min(rps, 3) if public else rps)       # 公共节点每 10 秒同一方法最多 40 次
            http.set_rate(ep.svc + "_trade", min(trade_rps, 1) if public else trade_rps)
        self._id = 0

    # ---- 兼容旧用法 ----
    @property
    def url(self) -> str:
        return self.eps[0].http if self.eps else ""

    @property
    def wss(self) -> str:
        return self.ws_pick().wss if self.eps else ""

    @property
    def out(self) -> bool:
        return bool(self.eps) and all(ep.out for ep in self.eps)

    @property
    def out_until(self) -> float:
        return min((ep.out_until for ep in self.eps), default=0.0)

    @out_until.setter
    def out_until(self, v: float) -> None:
        for ep in self.eps:
            ep.out_until = v

    @property
    def out_since(self) -> float:
        return min((ep.out_since for ep in self.eps if ep.out_since), default=0.0)

    # ---- WebSocket 选节点 ----
    def ws_pick(self) -> Endpoint:
        now = time.time()
        ok = [ep for ep in self.eps if ep.ws_out_until <= now]
        return ok[0] if ok else min(self.eps, key=lambda ep: ep.ws_out_until)

    def ws_failed(self, ep: Endpoint, why: str) -> bool:
        """WebSocket 连接被拒 / 断开。被拒（429 / 402 / 额度）时这个节点 10 分钟内先不用；返回是否还有别的节点可以马上试。"""
        rejected = any(x in why for x in ("HTTP 429", "HTTP 402", "HTTP 401", "HTTP 403")) or is_quota_text(why)
        if rejected and len(self.eps) > 1:
            ep.ws_out_until, ep.ws_why = time.time() + OUT_SEC, why
            log.warning("Solana 监听：%s 拒绝连接（%s），换下一个节点", ep.name, why[:120])
        return any(e.ws_out_until <= time.time() for e in self.eps if e is not ep)

    def ws_ok(self, ep: Endpoint) -> None:
        ep.ws_out_until, ep.ws_why = 0.0, ""

    # ---- HTTP ----
    def _mark_out(self, ep: Endpoint, why: str, sec: float = OUT_SEC) -> None:
        if not ep.out_since:
            ep.out_since = time.time()
            rest = [e.name for e in self.eps if e is not ep and not e.out]
            log.warning("Solana 节点 %s 用不了（%s），%d 分钟后再试；%s", ep.name, why, sec // 60,
                        f"先用 {rest[0]}" if rest else "没有别的节点了，分析改用 GMGN 数据")
        ep.out_until, ep.out_why = time.time() + sec, why

    async def _post(self, ep: Endpoint, body: dict, retries: int, trade: bool) -> Any:
        try:
            r = await self.http.post(ep.svc + ("_trade" if trade else ""), ep.http, json_body=body, retries=retries)
        except QuotaError:
            self._mark_out(ep, "额度用完")
            raise
        except PlanError:
            self._mark_out(ep, "地址里的密钥无效（401 / 403）：没复制全，或者在节点后台换过密钥", BAD_KEY_SEC)
            raise
        err = r.get("error") if isinstance(r, dict) else None
        if err and is_quota_text(str(err)):
            self._mark_out(ep, "额度用完")
            raise QuotaError(ep.svc, 429, str(err))
        ep.calls += 1
        if ep.out_since:
            log.info("Solana 节点 %s 恢复（停了 %.0f 分钟）", ep.name, (time.time() - ep.out_since) / 60)
            ep.out_since, ep.out_why = 0.0, ""
        return r

    async def call(self, method: str, params: list | None = None, retries: int = 3, svc: str = "helius") -> Any:
        trade = svc.endswith("_trade")
        last: Exception | None = None
        tried = 0
        for ep in self.eps:
            if ep.out:
                continue
            tried += 1
            self._id += 1
            body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
            try:
                r = await self._post(ep, body, retries, trade)
            except (QuotaError, PlanError) as e:
                last = e
                continue
            except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = e          # 这个节点这次出错（5xx / 超时 / 断线）：换下一个试
                continue
            if isinstance(r, dict) and r.get("error"):
                raise RpcError(f"{method}: {r['error']}")
            return r.get("result") if isinstance(r, dict) else None
        if not tried or isinstance(last, (QuotaError, PlanError)):
            whys = "；".join(f"{ep.name} {ep.out_why or '用不了'}" for ep in self.eps) or "没有配置 Solana 节点"
            wait = max(0.0, self.out_until - time.time()) / 60
            raise QuotaOut(f"Solana 节点都用不了（{whys}），{wait:.0f} 分钟后再试") from last
        raise last or RpcError(f"{method}: 没有可用的 Solana 节点")

    async def probe(self, ep: Endpoint) -> str:
        """/status 用：这个节点现在通不通。"""
        if ep.out:
            return f"❌ {ep.out_why}，{(ep.out_until - time.time()) / 60:.0f} 分钟后再试"
        try:
            r = await asyncio.wait_for(self._post(ep, {"jsonrpc": "2.0", "id": 0, "method": "getSlot", "params": []},
                                                  0, False), 8)
            if isinstance(r, dict) and r.get("error"):
                return f"❌ {str(r['error'])[:150]}"
            return "✅ 正常"
        except Exception as e:  # noqa: BLE001
            return f"❌ {ep.out_why or str(e)[:150] or type(e).__name__}"

    # ---------------- 读 ----------------
    async def get_transaction(self, sig: str) -> dict | None:
        for attempt in range(4):   # 刚确认的交易，RPC 节点可能还没同步到，稍等重试
            try:
                tx = await self.call("getTransaction", [sig, {
                    "encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
            except QuotaOut:
                return None          # 节点都用不了：别再等
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
