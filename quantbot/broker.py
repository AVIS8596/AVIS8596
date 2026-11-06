"""券商接口: 模拟盘 (PaperBroker) 与 ccxt 实盘 (CcxtBroker)。"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class Fill:
    time: str
    symbol: str
    qty: float  # 正数买入, 负数卖出
    price: float
    fee: float
    reason: str = ""


class Broker(ABC):
    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_position(self, symbol: str) -> float: ...

    @abstractmethod
    def market_order(self, symbol: str, qty: float, price_hint: float, reason: str = "") -> Fill | None:
        """下市价单。qty 为带符号数量; price_hint 为当前参考价 (模拟盘用它成交)。"""

    def equity(self, prices: dict[str, float]) -> float:
        return self.get_cash() + sum(self.get_position(s) * p for s, p in prices.items())


@dataclass
class PaperBroker(Broker):
    cash: float = 100_000.0
    commission: float = 0.001
    slippage: float = 0.0005
    positions: dict[str, float] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    state_file: str | None = None

    def __post_init__(self) -> None:
        if self.state_file and os.path.exists(self.state_file):
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            self.cash = st["cash"]
            self.positions = st["positions"]
            self.fills = [Fill(**x) for x in st.get("fills", [])]
            log.info("已从 %s 恢复模拟盘状态", self.state_file)

    def get_cash(self) -> float:
        return self.cash

    def get_position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def market_order(self, symbol: str, qty: float, price_hint: float, reason: str = "", time: str | None = None) -> Fill | None:
        if qty == 0:
            return None
        price = price_hint * (1 + self.slippage if qty > 0 else 1 - self.slippage)
        fee = abs(qty * price) * self.commission
        self.cash -= qty * price + fee
        new_pos = self.positions.get(symbol, 0.0) + qty
        self.positions[symbol] = 0.0 if abs(new_pos) < 1e-12 else new_pos
        fill = Fill(time or datetime.now(timezone.utc).isoformat(), symbol, qty, price, fee, reason)
        self.fills.append(fill)
        self._save()
        return fill

    def _save(self) -> None:
        if not self.state_file:
            return
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"cash": self.cash, "positions": self.positions, "fills": [asdict(x) for x in self.fills]},
                f,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, self.state_file)


class CcxtBroker(Broker):  # pragma: no cover - 需要真实交易所
    """ccxt 现货实盘券商。

    API 密钥从环境变量读取: QUANTBOT_API_KEY / QUANTBOT_API_SECRET。
    出于安全考虑默认 dry_run=True, 只打印订单而不真正下单。
    """

    def __init__(self, exchange: str = "binance", quote: str = "USDT", dry_run: bool = True, sandbox: bool = False) -> None:
        import ccxt  # type: ignore

        self.ex = getattr(ccxt, exchange)(
            {
                "apiKey": os.environ.get("QUANTBOT_API_KEY", ""),
                "secret": os.environ.get("QUANTBOT_API_SECRET", ""),
                "enableRateLimit": True,
            }
        )
        if sandbox:
            self.ex.set_sandbox_mode(True)
        self.quote = quote
        self.dry_run = dry_run

    def _balance(self) -> dict:
        return self.ex.fetch_balance()

    def get_cash(self) -> float:
        return float(self._balance().get("free", {}).get(self.quote, 0.0) or 0.0)

    def get_position(self, symbol: str) -> float:
        base = symbol.split("/")[0]
        return float(self._balance().get("total", {}).get(base, 0.0) or 0.0)

    def market_order(self, symbol: str, qty: float, price_hint: float, reason: str = "") -> Fill | None:
        if qty == 0:
            return None
        side = "buy" if qty > 0 else "sell"
        amount = float(self.ex.amount_to_precision(symbol, abs(qty)))
        if amount <= 0:
            return None
        now = datetime.now(timezone.utc).isoformat()
        if self.dry_run:
            log.warning("[DRY RUN] %s %s %s @~%.4f (%s)", side, amount, symbol, price_hint, reason)
            return Fill(now, symbol, amount if qty > 0 else -amount, price_hint, 0.0, f"dry_run:{reason}")
        order = self.ex.create_order(symbol, "market", side, amount)
        price = float(order.get("average") or order.get("price") or price_hint)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)
        filled = float(order.get("filled") or amount)
        return Fill(now, symbol, filled if qty > 0 else -filled, price, fee, reason)
