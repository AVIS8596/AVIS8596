"""新币发现：
  - Solana：PumpPortal 免费 WebSocket（pump.fun 新币、毕业迁移）
  - EVM：Birdeye 新上线 DEX 的代币（轮询）
pump.fun 每天新币上万个，全量深度分析既贵又没意义，所以：
  新币只入库记录；毕业（迁移到 DEX）的币才做深度分析；
  任何币只要有聪明钱买入，会在信号中心里触发分析（那才是主要信号）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import websockets

from ..sources.birdeye import BudgetExceeded
from ..utils import TTLSet

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.newtok")
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"


class NewTokenMonitor:
    def __init__(self, app: "App"):
        self.app = app
        self.seen = TTLSet(6 * 3600)
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def pumpportal(self) -> None:
        backoff = 2
        while True:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, ping_timeout=30,
                                              open_timeout=15) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    log.info("PumpPortal 已连接")
                    backoff = 2
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        await self._on_pump(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("PumpPortal 断开（%s），%ds 后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _on_pump(self, msg: dict) -> None:
        mint = msg.get("mint")
        if not mint:
            return
        tx_type = str(msg.get("txType", "")).lower()
        if tx_type == "create":
            if not self.seen.add_if_new("c:" + mint):
                return
            await self.app.db.execute(
                "INSERT OR IGNORE INTO tokens(chain,address,symbol,name,creator,created_at,source,first_seen) "
                "VALUES('solana',?,?,?,?,?,?,?)",
                (mint, str(msg.get("symbol", ""))[:32], str(msg.get("name", ""))[:64],
                 msg.get("traderPublicKey", ""), time.time(), "pump_new", time.time()))
            # 创建者是已知聪明钱/已标记的 dev：值得马上看一眼
            creator = msg.get("traderPublicKey", "")
            if creator and await self.app.db.wallet("solana", creator):
                self._spawn(self.app.hub.on_new_token("solana", mint, "聪明钱发币"))
        elif tx_type == "migrate" or "pool" in msg and "signature" in msg and not tx_type:
            if self.seen.add_if_new("m:" + mint):
                self._spawn(self.app.hub.on_new_token("solana", mint, "pump 毕业"))

    async def listings(self, chain: str) -> None:
        """轮询新币（GMGN trenches：已毕业 / 即将毕业的 launchpad 币；或 Birdeye 新上 DEX）。"""
        interval = float(self.app.cfg.get("discovery.new_token_poll_sec", 60))
        min_liq = float(self.app.cfg.get("discovery.min_liquidity_usd", 5000))
        while True:
            try:
                for it in await self.app.data.new_tokens(chain):
                    addr = it["address"]
                    if not self.seen.add_if_new(f"{chain}:{addr}") or it["liquidity"] < min_liq:
                        continue
                    self._spawn(self.app.hub.on_new_token(chain, addr, it["source"]))
            except BudgetExceeded as e:
                log.warning("%s", e)
                await asyncio.sleep(1800)
            except Exception as e:  # noqa: BLE001
                log.warning("%s 新币轮询失败: %s", chain, e)
            await asyncio.sleep(interval)
