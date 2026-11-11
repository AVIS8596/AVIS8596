"""捆绑地址 / 埋伏地址 / 狙击地址识别（纯计算，不调接口，方便测试）。

判定思路（每个钱包累计证据，证据越多越可信）：
  捆绑（dev 控制的小号）：
    - 和创建交易在同一个 slot / 区块买入（Solana 上通常是 Jito bundle 打包）
    - 同一笔交易里多个钱包一起买
    - 出资人是 dev 本人，或和 dev 同一个出资人
    - 和其他早期买家共用同一个出资人
    - Birdeye 标记为 bundler
  埋伏（提前知情、提前布局）：
    - 开盘前不久才首次入金的新钱包，并且在开盘最早几个 slot/区块买入
    - Birdeye 标记为 insider
  狙击：
    - 开盘最早几个 slot/区块买入，但没有上面的关联证据（多为公开的狙击机器人）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EarlyBuy:
    wallet: str
    slot: int               # Solana slot 或 EVM 区块号
    ts: float
    native: float           # 花费的原生币
    token_raw: int          # 买到的代币原始数量
    tx: str = ""
    multi_in_tx: bool = False   # 同一笔交易里有多个钱包买入
    tags: set[str] = field(default_factory=set)   # 外部标签（bundler / insider / sniper ...）


@dataclass
class Funding:
    funder: str
    ts: float


@dataclass
class WalletVerdict:
    wallet: str
    cls: str                 # bundle / insider / sniper / normal
    evidence: list[str]
    bought_raw: int
    held_raw: int = 0


@dataclass
class BundleResult:
    verdicts: dict[str, WalletVerdict]
    clusters: list[dict]
    bundle_bought_pct: float = 0.0
    bundle_held_pct: float = 0.0
    insider_bought_pct: float = 0.0
    insider_held_pct: float = 0.0
    sniper_held_pct: float = 0.0
    dev_held_pct: float = 0.0

    def wallets(self, cls: str) -> list[str]:
        return [w for w, v in self.verdicts.items() if v.cls == cls]


def _similar_amounts(buys: list[EarlyBuy], tol: float) -> set[str]:
    """找出买入金额彼此非常接近（相对差 ≤ tol）且至少 3 个一组的钱包。"""
    vals = [(b.native if b.native > 0 else float(b.token_raw), b.wallet) for b in buys]
    vals = [v for v in vals if v[0] > 0]
    vals.sort()
    out: set[str] = set()
    n = len(vals)
    j = 0
    for i in range(n):
        j = max(j, i)
        while j + 1 < n and (vals[j + 1][0] - vals[i][0]) / vals[j + 1][0] <= tol:
            j += 1
        if j - i + 1 >= 3:
            out.update(w for _, w in vals[i:j + 1])
    return out


def detect(*, creator: str, launch_slot: int, launch_ts: float, buys: list[EarlyBuy],
           funding: dict[str, Funding], supply_raw: int, holdings: dict[str, int],
           early_window: int = 3, fresh_hours: float = 72, similar_tol: float = 0.15,
           ignore: set[str] | None = None) -> BundleResult:
    ignore = ignore or set()
    # 同一钱包多次买入合并
    merged: dict[str, EarlyBuy] = {}
    for b in sorted(buys, key=lambda x: (x.slot, x.ts)):
        if not b.wallet or b.wallet in ignore:
            continue
        if b.wallet in merged:
            m = merged[b.wallet]
            m.native += b.native
            m.token_raw += b.token_raw
            m.multi_in_tx = m.multi_in_tx or b.multi_in_tx
            m.tags |= b.tags
        else:
            merged[b.wallet] = EarlyBuy(b.wallet, b.slot, b.ts, b.native, b.token_raw, b.tx, b.multi_in_tx, set(b.tags))
    early = [b for b in merged.values() if b.slot - launch_slot <= early_window]
    similar = _similar_amounts(early, similar_tol)

    creator_funder = funding.get(creator).funder if creator in funding else ""
    by_funder: dict[str, list[str]] = {}
    for w in merged:
        fd = funding.get(w)
        if fd and fd.funder:
            by_funder.setdefault(fd.funder, []).append(w)
    shared = {fd: ws for fd, ws in by_funder.items() if len(ws) >= 2}

    verdicts: dict[str, WalletVerdict] = {}
    for w, b in merged.items():
        if w == creator:
            verdicts[w] = WalletVerdict(w, "dev", ["代币创建者"], b.token_raw)
            continue
        ev: list[str] = []
        pts = 0
        is_early = b.slot - launch_slot <= early_window
        fd = funding.get(w)
        tags = {t.lower() for t in b.tags}

        if b.slot <= launch_slot:
            ev.append("与创建交易同一 slot/区块买入")
            pts += 3
        if b.multi_in_tx:
            ev.append("同一笔交易多个钱包一起买")
            pts += 3
        if fd and creator and fd.funder == creator:
            ev.append("出资人就是 dev")
            pts += 4
        elif fd and creator_funder and fd.funder == creator_funder:
            ev.append("和 dev 同一个出资人")
            pts += 4
        if fd and fd.funder in shared:
            ev.append(f"和另外 {len(shared[fd.funder]) - 1} 个早期钱包共用出资人")
            pts += 3
        if any("bundl" in t for t in tags):
            ev.append("Birdeye 标记 bundler")
            pts += 3
        if w in similar:
            ev.append("买入金额与其他早期钱包高度相近")
            pts += 1

        fresh = bool(fd and fd.ts and launch_ts and 0 <= launch_ts - fd.ts <= fresh_hours * 3600)
        fresh_after = bool(fd and fd.ts and launch_ts and fd.ts > launch_ts)
        if fresh:
            ev.append(f"开盘前 {max((launch_ts - fd.ts) / 3600, 0):.1f} 小时才首次入金的新钱包")
        elif fresh_after and is_early:
            ev.append("开盘后才入金就立刻买入")
            fresh = True

        if pts >= 3 and is_early or pts >= 4:
            cls = "bundle"
        elif any("insider" in t for t in tags) or (fresh and is_early):
            cls = "insider"
            if any("insider" in t for t in tags):
                ev.append("Birdeye 标记 insider")
        elif is_early or any("snip" in t for t in tags):
            cls = "sniper"
            if not ev:
                ev.append("开盘最早几个 slot/区块买入")
        else:
            cls = "normal"
        verdicts[w] = WalletVerdict(w, cls, ev, b.token_raw)

    for w, v in verdicts.items():
        v.held_raw = int(holdings.get(w, 0))

    def pct(cls: str, held: bool) -> float:
        if supply_raw <= 0:
            return 0.0
        s = sum((v.held_raw if held else v.bought_raw) for v in verdicts.values() if v.cls == cls)
        return round(100.0 * s / supply_raw, 2)

    clusters = [{"funder": fd, "wallets": ws, "dev_linked": fd in (creator, creator_funder) and bool(fd)}
                for fd, ws in sorted(shared.items(), key=lambda x: -len(x[1]))]
    dev_held = 100.0 * holdings.get(creator, 0) / supply_raw if supply_raw and creator else 0.0
    return BundleResult(
        verdicts=verdicts, clusters=clusters,
        bundle_bought_pct=pct("bundle", False), bundle_held_pct=pct("bundle", True),
        insider_bought_pct=pct("insider", False), insider_held_pct=pct("insider", True),
        sniper_held_pct=pct("sniper", True), dev_held_pct=round(dev_held, 2),
    )
