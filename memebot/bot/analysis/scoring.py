"""代币安全检查与潜力评分（纯计算）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..utils import f


@dataclass
class TokenFacts:
    chain: str
    address: str
    symbol: str = ""
    name: str = ""
    price: float = 0.0
    liquidity: float = 0.0
    mcap: float = 0.0
    holders: int = 0
    volume_1h: float = 0.0
    buys_1h: int = 0
    sells_1h: int = 0
    change_1h: float = 0.0
    age_hours: float = 0.0
    top10_pct: float = 0.0
    dev_held_pct: float = 0.0
    bundle_held_pct: float = 0.0
    insider_held_pct: float = 0.0
    sniper_held_pct: float = 0.0
    smart_buyers: int = 0
    buy_tax: float = 0.0          # %
    sell_tax: float = 0.0         # %
    risks: list[str] = field(default_factory=list)     # 致命风险（一票否决）
    warnings: list[str] = field(default_factory=list)  # 一般风险


def solana_safety(mint_info: dict, birdeye_sec: dict) -> tuple[list[str], list[str]]:
    risks, warns = [], []
    if mint_info.get("mintAuthority"):
        risks.append("铸币权限未放弃（可无限增发）")
    if mint_info.get("freezeAuthority"):
        risks.append("冻结权限未放弃（可冻结你的代币）")
    fee_bps = int(f(mint_info.get("transferFeeBps")))
    if fee_bps > 0:
        risks.append(f"带转账税 {fee_bps / 100:.2f}%（Token-2022）")
    elif birdeye_sec.get("transferFeeEnable"):
        warns.append("开启了转账税扩展")
    if mint_info.get("nonTransferable") or birdeye_sec.get("nonTransferable"):
        risks.append("代币不可转让")
    if birdeye_sec.get("mutableMetadata"):
        warns.append("元数据可修改")
    if mint_info.get("isToken2022"):
        warns.append("Token-2022 代币")
    return risks, warns


def evm_safety(gp: dict, max_sell_tax: float) -> tuple[list[str], list[str], float, float]:
    risks, warns = [], []

    def yes(k: str) -> bool:
        return str(gp.get(k, "0")) == "1"

    if not gp:
        warns.append("GoPlus 无数据（太新或接口失败），安全性未知")
        return risks, warns, 0.0, 0.0
    buy_tax = f(gp.get("buy_tax")) * 100
    sell_tax = f(gp.get("sell_tax")) * 100
    if yes("is_honeypot"):
        risks.append("貔貅盘（买了卖不出）")
    if yes("cannot_sell_all"):
        risks.append("不能全部卖出")
    if yes("owner_change_balance"):
        risks.append("项目方可修改余额")
    if yes("hidden_owner"):
        risks.append("隐藏 owner")
    if yes("selfdestruct"):
        risks.append("合约可自毁")
    if sell_tax > max_sell_tax:
        risks.append(f"卖出税 {sell_tax:.1f}% 过高")
    if yes("slippage_modifiable"):
        warns.append("税率可被修改")
    if yes("transfer_pausable"):
        warns.append("可暂停交易")
    if yes("is_blacklisted"):
        warns.append("有黑名单功能")
    if yes("is_mintable"):
        warns.append("可增发")
    if yes("is_proxy"):
        warns.append("代理合约（可升级）")
    if yes("can_take_back_ownership"):
        risks.append("可收回 owner 权限")
    return risks, warns, buy_tax, sell_tax


def score(t: TokenFacts) -> tuple[int, list[str]]:
    """0~100 分。有致命风险直接 0 分。"""
    if t.risks:
        return 0, ["致命风险：" + "；".join(t.risks)]
    s = 50.0
    why: list[str] = []

    # 聪明钱（最重要的信号）
    if t.smart_buyers:
        add = min(t.smart_buyers * 8, 30)
        s += add
        why.append(f"+{add:.0f} {t.smart_buyers} 个聪明钱买入")

    # 流动性
    if t.liquidity >= 50_000:
        s += 8; why.append("+8 流动性充足")
    elif t.liquidity >= 15_000:
        s += 4; why.append("+4 流动性一般")
    elif t.liquidity < 5_000:
        s -= 10; why.append("-10 流动性太低")

    # 交易活跃度（买多卖少）
    tot = t.buys_1h + t.sells_1h
    if tot >= 100:
        ratio = t.buys_1h / tot
        if ratio >= 0.6:
            s += 6; why.append("+6 买盘强")
        elif ratio <= 0.4:
            s -= 6; why.append("-6 卖盘强")
    if t.holders >= 500:
        s += 5; why.append("+5 持有人多")
    elif 0 < t.holders < 50:
        s -= 5; why.append("-5 持有人太少")

    # 筹码结构
    if t.bundle_held_pct >= 30:
        s -= 30; why.append(f"-30 捆绑地址仍持有 {t.bundle_held_pct:.1f}%")
    elif t.bundle_held_pct >= 15:
        s -= 15; why.append(f"-15 捆绑地址仍持有 {t.bundle_held_pct:.1f}%")
    elif t.bundle_held_pct >= 5:
        s -= 5; why.append(f"-5 捆绑地址仍持有 {t.bundle_held_pct:.1f}%")
    if t.insider_held_pct >= 20:
        s -= 15; why.append(f"-15 埋伏地址持有 {t.insider_held_pct:.1f}%")
    elif t.insider_held_pct >= 10:
        s -= 8; why.append(f"-8 埋伏地址持有 {t.insider_held_pct:.1f}%")
    if t.dev_held_pct >= 10:
        s -= 10; why.append(f"-10 dev 持有 {t.dev_held_pct:.1f}%")
    if t.top10_pct >= 50:
        s -= 15; why.append(f"-15 前10持仓 {t.top10_pct:.1f}%")
    elif t.top10_pct >= 35:
        s -= 6; why.append(f"-6 前10持仓 {t.top10_pct:.1f}%")

    # 涨幅过大追高风险
    if t.change_1h >= 500:
        s -= 10; why.append("-10 1小时涨幅过大")
    if t.warnings:
        s -= 3 * len(t.warnings)
        why.append(f"-{3 * len(t.warnings)} 一般风险：" + "；".join(t.warnings))
    return max(0, min(100, int(round(s)))), why
