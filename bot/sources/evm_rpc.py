"""EVM JSON-RPC（ETH / BSC / Base）。直接走 JSON-RPC，不依赖 web3.py 的异步 provider。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..http import Http

log = logging.getLogger("memebot.evm")

SEL_BALANCE_OF = "0x70a08231"
SEL_DECIMALS = "0x313ce567"
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_ALLOWANCE = "0xdd62ed3e"
SEL_APPROVE = "0x095ea7b3"
MAX_UINT = (1 << 256) - 1


class EvmRpcError(Exception):
    pass


def pad_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:].lower()


class EvmRPC:
    def __init__(self, http: Http, chain: str, url: str, rps: float = 25):
        self.http = http
        self.chain = chain
        self.url = url
        self.svc = f"rpc_{chain}"
        http.set_rate(self.svc, rps)
        self._id = 0
        self.nonce_lock = asyncio.Lock()

    async def call(self, method: str, params: list | None = None, retries: int = 3) -> Any:
        self._id += 1
        r = await self.http.post(self.svc, self.url, retries=retries,
                                 json_body={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []})
        if isinstance(r, dict) and r.get("error"):
            raise EvmRpcError(f"{self.chain} {method}: {r['error']}")
        return r.get("result") if isinstance(r, dict) else None

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber"), 16)

    async def tx(self, h: str) -> dict | None:
        return await self.call("eth_getTransactionByHash", [h])

    async def receipt(self, h: str) -> dict | None:
        return await self.call("eth_getTransactionReceipt", [h])

    async def code(self, addr: str) -> str:
        return await self.call("eth_getCode", [addr, "latest"]) or "0x"

    async def is_contract(self, addr: str) -> bool:
        c = await self.code(addr)
        # EIP-7702 委托的普通钱包代码以 0xef0100 开头，仍然算个人钱包
        return c not in ("0x", "0x0", "") and not c.lower().startswith("0xef0100")

    async def logs(self, params: dict) -> list[dict]:
        return await self.call("eth_getLogs", [params]) or []

    async def balance(self, addr: str) -> int:
        return int(await self.call("eth_getBalance", [addr, "latest"]), 16)

    async def eth_call(self, to: str, data: str) -> str:
        return await self.call("eth_call", [{"to": to, "data": data}, "latest"]) or "0x"

    async def erc20_balance(self, token: str, owner: str) -> int:
        r = await self.eth_call(token, SEL_BALANCE_OF + pad_addr(owner))
        return int(r, 16) if r and r != "0x" else 0

    async def erc20_decimals(self, token: str) -> int:
        try:
            r = await self.eth_call(token, SEL_DECIMALS)
            return int(r, 16) if r and r != "0x" else 18
        except EvmRpcError:
            return 18

    async def erc20_total_supply(self, token: str) -> int:
        r = await self.eth_call(token, SEL_TOTAL_SUPPLY)
        return int(r, 16) if r and r != "0x" else 0

    async def erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        r = await self.eth_call(token, SEL_ALLOWANCE + pad_addr(owner) + pad_addr(spender))
        return int(r, 16) if r and r != "0x" else 0

    async def gas_price(self) -> int:
        return int(await self.call("eth_gasPrice"), 16)

    async def nonce(self, addr: str) -> int:
        return int(await self.call("eth_getTransactionCount", [addr, "pending"]), 16)

    async def estimate_gas(self, tx: dict) -> int:
        return int(await self.call("eth_estimateGas", [tx]), 16)

    async def send_raw(self, raw_hex: str, url: str | None = None) -> str:
        if url and url != self.url:   # 私密交易通道（如 Flashbots Protect）
            r = await self.http.post(self.svc, url, retries=1, json_body={
                "jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_hex]})
            if isinstance(r, dict) and r.get("error"):
                raise EvmRpcError(f"私密通道发送失败: {r['error']}")
            return r.get("result")
        return await self.call("eth_sendRawTransaction", [raw_hex], retries=1)

    async def wait_receipt(self, h: str, timeout: float = 120) -> dict | None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                r = await self.receipt(h)
                if r:
                    return r
            except EvmRpcError:
                pass
            await asyncio.sleep(2)
        return None
