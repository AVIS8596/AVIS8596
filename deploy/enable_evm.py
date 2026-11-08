"""一条命令启用 EVM 链（BSC / ETH / Base），默认用 PublicNode 的免费公共节点，不用注册。

用法：python3 deploy/enable_evm.py bsc base ethereum
      python3 deploy/enable_evm.py off            # 全部关闭
已经在 .env 里填过自己的节点（比如 QuickNode / Alchemy）的链，保留你自己的，不会覆盖。
公共节点有限速，模拟盘够用；实盘或监听的钱包很多时，建议换成 Alchemy 免费账号的节点（同一个账号三条链都能用）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_env import CFG, ENV, EVM, load_env, set_chain, set_env  # noqa: E402

PUBLIC = {
    "bsc": ("https://bsc-rpc.publicnode.com", "wss://bsc-rpc.publicnode.com"),
    "ethereum": ("https://ethereum-rpc.publicnode.com", "wss://ethereum-rpc.publicnode.com"),
    "base": ("https://base-rpc.publicnode.com", "wss://base-rpc.publicnode.com"),
}


def main(args: list[str]) -> None:
    alias = {"eth": "ethereum", "ethereum": "ethereum", "bsc": "bsc", "bnb": "bsc", "base": "base", "off": "off"}
    want = {alias.get(a.lower(), a.lower()) for a in args}
    off = "off" in want
    text, vals = load_env()
    cfg = CFG.read_text(encoding="utf-8")
    on_names = []
    for chain, name, kh, kw in EVM:
        if off or chain not in want:
            if off:
                cfg = set_chain(cfg, chain, False)
            continue
        h, w = vals.get(kh, ""), vals.get(kw, "")
        if not (h.startswith("https://") and w.startswith("wss://")):
            h, w = PUBLIC[chain]
            text = set_env(text, kh, h)
            text = set_env(text, kw, w)
        cfg = set_chain(cfg, chain, True)
        on_names.append(f"{name}（{h.split('/')[2]}）")
    ENV.write_text(text, encoding="utf-8")
    os.chmod(ENV, 0o600)
    CFG.write_text(cfg, encoding="utf-8")
    print("已关闭全部 EVM 链" if off else "已启用：" + ("、".join(on_names) or "无（参数写 bsc / base / ethereum）"))


if __name__ == "__main__":
    main(sys.argv[1:] or ["bsc", "base", "ethereum"])
