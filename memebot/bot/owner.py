"""把你自己手动做的实盘交易当参考（默认看绑定在 GMGN API 上的 Solana 钱包和 EVM 钱包在罗宾汉链上的交易）。

三个用途：
1. 真实成本：你手动成交的价格 vs 那一分钟的 K 线中间价 → 补充实盘成本样本（尤其是卖出，机器人自己的样本少）。
2. 你的画像：入场时机、入场市值、持有多久、怎么止盈止损、胜率和每笔期望，和机器人对比。
3. 你买过、机器人没买的币：机器人当时看没看到、卡在哪条规则——你的判断比机器人强的地方，就是它该学的地方。
机器人自己下的单按交易哈希（或同币种 90 秒内同方向）排除，只看你手动的。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING

from .learn import build_positions, profile_from, profile_text
from .utils import dig, f, fmt_usd

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.owner")


async def owner_wallets(app: "App") -> list[tuple[str, str]]:
    saved = await app.db.kv_get("owner_wallets", None)
    if saved:
        return [(c, a) for c, a in saved]
    s = app.cfg.secrets
    out = []
    if getattr(s, "gmgn_wallet_sol", ""):
        out.append(("solana", s.gmgn_wallet_sol))
    if getattr(s, "gmgn_wallet_evm", ""):
        out.append(("robinhood", s.gmgn_wallet_evm.lower()))
    return out


async def fetch(app: "App", chain: str, addr: str, pages: int = 4) -> tuple[list[dict], list[dict]]:
    gm = app.gmgn
    acts: list[dict] = []
    cursor = None
    for _ in range(pages):
        d = await gm.wallet_activity(chain, addr, 50, cursor)
        rows = (d.get("activities") or []) if isinstance(d, dict) else []
        acts += rows
        cursor = d.get("next") if isinstance(d, dict) else None
        if not cursor or len(rows) < 50:
            break
    holds: list[dict] = []
    cursor = None
    for _ in range(3):
        try:
            d = await gm.wallet_holdings(chain, addr, 50, cursor)
        except Exception as e:  # noqa: BLE001
            log.debug("持仓查询失败 %s: %s", addr, e)
            break
        rows = (d.get("list") or []) if isinstance(d, dict) else []
        holds += rows
        cursor = d.get("next") if isinstance(d, dict) else None
        if not cursor or len(rows) < 50:
            break
    return acts, holds


def _tx(a: dict) -> str:
    return str(a.get("tx_hash") or a.get("hash") or a.get("transaction_hash") or "").lower()


def _side(a: dict) -> str:
    return str(a.get("event_type") or a.get("type") or "").lower()


def manual_only(acts: list[dict], bot_rows: list[dict]) -> list[dict]:
    """去掉机器人自己下的单：交易哈希对得上，或者同一个币 90 秒内同方向。"""
    txs = {str(r["tx"] or "").lower() for r in bot_rows if r["tx"]}
    out = []
    for a in acts:
        if _side(a) not in ("buy", "sell"):
            continue
        if _tx(a) and _tx(a) in txs:
            continue
        tok = str(dig(a, "token.address", "token.token_address", default="")).lower()
        ts = f(a.get("timestamp"))
        if any(str(r["token"]).lower() == tok and r["side"] == _side(a) and abs(f(r["ts"]) - ts) < 90 for r in bot_rows):
            continue
        out.append(a)
    return out


async def record_costs(app: "App", chain: str, acts: list[dict], limit: int = 25) -> int:
    """你手动成交的价格 vs 那一分钟 K 线中间价，存进 manual_costs（实盘成本样本）。"""
    from .trade.costs import ref_price
    db = app.db
    done = {r["tx"] for r in await db.fetchall("SELECT tx FROM manual_costs")}
    n = 0
    for a in sorted(acts, key=lambda x: -f(x.get("timestamp"))):
        tx = _tx(a) or f"{dig(a, 'token.address', default='')}:{a.get('timestamp')}"
        if tx in done or n >= limit or time.time() - f(a.get("timestamp")) > 30 * 86400:
            continue
        n += 1
        fill = f(a.get("price_usd"))
        tok = str(dig(a, "token.address", "token.token_address", default=""))
        ref = await ref_price(app, chain, tok, f(a.get("timestamp"))) if fill > 0 and tok else 0
        await asyncio.sleep(0.3)        # 后台任务，慢慢查，别和交易抢 GMGN 额度
        cost = None
        if fill > 0 and ref > 0:
            cost = (fill / ref - 1) if _side(a) == "buy" else (1 - fill / ref)
            cost = max(-0.5, min(0.9, cost))
        await db.execute("INSERT OR REPLACE INTO manual_costs(tx,ts,chain,side,cost,usd) VALUES(?,?,?,?,?,?)",
                         (tx, f(a.get("timestamp")), chain, _side(a), cost, f(a.get("cost_usd"))))
    return n


async def coverage(app: "App", chain: str, positions: list[dict], first_buy: dict[str, float]) -> tuple[Counter, list[str]]:
    """你买过的币，机器人当时看没看到、买没买、没买的卡在哪。
    "看到"分三层：完整分析过（结果档案）> 在快毕业列表里出现过（早期快照）> 监听的钱包交易过（信号）。"""
    from .brain import block_reason
    db = app.db
    since = await db.fetchone("SELECT MIN(ts) t FROM token_outcomes WHERE chain=?", (chain,))
    t_start = f(since["t"]) if since and since["t"] else time.time()
    why: Counter = Counter()
    eg: list[str] = []
    for p in positions:
        tok = p["token"]
        tb = first_buy.get(tok, 0)
        if tb and tb < t_start:
            why["那时机器人还没在这条链上跑"] += 1
            continue
        bought = await db.fetchone("SELECT 1 FROM positions WHERE chain=? AND token=?", (chain, tok))
        if bought:
            why["机器人也买了"] += 1
            continue
        o = await db.fetchone("SELECT ts, features FROM token_outcomes WHERE chain=? AND token=? ORDER BY ts LIMIT 1",
                              (chain, tok))
        if o:
            r = await block_reason(app, chain, tok, f(o["ts"]), json.loads(o["features"] or "{}"))
            why["分析过但" + r] += 1
        elif await db.fetchone("SELECT 1 FROM early_obs WHERE chain=? AND token=? LIMIT 1", (chain, tok)):
            r = "在快毕业列表里见过，但没触发早期建仓、也没做完整分析"
            why[r] += 1
        elif await db.fetchone("SELECT 1 FROM signals WHERE chain=? AND token=? LIMIT 1", (chain, tok)):
            r = "监听的钱包交易过，但没触发跟单"
            why[r] += 1
        else:
            r = "完全没出现在机器人的数据源里"
            why[r] += 1
        if p.get("pnl") is not None and p["pnl"] > 0.5 and len(eg) < 4:
            eg.append(f"{p['symbol']} 你赚 {p['pnl'] * 100:+.0f}%（机器人：{r}）")
    return why, eg


async def report(app: "App", send_costs: bool = True) -> str:
    db = app.db
    wallets = await owner_wallets(app)
    if not wallets:
        return "没有找到你的钱包：.env 里没填 GMGN_WALLET_SOL / GMGN_WALLET_EVM。也可以 /me add solana 地址"
    lines = ["<b>👤 你自己的实盘（手动交易）</b>——机器人用来参考成本、对照画像、找自己的盲区"]
    for chain, addr in wallets:
        try:
            acts, holds = await fetch(app, chain, addr)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{chain}：取不到交易记录（{html.escape(str(e)[:80])}）")
            continue
        bot_rows = [dict(r) for r in await db.fetchall(
            "SELECT tx, token, side, ts FROM trades WHERE chain=? AND dry_run=0", (chain,))]
        man = manual_only(acts, bot_rows)
        n_cost = await record_costs(app, chain, man)
        pos = build_positions(man, holds)
        if not pos:
            lines.append(f"\n<b>{chain}</b> {addr[:6]}…：最近没有你手动的交易（{len(acts)} 条记录都是机器人下的或不是买卖）")
            continue
        prof = profile_from(pos)
        lines.append("\n" + profile_text(f"你在 {chain}", addr, prof))
        done = [p for p in pos if p["closed"] and p["pnl"] is not None]
        if done:
            exp = sum(p["pnl"] for p in done) / len(done)
            usd = sum(p["pnl"] * p["cost"] for p in done)
            lines.append(f"已清仓 {len(done)} 个币：每笔期望 {exp * 100:+.1f}%，合计 {fmt_usd(usd)}")
        first_buy: dict[str, float] = {}
        for a in man:
            if _side(a) == "buy":
                tk = str(dig(a, "token.address", "token.token_address", default=""))
                ts = f(a.get("timestamp"))
                first_buy[tk] = min(first_buy.get(tk, ts), ts)
        why, eg = await coverage(app, chain, pos, first_buy)
        lines.append("你买的币机器人怎么样：" + html.escape("；".join(f"{k} ×{v}" for k, v in why.most_common(5))))
        if eg:
            lines.append("你赚了、机器人没买的：" + html.escape("、".join(eg)))
        if n_cost:
            lines.append(f"（新增 {n_cost} 笔你的成交作为实盘成本样本）")
        await db.execute(
            "INSERT INTO learn_wallets(chain,address,label,source,added_at,profiled_at,profile) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chain,address) DO UPDATE SET label=excluded.label, source=excluded.source, "
            "profiled_at=excluded.profiled_at, profile=excluded.profile",
            (chain, addr, "我自己", "owner", time.time(), time.time(), json.dumps(prof)))
    # 机器人自己的对照
    for dry, name in ((1, "模拟盘"), (0, "实盘")):
        r = await db.fetchall("SELECT cost_usd, realized_usd FROM positions WHERE status='closed' AND dry_run=? "
                              "AND closed_at>? AND cost_usd>0", (dry, time.time() - 7 * 86400))
        if r:
            e = sum(f(x["realized_usd"]) / f(x["cost_usd"]) - 1 for x in r) / len(r)
            lines.append(f"对照：机器人{name}最近 7 天 {len(r)} 笔，每笔期望 {e * 100:+.1f}%")
    if send_costs:
        from .trade.costs import calibrate
        app.live_costs = await calibrate(app)
    return "\n".join(lines)
