"""公共节点上的监听：一条连接发到约 100 个订阅就被踢（1013），所以拆成每条 40 个。"""
import asyncio
import json
from types import SimpleNamespace

import websockets

import bot.monitor.solana_wallets as sw
from bot.monitor.solana_wallets import PUBLIC, PUBLIC_PER_CONN, SolanaWalletMonitor
from bot.sources.solana_rpc import Endpoint


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_public_node_split(monkeypatch):
    monkeypatch.setattr(sw, "PUBLIC_SUB_GAP", 0.001)
    kicked, per_conn = [], []

    async def handler(ws):
        n = 0
        per_conn.append(0)
        i = len(per_conn) - 1
        async for raw in ws:
            msg = json.loads(raw)
            n += 1
            per_conn[i] = n
            if n > 100:                       # 像公共节点一样：订阅发太多就踢
                kicked.append(1)
                await ws.close(1013, "Too many subscriptions attempted")
                return
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": 1000 * i + n}))

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 8799):
            ep = Endpoint(PUBLIC, "https://api.mainnet-beta.solana.com", "ws://127.0.0.1:8799", "x")
            sol = SimpleNamespace(ws_pick=lambda: ep, ws_ok=lambda e: None, ws_note=lambda e, n=1: None,
                                  ws_should_move=lambda e: False, ws_failed=lambda e, why: False, wss=ep.wss)
            wallets = [f"W{i}" for i in range(300)]

            async def pick(n):
                return wallets
            ev = asyncio.Event()
            app = SimpleNamespace(sol=sol, cfg=Cfg({"smart_money.public_ws_max": 160}), wallets_changed={"solana": ev})
            mon = SolanaWalletMonitor(app)
            mon.pick_wallets = pick
            t = asyncio.create_task(mon.run())
            for _ in range(100):
                await asyncio.sleep(0.1)
                if sum(mon.live.values()) >= 160:
                    break
            assert sum(mon.live.values()) == 160, mon.live      # 排名前 160 个都订上了
            assert len(mon.live) == 4 and max(per_conn) <= PUBLIC_PER_CONN and not kicked
            assert mon.capped == 160
            # 一条按别的节点分的大连接落到公共节点：自己退出，让 run() 重新分
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            await asyncio.wait_for(mon._conn(0, wallets), 2)
    asyncio.run(go())
