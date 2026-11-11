"""一条命令启用 EVM 链（BSC / ETH / Base），默认用 PublicNode 的免费公共节点，不用注册。

用法：python3 deploy/enable_evm.py bsc base ethereum robinhood
      python3 deploy/enable_evm.py off            # 全部关闭
      python3 deploy/enable_evm.py only solana bsc   # 只跑这几条链，其他的都关掉（节点地址保留，以后再开不用重填）
罗宾汉链不需要填节点：机器人内置了免费公共节点（没有 WebSocket，自动改成每几秒轮询）。
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


ALL = ("solana", "bsc", "ethereum", "base", "robinhood")
NAMES = {"solana": "Solana", "bsc": "BSC", "ethereum": "Ethereum", "base": "Base", "robinhood": "罗宾汉链"}


def open_positions(chains: list[str]) -> dict[str, int]:
    """这些链上还没卖掉的持仓数（关掉链以后机器人不再管它们，要先提醒）。"""
    import sqlite3
    db = CFG.parent / "data" / "memebot.db"
    if not db.exists() or not chains:
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        marks = ",".join("?" * len(chains))
        rows = con.execute(f"SELECT chain, COUNT(*) FROM positions WHERE status='open' AND chain IN ({marks}) "
                           f"GROUP BY chain", chains).fetchall()
        con.close()
        return {c: n for c, n in rows}
    except Exception:  # noqa: BLE001
        return {}


def only(want: set[str]) -> None:
    bad = want - set(ALL)
    if bad or not want:
        print("写法：python3 deploy/enable_evm.py only solana bsc   （可选：solana bsc ethereum base robinhood）")
        return
    cfg = CFG.read_text(encoding="utf-8")
    off = [c for c in ALL if c not in want]
    held = open_positions(off)
    if held:
        print("⚠️ 要关掉的链上还有没卖掉的持仓：" + "、".join(f"{NAMES[c]} {n} 个" for c, n in held.items()))
        print("   关掉以后机器人不再管这些持仓（不会止盈止损）。先在 Telegram 用 /sell 卖掉，或者等它们平仓后再运行本命令。")
        print("   没有改动。")
        return
    for c in ALL:
        cfg = set_chain(cfg, c, c in want)
    CFG.write_text(cfg, encoding="utf-8")
    print("只跑：" + "、".join(NAMES[c] for c in ALL if c in want) + "；已关闭：" + ("、".join(NAMES[c] for c in off) or "无"))
    print("下一步：sudo systemctl restart memebot")


def main(args: list[str]) -> None:
    alias = {"eth": "ethereum", "ethereum": "ethereum", "bsc": "bsc", "bnb": "bsc", "base": "base", "off": "off",
             "robinhood": "robinhood", "hood": "robinhood", "rh": "robinhood", "sol": "solana", "solana": "solana"}
    if args and args[0].lower() == "only":
        only({alias.get(a.lower(), a.lower()) for a in args[1:]})
        return
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
    if off or "robinhood" in want:
        cfg = set_chain(cfg, "robinhood", not off)
        if not off:
            on_names.append("罗宾汉链（内置公共节点）")
    ENV.write_text(text, encoding="utf-8")
    os.chmod(ENV, 0o600)
    CFG.write_text(cfg, encoding="utf-8")
    print("已关闭全部 EVM 链" if off else "已启用：" + ("、".join(on_names) or "无（参数写 bsc / base / ethereum / robinhood）"))


if __name__ == "__main__":
    main(sys.argv[1:] or ["bsc", "base", "ethereum"])
