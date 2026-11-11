"""罗宾汉链：配置、接口按链停用、轮询监听（不连网）。"""
import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

from bot.chains import CHAINS, NO_FEED_CHAINS, PUBLIC_RPC, TRANSFER_TOPIC, is_quote_token  # noqa: E402
from bot.sources.evm_rpc import pad_addr  # noqa: E402


def test_chain_constants():
    rh = CHAINS["robinhood"]
    assert rh.chain_id == 4663 and rh.native_symbol == "ETH" and rh.goplus_id is None
    assert is_quote_token("robinhood", rh.wrapped) and is_quote_token("robinhood", rh.wrapped.upper().replace("0X", "0x"))
    assert "robinhood" not in NO_FEED_CHAINS and PUBLIC_RPC["robinhood"].startswith("https://")


def test_config_uses_public_rpc(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / ".env").write_text("")
    for k, v in {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "1", "GMGN_API_KEY": "k" * 12,
                 "HELIUS_API_KEY": "h" * 24, "ROBINHOOD_RPC_HTTP": "", "ROBINHOOD_RPC_WSS": ""}.items():
        monkeypatch.setenv(k, v)
    for k in ("BSC", "ETH", "BASE"):
        monkeypatch.setenv(f"{k}_RPC_HTTP", "https://x")
        monkeypatch.setenv(f"{k}_RPC_WSS", "wss://x")
    from bot.config import load_config
    cfg = load_config(tmp_path)
    assert "robinhood" in cfg.evm_chains
    assert cfg.secrets.rpc_http["robinhood"] == PUBLIC_RPC["robinhood"] and not cfg.secrets.rpc_wss["robinhood"]
    assert cfg.per_chain("trading.buy_amount_native", "robinhood") > 0


def test_feed_short_circuit_and_per_chain_disable():
    from bot.sources.gmgn import CHAIN_MAP, Gmgn
    assert CHAIN_MAP["robinhood"] == "robinhood"
    g = Gmgn("k" * 12)

    async def go():
        g.disabled.add("/v1/user/smartmoney@robinhood")          # 只停罗宾汉链
        assert await g.request("GET", "/v1/user/smartmoney", {"chain": "robinhood"}) is None
        await g.close()
    asyncio.run(go())
    # 其他链没被停：key 不同
    assert "/v1/user/smartmoney@sol" not in g.disabled and "/v1/user/smartmoney" not in g.disabled


def test_poll_monitor_on_log():
    from bot.monitor.evm_wallets import EvmWalletMonitor
    w = "0x" + "ab" * 20
    handled = []
    mon = EvmWalletMonitor.__new__(EvmWalletMonitor)
    from bot.utils import TTLSet
    mon.seen, mon._tasks, mon.chain = TTLSet(60), set(), "robinhood"
    from collections import deque
    mon.hits, mon.sigs = deque(), deque()

    async def fake_handle(h, wallet):
        handled.append((h, wallet))
    mon._handle = fake_handle

    async def go():
        lg = {"transactionHash": "0x1", "topics": [TRANSFER_TOPIC, "0x" + pad_addr("0x" + "cd" * 20), "0x" + pad_addr(w)]}
        mon._on_log(lg, {w})
        mon._on_log(lg, {w})                     # 同一笔去重
        mon._on_log({**lg, "removed": True, "transactionHash": "0x2"}, {w})
        await asyncio.sleep(0)
    asyncio.run(go())
    assert handled == [("0x1", w)]


def test_enable_script_toggles(tmp_path):
    sys.path.insert(0, str(ROOT / "deploy"))
    from setup_env import set_chain
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    off = set_chain(cfg, "robinhood", False)
    assert "robinhood: { enabled: false }" in off and "solana:   { enabled: true }" in off


def test_pick_chain_alias():
    from bot.notify.telegram import CHAIN_ALIAS, Telegram
    assert CHAIN_ALIAS["hood"] == "robinhood"
    tg = Telegram.__new__(Telegram)

    async def ov(c, t):
        return {"price": 1.0 if c == "robinhood" else 0}
    tg.app = SimpleNamespace(cfg=SimpleNamespace(enabled_chains=["solana", "bsc", "robinhood"],
                                                 evm_chains=["bsc", "robinhood"]),
                             data=SimpleNamespace(overview=ov))
    assert asyncio.run(tg._pick_chain(["0xAbC"])) == ("robinhood", ["0xAbC"])
    assert asyncio.run(tg._pick_chain(["bsc", "0xAbC"])) == ("bsc", ["0xAbC"])
    assert asyncio.run(tg._pick_chain(["So1abc"]))[0] == "solana"
