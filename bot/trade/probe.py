"""试探仓：信号很强、只是筹码"表面指标"超限时（刚发币几分钟里前10持仓、捆绑交易占比天然偏高），先小仓位建仓再观察。

只放宽这两类表面指标；真正说明"阴谋盘"的证据一个不放宽：老鼠仓 / 埋伏地址、已知阴谋地址、团伙、坏项目方、
同名重发、批量发币、刷量、跑路风险、dev 持仓、蜜罐 / 致命风险。
"""
from __future__ import annotations

PROBE = "试探仓"
STRUCT_PREFIX = ("捆绑地址持有", "前10持仓")          # 完整分析里可以按试探仓放宽的拒绝原因
PROBE_DEFAULTS = {
    "enabled": True,
    "size_mult": 0.25,             # 仓位 = 正常仓位 × 0.25
    "max_top10_pct": 65,           # 完整分析：前10持仓上限（正常 45）
    "max_bundle_pct": 45,          # 完整分析：捆绑地址现在还拿着的比例上限（正常 25）
    "max_bundler_trade_pct": 60,   # 强叙事扫描：GMGN 列表"捆绑交易占成交量"上限（正常 30）
    "min_smart": 2,                # 跟单要 ≥ 2 个还拿着的聪明钱才开试探仓
    "narrative_min_score": 80,     # 强叙事要 ≥ 80 分才开试探仓
}


def probe_cfg(cfg) -> dict:
    return {**PROBE_DEFAULTS, **(cfg.get("trading.probe", {}) or {})}


def probe_note(why: str) -> str:
    return f"（{PROBE}：{why}，先小仓位建仓再观察）"
