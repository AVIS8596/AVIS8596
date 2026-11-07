"""下单执行器。

Solana：Jupiter Swap V2（/order + /execute，Jupiter 负责上链）→ 失败且是 pump 币时用 PumpPortal 本地签名兜底
EVM：0x Swap API v2（allowance-holder），卖出前自动授权
模拟盘：按 Birdeye 实时价格计算成交，不发任何交易

所有执行器都遵守一条规则：交易状态"未知"（超时）时，不自动重试买入，
而是先查余额确认是否已成交，防止重复买入。
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..chains import CHAINS, NATIVE_EVM_PLACEHOLDER, SOL_MINT
from ..http import ApiError
from ..sources.evm_rpc import MAX_UINT, SEL_APPROVE, pad_addr
from ..utils import dig, f

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.exec")


@dataclass
class Fill:
    ok: bool
    tx: str = ""
    token_raw: int = 0        # 买入得到 / 卖出卖掉的代币原始数量
    decimals: int = 0
    native: float = 0.0       # 买入花费 / 卖出得到的原生币
    error: str = ""
    unknown: bool = False     # 状态未知（需要人工确认）


# ====================================================================== 模拟盘
class DryRunExecutor:
    def __init__(self, app: "App", chain: str):
        self.app, self.chain = app, chain

    async def _decimals(self, token: str) -> int:
        if self.chain == "solana":
            return (await self.app.sol.mint_info(token)).get("decimals", 6)
        return await self.app.evm[self.chain].erc20_decimals(token)

    async def native_balance(self) -> float:
        return 1e9

    async def buy(self, token: str, amount: float, slippage_bps: int) -> Fill:
        px = await self.app.data.price(self.chain, token)
        npx = await self.app.native_price(self.chain)
        if px <= 0 or npx <= 0:
            return Fill(False, error="取不到价格，模拟买入失败")
        dec = await self._decimals(token)
        ui = amount * npx / px * (1 - slippage_bps / 10000 / 3)   # 模拟一点滑点损耗
        return Fill(True, "DRYRUN", int(ui * 10 ** dec), dec, amount)

    async def sell(self, token: str, raw: int, decimals: int, slippage_bps: int) -> Fill:
        px = await self.app.data.price(self.chain, token)
        npx = await self.app.native_price(self.chain)
        if px <= 0 or npx <= 0:
            return Fill(False, error="取不到价格，模拟卖出失败")
        native = raw / 10 ** decimals * px / npx * (1 - slippage_bps / 10000 / 3)
        return Fill(True, "DRYRUN", raw, decimals, native)


# ====================================================================== Solana
class SolanaExecutor:
    JUP = "https://api.jup.ag/swap/v2"
    PUMP = "https://pumpportal.fun/api/trade-local"

    def __init__(self, app: "App"):
        from solders.keypair import Keypair
        self.app = app
        self.kp = Keypair.from_base58_string(app.cfg.secrets.solana_private_key)
        self.pub = str(self.kp.pubkey())
        self.max_impact = float(app.cfg.get("trading.max_price_impact_pct", 15))
        app.http.set_rate("jupiter", float(app.cfg.get("budget.jupiter_rps", 8)))
        app.http.set_rate("pumpportal", 2)

    def _sign(self, raw: bytes) -> bytes:
        """只签自己那一个签名位（兼容需要多方签名的交易）。"""
        from solders.message import to_bytes_versioned
        from solders.transaction import VersionedTransaction
        vt = VersionedTransaction.from_bytes(raw)
        msg = vt.message
        sig = self.kp.sign_message(to_bytes_versioned(msg))
        keys = list(msg.account_keys)
        idx = keys.index(self.kp.pubkey())
        sigs = list(vt.signatures)
        sigs[idx] = sig
        return bytes(VersionedTransaction.populate(msg, sigs))

    async def native_balance(self) -> float:
        return await self.app.sol.balance_lamports(self.pub) / 1e9

    async def _jupiter(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
        h = {"x-api-key": self.app.cfg.secrets.jupiter_key}
        order = await self.app.http.get("jupiter", f"{self.JUP}/order", headers=h, retries=2, params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount),
            "taker": self.pub, "slippageBps": slippage_bps})
        tx_b64 = (order or {}).get("transaction")
        if not tx_b64:
            return {"ok": False, "no_route": True, "error": dig(order, "errorMessage", "error", default="无路由")}
        impact = abs(f(dig(order, "priceImpactPct", "priceImpact")))
        impact = impact * 100 if impact <= 1 else impact
        if impact > self.max_impact:
            return {"ok": False, "error": f"价格冲击 {impact:.1f}% 过大"}
        signed = base64.b64encode(self._sign(base64.b64decode(tx_b64))).decode()
        ex = await self.app.http.post("jupiter", f"{self.JUP}/execute", headers=h, retries=0, timeout=90,
                                      json_body={"signedTransaction": signed, "requestId": order["requestId"]})
        ok = (ex or {}).get("status") == "Success"
        return {"ok": ok, "sig": (ex or {}).get("signature", ""), "error": (ex or {}).get("error", ""),
                "out": int(f(dig(ex, "totalOutputAmount", "outputAmountResult"))),
                "inp": int(f(dig(ex, "totalInputAmount", "inputAmountResult")))}

    async def _pumpportal(self, action: str, mint: str, amount: float, in_sol: bool, slippage_bps: int) -> dict:
        raw = await self.app.http.post("pumpportal", self.PUMP, binary=True, retries=1, json_body={
            "publicKey": self.pub, "action": action, "mint": mint, "amount": amount,
            "denominatedInSol": "true" if in_sol else "false",
            "slippage": max(1, slippage_bps // 100), "priorityFee": 0.0005, "pool": "auto"})
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < 100:
            return {"ok": False, "error": "PumpPortal 未返回交易"}
        sig = await self.app.sol.send_raw(self._sign(bytes(raw)))
        res = await self.app.sol.confirm(sig, 60)
        return {"ok": res is True, "sig": sig, "unknown": res is None,
                "error": "" if res else ("链上失败" if res is False else "确认超时")}

    async def buy(self, mint: str, amount: float, slippage_bps: int) -> Fill:
        rpc = self.app.sol
        before, dec = await rpc.token_balance(self.pub, mint)
        lamports = int(amount * 1e9)
        err = ""
        try:
            r = await self._jupiter(SOL_MINT, mint, lamports, slippage_bps)
            if r["ok"]:
                after, dec2 = await rpc.token_balance(self.pub, mint)
                got = max(after - before, r["out"])
                return Fill(True, r["sig"], got, dec2 or dec, (r["inp"] or lamports) / 1e9)
            err = r["error"]
            if not r.get("no_route") and not mint.endswith("pump"):
                return Fill(False, r.get("sig", ""), error=f"Jupiter: {err}")
        except ApiError as e:
            err = str(e)
        if not mint.endswith("pump"):
            return Fill(False, error=f"Jupiter: {err}")
        # pump.fun 内盘币兜底
        try:
            r = await self._pumpportal("buy", mint, amount, True, slippage_bps)
        except Exception as e:  # noqa: BLE001
            return Fill(False, error=f"Jupiter: {err}; PumpPortal: {e}")
        after, dec2 = await rpc.token_balance(self.pub, mint)
        got = after - before
        if got > 0:
            return Fill(True, r.get("sig", ""), got, dec2 or dec, amount)
        return Fill(False, r.get("sig", ""), error=f"PumpPortal: {r.get('error')}", unknown=bool(r.get("unknown")))

    async def sell(self, mint: str, raw: int, decimals: int, slippage_bps: int) -> Fill:
        rpc = self.app.sol
        have, dec = await rpc.token_balance(self.pub, mint)
        raw = min(raw, have)
        if raw <= 0:
            return Fill(False, error="钱包里没有这个币了")
        sol_before = await rpc.balance_lamports(self.pub)
        err = ""
        try:
            r = await self._jupiter(mint, SOL_MINT, raw, slippage_bps)
            if r["ok"]:
                return Fill(True, r["sig"], raw, dec, r["out"] / 1e9)
            err = r["error"]
            if not r.get("no_route") and not mint.endswith("pump"):
                return Fill(False, r.get("sig", ""), error=f"Jupiter: {err}")
        except ApiError as e:
            err = str(e)
        if not mint.endswith("pump"):
            return Fill(False, error=f"Jupiter: {err}")
        try:
            r = await self._pumpportal("sell", mint, raw / 10 ** dec, False, slippage_bps)
        except Exception as e:  # noqa: BLE001
            return Fill(False, error=f"Jupiter: {err}; PumpPortal: {e}")
        after, _ = await rpc.token_balance(self.pub, mint)
        sold = have - after
        if sold > 0:
            got = max(await rpc.balance_lamports(self.pub) - sol_before, 0) / 1e9
            return Fill(True, r.get("sig", ""), sold, dec, got)
        return Fill(False, r.get("sig", ""), error=f"PumpPortal: {r.get('error')}", unknown=bool(r.get("unknown")))


# ====================================================================== EVM
class EvmExecutor:
    ZX = "https://api.0x.org/swap/allowance-holder/quote"

    def __init__(self, app: "App", chain: str):
        from eth_account import Account
        self.app, self.chain = app, chain
        self.rpc = app.evm[chain]
        self.acct = Account.from_key(app.cfg.secrets.evm_private_key)
        self.addr = self.acct.address.lower()
        self.cid = CHAINS[chain].chain_id
        self.max_gwei = float(app.cfg.per_chain("trading.max_gas_gwei", chain, 50))
        self.private_rpc = app.cfg.per_chain("trading.private_rpc", chain, "") or None
        self.max_impact = float(app.cfg.get("trading.max_price_impact_pct", 15))
        app.http.set_rate("zerox", float(app.cfg.get("budget.zerox_rps", 1)))

    async def native_balance(self) -> float:
        return await self.rpc.balance(self.addr) / 1e18

    async def _quote(self, sell: str, buy: str, amount: int, slippage_bps: int) -> dict:
        return await self.app.http.get("zerox", self.ZX, retries=2, headers={
            "0x-api-key": self.app.cfg.secrets.zerox_key, "0x-version": "v2"}, params={
            "chainId": self.cid, "sellToken": sell, "buyToken": buy, "sellAmount": str(amount),
            "taker": self.addr, "slippageBps": slippage_bps}) or {}

    async def _send(self, to: str, data: str, value: int, gas: int, gas_price: int) -> tuple[str, dict | None]:
        from eth_utils import to_checksum_address
        async with self.rpc.nonce_lock:   # 同一钱包串行发交易，避免 nonce 冲突
            nonce = await self.rpc.nonce(self.addr)
            txd = {"to": to_checksum_address(to), "data": data, "value": value, "gas": gas,
                   "gasPrice": gas_price, "nonce": nonce, "chainId": self.cid}
            signed = self.acct.sign_transaction(txd)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            h = await self.rpc.send_raw("0x" + bytes(raw).hex(), self.private_rpc)
        rc = await self.rpc.wait_receipt(h, 180 if self.chain == "ethereum" else 90)
        return h, rc

    async def _gas_ok(self) -> tuple[bool, int]:
        gp = await self.rpc.gas_price()
        return gp <= self.max_gwei * 1e9, gp

    def _impact_ok(self, q: dict) -> bool:
        imp = f(q.get("estimatedPriceImpact"))
        return imp <= self.max_impact if imp else True

    async def buy(self, token: str, amount: float, slippage_bps: int) -> Fill:
        ok, gp = await self._gas_ok()
        if not ok:
            return Fill(False, error=f"Gas {gp / 1e9:.1f} gwei 超过上限")
        wei = int(amount * 1e18)
        try:
            q = await self._quote(NATIVE_EVM_PLACEHOLDER, token, wei, slippage_bps)
        except ApiError as e:
            return Fill(False, error=f"0x 报价失败: {e}")
        if not q.get("liquidityAvailable", True) or not q.get("transaction"):
            return Fill(False, error="0x 无可用流动性")
        if not self._impact_ok(q):
            return Fill(False, error=f"价格冲击 {q.get('estimatedPriceImpact')}% 过大")
        t = q["transaction"]
        dec = await self.rpc.erc20_decimals(token)
        before = await self.rpc.erc20_balance(token, self.addr)
        h, rc = await self._send(t["to"], t["data"], int(t.get("value", wei)),
                                 int(int(t["gas"]) * 1.25), min(int(t.get("gasPrice", gp)), int(self.max_gwei * 1e9)))
        after = await self.rpc.erc20_balance(token, self.addr)
        got = after - before
        if rc and rc.get("status") == "0x1" and got > 0:
            return Fill(True, h, got, dec, amount)
        if rc is None:
            return Fill(got > 0, h, max(got, 0), dec, amount, error="" if got > 0 else "确认超时", unknown=got <= 0)
        return Fill(False, h, error="链上执行失败")

    async def sell(self, token: str, raw: int, decimals: int, slippage_bps: int) -> Fill:
        have = await self.rpc.erc20_balance(token, self.addr)
        raw = min(raw, have)
        if raw <= 0:
            return Fill(False, error="钱包里没有这个币了")
        ok, gp = await self._gas_ok()
        if not ok:
            return Fill(False, error=f"Gas {gp / 1e9:.1f} gwei 超过上限")
        try:
            q = await self._quote(token, NATIVE_EVM_PLACEHOLDER, raw, slippage_bps)
            allow = (q.get("issues") or {}).get("allowance")
            if allow and allow.get("spender"):
                spender = allow["spender"]
                data = SEL_APPROVE + pad_addr(spender) + hex(MAX_UINT)[2:].rjust(64, "0")
                try:
                    g = await self.rpc.estimate_gas({"from": self.addr, "to": token, "data": data})
                except Exception:  # noqa: BLE001
                    g = 100_000
                h_ap, rc_ap = await self._send(token, data, 0, int(g * 1.3), gp)
                if not rc_ap or rc_ap.get("status") != "0x1":
                    return Fill(False, h_ap, error="授权交易失败")
                q = await self._quote(token, NATIVE_EVM_PLACEHOLDER, raw, slippage_bps)
        except ApiError as e:
            return Fill(False, error=f"0x 报价失败: {e}")
        if not q.get("transaction"):
            return Fill(False, error="0x 无可用流动性（可能已无法卖出）")
        t = q["transaction"]
        nb = await self.rpc.balance(self.addr)
        h, rc = await self._send(t["to"], t["data"], int(t.get("value", 0)),
                                 int(int(t["gas"]) * 1.25), min(int(t.get("gasPrice", gp)), int(self.max_gwei * 1e9)))
        after = await self.rpc.erc20_balance(token, self.addr)
        sold = have - after
        if rc and rc.get("status") == "0x1" and sold > 0:
            gas_cost = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice", hex(gp)), 16)
            got = (await self.rpc.balance(self.addr) - nb + gas_cost) / 1e18
            return Fill(True, h, sold, decimals, max(got, 0.0))
        return Fill(False, h, error="确认超时" if rc is None else "链上执行失败", unknown=rc is None)


# ====================================================================== GMGN 下单
class GmgnExecutor:
    """通过 GMGN OpenAPI 下单（/v1/trade/swap），资金在绑定到 API Key 的 GMGN 钱包里。

    注意 GMGN 的规则：
      - 滑点是 0~100 的整数百分比（15 = 15%）
      - 金额用最小单位（lamports / wei）
      - 原生币地址：Solana 用 WSOL 地址，EVM 用 0x000…000
      - 同一个错误反复触发会被临时封禁，所以失败后不立刻重试（持仓管理里有退避）
    """
    EVM_NATIVE = "0x0000000000000000000000000000000000000000"

    def __init__(self, app: "App", chain: str):
        from ..sources.gmgn import CHAIN_MAP
        self.app, self.chain = app, chain
        self.gm = app.gmgn
        if self.gm is None or self.gm.pk is None:
            raise RuntimeError("缺少 GMGN_API_KEY 或 GMGN_PRIVATE_KEY")
        s = app.cfg.secrets
        self.addr = s.gmgn_wallet_sol if chain == "solana" else s.gmgn_wallet_evm
        if not self.addr:
            raise RuntimeError(f"缺少 {chain} 的 GMGN 钱包地址")
        self.gchain = CHAIN_MAP[chain]
        self.native = SOL_MINT if chain == "solana" else self.EVM_NATIVE
        self.ndec = CHAINS[chain].native_decimals
        g = app.cfg.get("gmgn", {}) or {}
        self.anti_mev = bool(g.get("anti_mev", True)) and chain != "base"   # Base 不支持防夹
        self.priority_fee = str(g.get("priority_fee_sol", "")) if chain == "solana" else ""
        self.tip_fee = str((g.get("tip_fee") or {}).get(chain, "") or "")
        self.max_gwei = float(app.cfg.per_chain("trading.max_gas_gwei", chain, 50))
        self.timeout = 150 if chain == "ethereum" else 75

    async def native_balance(self) -> float:
        if self.chain == "solana":
            return await self.app.sol.balance_lamports(self.addr, svc="helius_trade") / 1e9
        return await self.app.evm[self.chain].balance(self.addr) / 1e18

    async def _token_balance(self, token: str) -> tuple[int, int]:
        if self.chain == "solana":
            return await self.app.sol.token_balance(self.addr, token, svc="helius_trade")
        rpc = self.app.evm[self.chain]
        return await rpc.erc20_balance(token, self.addr), await rpc.erc20_decimals(token)

    async def _gas_ok(self) -> str:
        if self.chain == "solana":
            return ""
        gp = await self.app.evm[self.chain].gas_price()
        return f"Gas {gp / 1e9:.1f} gwei 超过上限" if gp > self.max_gwei * 1e9 else ""

    def _base_params(self, inp: str, out: str, slippage_bps: int) -> dict:
        p: dict = {"chain": self.gchain, "from_address": self.addr, "input_token": inp, "output_token": out,
                   "slippage": max(1, min(100, round(slippage_bps / 100)))}
        if self.anti_mev:
            p["is_anti_mev"] = True
        if self.priority_fee:
            p["priority_fee"] = self.priority_fee
        if self.tip_fee:
            p["tip_fee"] = self.tip_fee
        return p

    FINAL_OK = ("confirmed", "successful", "success")
    FINAL_BAD = ("failed", "expired", "cancel", "cancelled", "canceled", "error", "rejected")

    @staticmethod
    def _st(r: dict) -> str:
        return str(r.get("status", "")).lower()

    async def _run(self, params: dict) -> dict:
        """提交订单并轮询到最终状态（confirmed / failed / expired）。

        注意：GMGN 实际会返回文档里没写的中间状态（比如 submitted），
        所以这里用"不是最终状态就继续等"的写法，而不是列举中间状态。
        """
        import asyncio
        import time as _t
        r = await self.gm.swap(params)
        oid = r.get("order_id")
        end = _t.time() + self.timeout
        while oid and self._st(r) not in self.FINAL_OK + self.FINAL_BAD and _t.time() < end:
            await asyncio.sleep(1.5)
            try:
                q = await self.gm.query_order(self.chain, oid)
                if q:
                    r = {**r, **q}
            except Exception as e:  # noqa: BLE001
                log.debug("查询订单 %s: %s", oid, e)
        return r

    def _ok(self, r: dict) -> bool:
        return self._st(r) in self.FINAL_OK

    def _bad(self, r: dict) -> bool:
        return self._st(r) in self.FINAL_BAD

    @staticmethod
    def _err(r: dict) -> str:
        return str(r.get("error_status") or r.get("error_code") or r.get("status") or "未知错误")

    async def _wait_balance(self, token: str, cond, tries: int = 8, gap: float = 3.0) -> tuple[int, int]:
        """订单状态未知时，再观察一会儿链上余额，确认到底成交没有。"""
        import asyncio
        bal, dec = await self._token_balance(token)
        for _ in range(tries):
            if cond(bal):
                break
            await asyncio.sleep(gap)
            bal, dec = await self._token_balance(token)
        return bal, dec

    async def buy(self, token: str, amount: float, slippage_bps: int) -> Fill:
        from ..sources.gmgn import GmgnError
        g = await self._gas_ok()
        if g:
            return Fill(False, error=g)
        before, dec = await self._token_balance(token)
        params = self._base_params(self.native, token, slippage_bps)
        params["input_amount"] = str(int(amount * 10 ** self.ndec))
        try:
            r = await self._run(params)
        except GmgnError as e:
            return Fill(False, error=f"GMGN: {e.error} {e.message}".strip() if e.error else f"GMGN: {e.message or e}")
        rep = r.get("report") or {}
        h = r.get("hash", "")
        if self._bad(r):
            return Fill(False, h, error=f"GMGN: {self._err(r)}")
        # 成功或状态未知：都以链上余额为准（未知时多等一会儿）
        after, dec2 = await self._wait_balance(token, lambda b: b > before, tries=2 if self._ok(r) else 10)
        got = int(f(rep.get("output_amount"))) or max(after - before, 0)
        dec2 = int(f(rep.get("output_token_decimals"))) or dec2 or dec
        spent = int(f(rep.get("input_amount"))) / 10 ** self.ndec if rep.get("input_amount") else amount
        if self._ok(r) or after > before:
            return Fill(True, h, got, dec2, spent)
        return Fill(False, h, error=f"GMGN: 订单状态 {self._st(r) or '未知'}，链上暂未看到到账", unknown=True)

    async def sell(self, token: str, raw: int, decimals: int, slippage_bps: int) -> Fill:
        from ..sources.gmgn import GmgnError
        have, dec = await self._token_balance(token)
        if have <= 0:
            return Fill(False, error="钱包里没有这个币了")
        g = await self._gas_ok()
        if g:
            return Fill(False, error=g)
        raw = min(raw, have)
        native_before = await self.native_balance()
        params = self._base_params(token, self.native, slippage_bps)
        if raw >= have * 0.999:     # 全部卖出用百分比，避免余额精度问题留下碎渣
            params["input_amount"] = "0"
            params["input_amount_bps"] = "10000"
        else:
            params["input_amount"] = str(raw)
        try:
            r = await self._run(params)
        except GmgnError as e:
            return Fill(False, error=f"GMGN: {e.error} {e.message}".strip() if e.error else f"GMGN: {e.message or e}")
        rep = r.get("report") or {}
        h = r.get("hash", "")
        if self._bad(r):
            return Fill(False, h, error=f"GMGN: {self._err(r)}")
        after, _ = await self._wait_balance(token, lambda b: b < have, tries=2 if self._ok(r) else 10)
        sold = int(f(rep.get("input_amount"))) or max(have - after, 0)
        out_dec = int(f(rep.get("output_token_decimals"))) or self.ndec
        got = int(f(rep.get("output_amount"))) / 10 ** out_dec
        if not got and sold:
            got = max(await self.native_balance() - native_before, 0.0)   # 没有成交回报时按余额变化估算
        if self._ok(r) or after < have:
            return Fill(True, h, sold, decimals or dec, got)
        return Fill(False, h, error=f"GMGN: 订单状态 {self._st(r) or '未知'}，链上暂未看到卖出", unknown=True)
