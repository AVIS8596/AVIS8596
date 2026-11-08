"""把所有模块组装在一起。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .db import DB
from .http import Http
from .sources.aux import Etherscan, GoPlus
from .sources.birdeye import Birdeye
from .sources.evm_rpc import EvmRPC
from .sources.solana_rpc import SolanaRPC

log = logging.getLogger("memebot.app")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        s = cfg.secrets
        b = cfg.get("budget", {}) or {}
        self.http = Http()
        self.db = DB(cfg.path(cfg.get("general.db_path", "data/memebot.db")))
        self.tz = ZoneInfo(cfg.get("general.timezone", "UTC"))
        self.birdeye = Birdeye(self.http, s.birdeye_key, int(b.get("birdeye_daily_cu", 300000)),
                               float(b.get("birdeye_rps", 40)))
        self.gmgn = None
        if s.gmgn_key:
            from .sources.gmgn import Gmgn
            self.gmgn = Gmgn(s.gmgn_key, s.gmgn_private_pem, cfg.get("gmgn.plan", "plus"),
                             float(cfg.get("gmgn.rate_usage", 0.8)))
        self.goplus = GoPlus(self.http, float(b.get("goplus_rps", 2)))
        self.etherscan = Etherscan(self.http, s.etherscan_key, float(b.get("etherscan_rps", 4)))
        self.sol: SolanaRPC | None = None
        if "solana" in cfg.enabled_chains:
            self.sol = SolanaRPC(self.http, s.helius_key, float(b.get("helius_rps", 6)),
                                 float(b.get("helius_trade_rps", 3)))
        from .chains import PUBLIC_RPC
        # 用内置免费公共节点时限速低一些，免得被节点封
        self.evm: dict[str, EvmRPC] = {
            c: EvmRPC(self.http, c, s.rpc_http[c], 8 if s.rpc_http[c] == PUBLIC_RPC.get(c) else 25)
            for c in cfg.evm_chains}
        self.wallets_changed: dict[str, asyncio.Event] = {c: asyncio.Event() for c in cfg.enabled_chains}
        self._native_px: dict[str, tuple[float, float]] = {}
        self.started_at = time.time()

        # 下面这些在 main 里创建（互相引用）
        from .analysis.analyzer import Analyzer
        from .data import DataHub
        self.data = DataHub(self)
        from .notify.telegram import Telegram
        from .signals import SignalHub
        from .discovery.smart_money import SmartMoney
        from .trade.positions import PositionManager
        from .trade.trader import Trader
        self.tg = Telegram(self)
        self.analyzer = Analyzer(self)
        self.trader = Trader(self)
        self.positions = PositionManager(self)
        self.hub = SignalHub(self)
        self.smart = SmartMoney(self)

    async def start(self) -> None:
        await self.http.start()
        await self.db.open()

    async def close(self) -> None:
        if self.gmgn:
            await self.gmgn.close()
        await self.http.close()
        await self.db.close()

    async def native_price(self, chain: str) -> float:
        """原生币美元价格，缓存 60 秒。取不到时返回上次的值，避免 0 价格导致误判。"""
        cached = self._native_px.get(chain)
        if cached and time.time() - cached[1] < 60:
            return cached[0]
        p = await self.data.native_usd(chain)
        if p > 0:
            self._native_px[chain] = (p, time.time())
            return p
        return cached[0] if cached else 0.0

    def day_start(self) -> float:
        now = datetime.now(self.tz)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def fmt_time(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%m-%d %H:%M:%S")
