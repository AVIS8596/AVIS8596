"""各链常量。"""
from __future__ import annotations

from dataclasses import dataclass

NATIVE_EVM_PLACEHOLDER = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"  # 0x API 用这个代表原生币
SOL_MINT = "So11111111111111111111111111111111111111112"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@dataclass(frozen=True)
class ChainInfo:
    key: str
    name: str
    native_symbol: str
    native_decimals: int
    birdeye: str                   # Birdeye x-chain 名
    wrapped: str                   # 包装原生币地址（价格查询用）
    chain_id: int | None = None
    goplus_id: str | None = None
    explorer_tx: str = ""
    explorer_token: str = ""
    explorer_addr: str = ""
    quote_tokens: frozenset[str] = frozenset()   # 计价币（不当作 meme 币）


CHAINS: dict[str, ChainInfo] = {
    "solana": ChainInfo(
        key="solana", name="Solana", native_symbol="SOL", native_decimals=9, birdeye="solana",
        wrapped=SOL_MINT, goplus_id="solana",
        explorer_tx="https://solscan.io/tx/{}", explorer_token="https://gmgn.ai/sol/token/{}",
        explorer_addr="https://gmgn.ai/sol/address/{}",
        quote_tokens=frozenset({
            SOL_MINT,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
            "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",   # USD1
        }),
    ),
    "bsc": ChainInfo(
        key="bsc", name="BSC", native_symbol="BNB", native_decimals=18, birdeye="bsc",
        wrapped="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", chain_id=56, goplus_id="56",
        explorer_tx="https://bscscan.com/tx/{}", explorer_token="https://gmgn.ai/bsc/token/{}",
        explorer_addr="https://gmgn.ai/bsc/address/{}",
        quote_tokens=frozenset(a.lower() for a in {
            "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",  # WBNB
            "0x55d398326f99059fF775485246999027B3197955",  # USDT
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",  # USDC
            "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",  # BUSD
            "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",  # USD1
        }),
    ),
    "ethereum": ChainInfo(
        key="ethereum", name="Ethereum", native_symbol="ETH", native_decimals=18, birdeye="ethereum",
        wrapped="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", chain_id=1, goplus_id="1",
        explorer_tx="https://etherscan.io/tx/{}", explorer_token="https://gmgn.ai/eth/token/{}",
        explorer_addr="https://gmgn.ai/eth/address/{}",
        quote_tokens=frozenset(a.lower() for a in {
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
        }),
    ),
    "base": ChainInfo(
        key="base", name="Base", native_symbol="ETH", native_decimals=18, birdeye="base",
        wrapped="0x4200000000000000000000000000000000000006", chain_id=8453, goplus_id="8453",
        explorer_tx="https://basescan.org/tx/{}", explorer_token="https://gmgn.ai/base/token/{}",
        explorer_addr="https://gmgn.ai/base/address/{}",
        quote_tokens=frozenset(a.lower() for a in {
            "0x4200000000000000000000000000000000000006",  # WETH
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
            "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",  # USDT
        }),
    ),
    # 罗宾汉链：Arbitrum Orbit L2，chain id 4663，Gas 用 ETH；GoPlus 暂不支持（安全检测只用 GMGN）
    "robinhood": ChainInfo(
        key="robinhood", name="Robinhood", native_symbol="ETH", native_decimals=18, birdeye="robinhood",
        wrapped="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", chain_id=4663, goplus_id=None,
        explorer_tx="https://robinhoodchain.blockscout.com/tx/{}",
        explorer_token="https://gmgn.ai/robinhood/token/{}",
        explorer_addr="https://gmgn.ai/robinhood/address/{}",
        quote_tokens=frozenset(a.lower() for a in {
            "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",  # WETH
            "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",  # USDG
            "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34",  # USDe
        }),
    ),
}

# GMGN 聪明钱 / KOL 交易流还不支持的链：钱包只能靠自己从大涨币里挖
NO_FEED_CHAINS = frozenset({"robinhood"})
# 没有免费 WebSocket 的链默认用的公共 HTTP 节点（.env 里填了自己的就用自己的）
PUBLIC_RPC = {"robinhood": "https://rpc.nodeflare.app/robinhood/public"}


def norm_addr(chain: str, addr: str) -> str:
    """EVM 地址统一小写存储；Solana 地址区分大小写，原样保留。"""
    if not addr:
        return addr
    return addr if chain == "solana" else addr.lower()


def is_quote_token(chain: str, addr: str) -> bool:
    return norm_addr(chain, addr) in CHAINS[chain].quote_tokens or (
        chain != "solana" and addr.lower() == NATIVE_EVM_PLACEHOLDER.lower())
