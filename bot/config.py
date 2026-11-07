"""加载 config.yaml + .env，并在启动时检查配置是否完整。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

EVM_CHAINS = ("bsc", "ethereum", "base")
ALL_CHAINS = ("solana",) + EVM_CHAINS

_RPC_ENV = {
    "ethereum": ("ETH_RPC_HTTP", "ETH_RPC_WSS"),
    "bsc": ("BSC_RPC_HTTP", "BSC_RPC_WSS"),
    "base": ("BASE_RPC_HTTP", "BASE_RPC_WSS"),
}


class ConfigError(Exception):
    pass


@dataclass
class Secrets:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    helius_key: str = ""
    jupiter_key: str = ""
    birdeye_key: str = ""
    goplus_key: str = ""
    etherscan_key: str = ""
    zerox_key: str = ""
    solana_private_key: str = ""
    evm_private_key: str = ""
    gmgn_key: str = ""
    gmgn_private_pem: str = ""
    gmgn_wallet_sol: str = ""
    gmgn_wallet_evm: str = ""
    rpc_http: dict[str, str] = field(default_factory=dict)
    rpc_wss: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # 防止私钥被打印到日志
        return "Secrets(***)"


class Config:
    def __init__(self, data: dict[str, Any], secrets: Secrets, base_dir: Path):
        self.raw = data
        self.secrets = secrets
        self.base_dir = base_dir

    # ---- 便捷读取 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def per_chain(self, dotted: str, chain: str, default: Any = None) -> Any:
        v = self.get(dotted, {}) or {}
        return v.get(chain, default) if isinstance(v, dict) else default

    @property
    def dry_run(self) -> bool:
        return bool(self.get("general.dry_run", True))

    @property
    def enabled_chains(self) -> list[str]:
        out = []
        for c in ALL_CHAINS:
            if self.get(f"chains.{c}.enabled", False):
                out.append(c)
        return out

    @property
    def evm_chains(self) -> list[str]:
        return [c for c in self.enabled_chains if c in EVM_CHAINS]

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p


def _read_pem(inline: str, path: str, base: Path) -> str:
    """GMGN 私钥：可以直接写在 .env（换行写成 \\n），也可以写文件路径。"""
    if inline.strip():
        return inline.strip().strip('"').replace("\\n", "\n")
    if path.strip():
        p = Path(path.strip())
        p = p if p.is_absolute() else base / p
        if p.exists():
            return p.read_text().strip()
    return ""


def load_config(base_dir: str | Path = ".", config_file: str = "config.yaml") -> Config:
    base = Path(base_dir).resolve()
    load_dotenv(base / ".env")
    cfg_path = base / config_file
    if not cfg_path.exists():
        raise ConfigError(f"找不到配置文件 {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    env = os.environ.get
    s = Secrets(
        telegram_token=env("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=env("TELEGRAM_CHAT_ID", "").strip(),
        helius_key=env("HELIUS_API_KEY", "").strip(),
        jupiter_key=env("JUPITER_API_KEY", "").strip(),
        birdeye_key=env("BIRDEYE_API_KEY", "").strip(),
        goplus_key=env("GOPLUS_API_KEY", "").strip(),
        etherscan_key=env("ETHERSCAN_API_KEY", "").strip(),
        zerox_key=env("ZEROX_API_KEY", "").strip(),
        solana_private_key=env("SOLANA_PRIVATE_KEY", "").strip(),
        evm_private_key=env("EVM_PRIVATE_KEY", "").strip(),
        gmgn_key=env("GMGN_API_KEY", "").strip(),
        gmgn_private_pem=_read_pem(env("GMGN_PRIVATE_KEY", ""), env("GMGN_PRIVATE_KEY_FILE", ""), base),
        gmgn_wallet_sol=env("GMGN_WALLET_SOL", "").strip(),
        gmgn_wallet_evm=env("GMGN_WALLET_EVM", "").strip().lower(),
    )
    for chain, (h, w) in _RPC_ENV.items():
        s.rpc_http[chain] = env(h, "").strip()
        s.rpc_wss[chain] = env(w, "").strip()

    cfg = Config(data, s, base)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> list[str]:
    """检查必填项。缺少会导致功能无法运行的直接报错；可选项只给警告。"""
    errors: list[str] = []
    warnings: list[str] = []
    s = cfg.secrets
    chains = cfg.enabled_chains

    if not chains:
        errors.append("config.yaml 里至少要启用一条链")
    if not s.telegram_token or not s.telegram_chat_id:
        errors.append("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    provider = cfg.get("data.provider", "gmgn")
    executor = cfg.get("trading.executor", "gmgn")
    if provider == "gmgn" and not s.gmgn_key:
        errors.append("数据源是 GMGN，但缺少 GMGN_API_KEY")
    if provider == "birdeye" and not s.birdeye_key:
        errors.append("数据源是 Birdeye，但缺少 BIRDEYE_API_KEY")

    trading_live = cfg.get("trading.enabled", False) and not cfg.dry_run
    if "solana" in chains and not s.helius_key:
        errors.append("启用了 Solana 但缺少 HELIUS_API_KEY（实时监听聪明钱要用）")
    for c in cfg.evm_chains:
        if not s.rpc_http.get(c) or not s.rpc_wss.get(c):
            errors.append(f"启用了 {c} 但缺少它的 RPC_HTTP / RPC_WSS 地址")
        elif not s.rpc_wss[c].startswith("wss://"):
            errors.append(f"{c} 的 RPC_WSS 必须以 wss:// 开头")
    if trading_live and executor == "gmgn":
        if not s.gmgn_key:
            errors.append("用 GMGN 下单需要 GMGN_API_KEY")
        if not s.gmgn_private_pem:
            errors.append("用 GMGN 下单需要 GMGN_PRIVATE_KEY 或 GMGN_PRIVATE_KEY_FILE（申请 API 时生成的 Ed25519 私钥）")
        if "solana" in chains and not s.gmgn_wallet_sol:
            errors.append("用 GMGN 在 Solana 下单需要 GMGN_WALLET_SOL（绑定在 API Key 上的钱包地址）")
        if cfg.evm_chains and not s.gmgn_wallet_evm:
            errors.append("用 GMGN 在 EVM 链下单需要 GMGN_WALLET_EVM（绑定在 API Key 上的钱包地址）")
    if trading_live and executor == "self":
        if "solana" in chains and (not s.solana_private_key or not s.jupiter_key):
            errors.append("自托管下单（Solana）需要 SOLANA_PRIVATE_KEY 和 JUPITER_API_KEY")
        if cfg.evm_chains and (not s.evm_private_key or not s.zerox_key):
            errors.append("自托管下单（EVM）需要 EVM_PRIVATE_KEY 和 ZEROX_API_KEY")
    if s.gmgn_private_pem and "PRIVATE KEY" not in s.gmgn_private_pem:
        errors.append("GMGN 私钥格式不对，应该是 -----BEGIN PRIVATE KEY----- 开头的 PEM 内容")
    if cfg.evm_chains and not s.etherscan_key:
        warnings.append("未填 ETHERSCAN_API_KEY：EVM 链的资金溯源（捆绑出资人识别）将跳过")

    if s.solana_private_key and s.solana_private_key.startswith("["):
        errors.append("SOLANA_PRIVATE_KEY 请填 base58 字符串，不要填 [1,2,3...] 数组格式")
    if s.evm_private_key and not (s.evm_private_key.startswith("0x") and len(s.evm_private_key) == 66):
        errors.append("EVM_PRIVATE_KEY 格式不对，应为 0x 开头的 64 位十六进制")

    sl = cfg.get("exit.stop_loss_pct", -35)
    if sl is not None and sl >= 0:
        errors.append("exit.stop_loss_pct 必须是负数，例如 -35")

    cfg.warnings = warnings  # type: ignore[attr-defined]
    if errors:
        raise ConfigError("配置有问题：\n- " + "\n- ".join(errors))
    return warnings
