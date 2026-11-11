"""手机友好的配置向导：一问一答填写 .env，不用 nano。

用法（在 ~/memebot 目录下）：python3 deploy/setup_env.py
  - 每一项直接粘贴后回车；已经填过的项直接回车就保留原值
  - 某条 EVM 链暂时没有节点地址，直接回车跳过，会自动在 config.yaml 里关掉这条链
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
ENV = BASE / ".env"
CFG = BASE / "config.yaml"

ITEMS = [
    # (变量名, 说明, 校验正则 或 None, 是否必填)
    ("GMGN_API_KEY", "GMGN API Key（gmgn.ai/ai 页面复制）", r"^\S{10,}$", True),
    ("GMGN_WALLET_SOL", "GMGN 的 SOL 钱包地址", r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", True),
    ("GMGN_WALLET_EVM", "GMGN 的 EVM 钱包地址（0x 开头，BSC/ETH/Base 共用）", r"^0x[0-9a-fA-F]{40}$", True),
    ("TELEGRAM_BOT_TOKEN", "Telegram 机器人 token（@BotFather 新建，不要和量化机器人共用）", r"^\d+:[\w-]{20,}$", True),
    ("TELEGRAM_CHAT_ID", "你的 Telegram 数字 ID（@userinfobot 获取）", r"^-?\d+$", True),
    ("HELIUS_API_KEY", "Helius API Key（helius.dev，Solana 实时监听）", r"^[\w-]{20,}$", True),
]
EVM = [
    ("bsc", "BSC", "BSC_RPC_HTTP", "BSC_RPC_WSS"),
    ("ethereum", "ETH", "ETH_RPC_HTTP", "ETH_RPC_WSS"),
    ("base", "Base", "BASE_RPC_HTTP", "BASE_RPC_WSS"),
]


def load_env() -> tuple[str, dict[str, str]]:
    if not ENV.exists():
        shutil.copy(BASE / ".env.example", ENV)
    text = ENV.read_text(encoding="utf-8")
    vals = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            vals[m.group(1)] = m.group(2).strip()
    return text, vals


def set_env(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"(?m)^{key}=", text):
        return re.sub(rf"(?m)^{key}=.*$", lambda _: line, text)
    return text.rstrip("\n") + "\n" + line + "\n"


def mask(v: str) -> str:
    return v if len(v) <= 10 else f"{v[:4]}…{v[-4:]}"


def ask(key: str, desc: str, pattern: str | None, old: str) -> str:
    hint = f"（当前 {mask(old)}，回车保留）" if old else "（回车跳过）"
    while True:
        try:
            v = input(f"\n{desc}\n{key}{hint}: ").strip().strip('"').strip("'")
        except EOFError:
            print("\n已取消")
            sys.exit(1)
        if not v:
            return old
        if pattern and not re.match(pattern, v):
            print("  ⚠️ 格式看起来不对，请检查后重新粘贴（多复制了空格或少复制了字符？）")
            continue
        return v


def set_chain(cfg: str, chain: str, enabled: bool) -> str:
    return re.sub(rf"(?m)^(\s*{chain}:\s*\{{\s*enabled:\s*)(true|false)", rf"\g<1>{'true' if enabled else 'false'}", cfg)


def solana_only() -> None:
    """只填备用 Solana 节点：python3 deploy/setup_env.py solana"""
    text, vals = load_env()
    old = vals.get("SOLANA_RPC_URLS", "")
    urls = [u for u in old.split(",") if u.strip()]
    print("=== 添加 Solana 备用节点 ===")
    if urls:
        print("现在已有：" + "、".join(mask(u) for u in urls))
    print("把节点后台复制的 HTTPS 地址粘贴进来（https:// 开头），回车；不加了直接回车。")
    while True:
        v = ask("SOLANA_RPC_URLS", "新的 Solana 节点 HTTPS 地址", r"^https://\S+$", "")
        if not v:
            break
        if v in urls:
            print("  这个地址已经有了")
            continue
        urls.append(v)
        print(f"  ✅ 已加入（第 {len(urls)} 个）")
    text = set_env(text, "SOLANA_RPC_URLS", ",".join(urls))
    ENV.write_text(text, encoding="utf-8")
    os.chmod(ENV, 0o600)
    print("\n✅ 已保存到 .env。下一步：sudo systemctl restart memebot")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "solana":
        solana_only()
        return
    print("=== 扫链机器人配置向导 ===  粘贴后回车；填错了重新运行本向导即可")
    text, vals = load_env()
    missing = []
    for key, desc, pat, required in ITEMS:
        v = ask(key, desc, pat, vals.get(key, ""))
        text = set_env(text, key, v)
        if required and not v:
            missing.append(key)

    pem = BASE / "gmgn_private.pem"
    if pem.exists():
        text = set_env(text, "GMGN_PRIVATE_KEY_FILE", "gmgn_private.pem")
        text = set_env(text, "GMGN_PRIVATE_KEY", "")
        os.chmod(pem, 0o600)
    else:
        print("\n⚠️ 没找到 gmgn_private.pem，下单签名会失败。请确认私钥文件在 ~/memebot 目录里")

    print("\n--- EVM 链节点（QuickNode 后台复制；某条链暂时没有就直接回车，会自动关闭这条链）---")
    cfg = CFG.read_text(encoding="utf-8")
    enabled = []
    for chain, name, kh, kw in EVM:
        h = ask(kh, f"{name} 节点 HTTPS 地址", r"^https://\S+$", vals.get(kh, ""))
        w = ask(kw, f"{name} 节点 WSS 地址", r"^wss://\S+$", vals.get(kw, "")) if h else ""
        text = set_env(text, kh, h)
        text = set_env(text, kw, w)
        on = bool(h and w)
        cfg = set_chain(cfg, chain, on)
        if on:
            enabled.append(name)
    CFG.write_text(cfg, encoding="utf-8")

    ENV.write_text(text, encoding="utf-8")
    os.chmod(ENV, 0o600)
    print("\n✅ 已保存到 .env")
    print("启用的链：Solana" + ("、" + "、".join(enabled) if enabled else "（EVM 链都没填，已全部关闭，以后补上节点地址再运行本向导即可）"))
    if missing:
        print("⚠️ 还没填：" + "、".join(missing) + "。机器人启动前必须补上")
    print("\n下一步：.venv/bin/python main.py check")


if __name__ == "__main__":
    main()
