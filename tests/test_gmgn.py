"""GMGN 客户端 / 下单执行器 / 数据合并测试（用本地假服务器，不连真实 GMGN）。"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.sources.gmgn as gm  # noqa: E402
from bot.analysis import bundle as bd  # noqa: E402
from bot.analysis.analyzer import merge_gmgn_holders  # noqa: E402


def _pem():
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return k, pem


def test_build_message_matches_cli_format():
    m = gm.build_message("/v1/trade/swap", {"b": "x y", "a": 1, "w": ["z", "c"]}, '{"k":1}', 123)
    assert m == '/v1/trade/swap:a=1&b=x%20y&w=c&w=z:{"k":1}:123'


def test_load_pem_with_escaped_newlines():
    _, pem = _pem()
    key = gm.load_private_key(pem.replace("\n", "\\n"))
    assert gm.sign("hello", key)


def _run_server(handler):
    async def start():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}"
    return start


def test_client_auth_signature_and_envelope():
    key, pem = _pem()
    pub = key.public_key()
    seen = {}

    async def handler(req: web.Request):
        body = await req.text()
        q = dict(req.query)
        seen["path"], seen["q"], seen["headers"], seen["body"] = req.path, q, dict(req.headers), body
        if req.path == "/v1/trade/swap":
            msg = gm.build_message(req.path, q, body, int(q["timestamp"]))
            pub.verify(base64.b64decode(req.headers["X-Signature"]), msg.encode())   # 签名不对会抛异常 → 500
            return web.json_response({"code": 0, "data": {"order_id": "o1", "status": "pending"}})
        return web.json_response({"code": 0, "data": {"price": {"price": "0.5"}, "symbol": "T"}})

    async def go():
        runner, url = await _run_server(handler)()
        old = gm.HOST
        gm.HOST = url
        try:
            c = gm.Gmgn("KEY123", pem, "pro")
            info = await c.token_info("solana", "Mint111")
            assert info["symbol"] == "T"
            assert seen["headers"]["X-APIKEY"] == "KEY123" and "X-Signature" not in seen["headers"]
            assert seen["q"]["chain"] == "sol" and "timestamp" in seen["q"] and "client_id" in seen["q"]
            r = await c.swap({"chain": "sol", "input_amount": "1000", "slippage": 15, "is_anti_mev": True})
            assert r["order_id"] == "o1"
            assert json.loads(seen["body"])["slippage"] == 15
            await c.close()
        finally:
            gm.HOST = old
            await runner.cleanup()

    asyncio.run(go())


def test_client_rate_limit_pauses_all_requests():
    calls = {"n": 0}

    async def handler(req):
        calls["n"] += 1
        return web.json_response({"code": 429, "error": "RATE_LIMIT_BANNED", "reset_at": int(time.time()) + 300},
                                 status=429)

    async def go():
        runner, url = await _run_server(handler)()
        old = gm.HOST
        gm.HOST = url
        try:
            c = gm.Gmgn("K", "", "plus")
            with pytest.raises(gm.GmgnError):
                await c.request("GET", "/v1/token/info", {"chain": "sol"}, retries=0)
            assert c.paused_until > time.time() + 250      # 记住了解封时间，后续请求会先等待
            assert calls["n"] == 1
            await c.close()
        finally:
            gm.HOST = old
            await runner.cleanup()

    asyncio.run(go())


def test_plan_upgrade_disables_endpoint():
    async def handler(req):
        return web.json_response({"code": 403, "error": "PLAN_UPGRADE_REQUIRED", "upgrade_message": "upgrade"},
                                 status=403)

    async def go():
        runner, url = await _run_server(handler)()
        old = gm.HOST
        gm.HOST = url
        try:
            c = gm.Gmgn("K", "", "free")
            with pytest.raises(gm.GmgnError):
                await c.top_holders("solana", "M")
            assert "/v1/market/token_top_holders" in c.disabled
            assert await c.top_holders("solana", "M") == []      # 之后直接跳过，不再请求
            await c.close()
        finally:
            gm.HOST = old
            await runner.cleanup()

    asyncio.run(go())


def test_merge_gmgn_holders():
    buys = [bd.EarlyBuy("A", 100, 0, 1, 10)]
    funding = {}
    holders = [
        {"address": "POOL", "addr_type": 2, "amount_percentage": 0.5, "maker_token_tags": ["bundler"]},
        {"address": "A", "addr_type": 0, "amount_percentage": 0.02, "maker_token_tags": ["sniper"],
         "native_transfer": {"address": "F1", "timestamp": 50}},
        {"address": "B", "addr_type": 0, "amount_percentage": 0.03, "maker_token_tags": ["bundler"],
         "native_transfer": {"address": "F1", "timestamp": 60}},
        {"address": "C", "addr_type": 0, "amount_percentage": 0.04, "tags": ["rat_trader"]},
        {"address": "D", "addr_type": 0, "amount_percentage": 0.01},
    ]
    held = merge_gmgn_holders(holders, buys, funding, 1_000_000, 100, 1000, "DEV",
                              {"dev": {"fund_from": "F0", "fund_from_ts": 10}})
    assert "POOL" not in held and held["B"] == 30_000
    w = {b.wallet: b for b in buys}
    assert "sniper(GMGN)" in w["A"].tags and "B" in w and "C" in w and "D" not in w
    assert funding["A"].funder == "F1" and funding["DEV"].funder == "F0"
    r = bd.detect(creator="DEV", launch_slot=100, launch_ts=1000, buys=buys, funding=funding,
                  supply_raw=1_000_000, holdings=held)
    assert r.verdicts["B"].cls == "bundle"
    assert r.verdicts["C"].cls == "insider"


def test_gmgn_executor_buy_and_sell_flow():
    from bot.trade.executors import GmgnExecutor

    class FakeGm:
        pk = object()

        def __init__(self):
            self.params = []

        async def swap(self, p):
            self.params.append(p)
            return {"order_id": "o", "status": "pending"}

        async def query_order(self, chain, oid):
            p = self.params[-1]
            if p["input_token"] == GmgnExecutor.EVM_NATIVE:
                return {"status": "confirmed", "hash": "0xh",
                        "report": {"input_amount": str(10 ** 16), "output_amount": "5000", "output_token_decimals": 18}}
            return {"status": "confirmed", "hash": "0xs",
                    "report": {"input_amount": "5000", "output_amount": str(2 * 10 ** 16), "output_token_decimals": 18}}

    bal = {"tok": 0}

    class FakeRpc:
        async def gas_price(self):
            return 1 * 10 ** 9

        async def erc20_balance(self, t, a):
            return bal["tok"]

        async def erc20_decimals(self, t):
            return 18

        async def balance(self, a):
            return 10 ** 18

    cfg = SimpleNamespace(secrets=SimpleNamespace(gmgn_wallet_sol="", gmgn_wallet_evm="0xabc"),
                          get=lambda k, d=None: {"gmgn": {"anti_mev": True}}.get(k, d),
                          per_chain=lambda k, c, d=None: 5)
    fake = FakeGm()
    app = SimpleNamespace(gmgn=fake, cfg=cfg, evm={"base": FakeRpc(), "bsc": FakeRpc()}, sol=None)

    async def go():
        ex = GmgnExecutor(app, "base")
        f1 = await ex.buy("tok", 0.01, 1500)
        assert f1.ok and f1.token_raw == 5000 and f1.native == pytest.approx(0.01)
        p = fake.params[-1]
        assert p["slippage"] == 15 and "is_anti_mev" not in p        # Base 不支持防夹，自动关闭
        assert p["input_amount"] == str(10 ** 16) and p["from_address"] == "0xabc"
        bal["tok"] = 5000
        f2 = await ex.sell("tok", 5000, 18, 1500)
        assert f2.ok and f2.native == pytest.approx(0.02)
        assert fake.params[-1]["input_amount_bps"] == "10000"        # 全部卖出用百分比
        ex2 = GmgnExecutor(app, "bsc")
        await ex2.buy("tok", 0.01, 1200)
        assert fake.params[-1]["is_anti_mev"] is True

    asyncio.run(go())
