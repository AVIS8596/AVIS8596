"""撤池子 / 连环收割的记忆：同一伙人换个地址、用同一个名字反复发币、拉一下就撤池子。

记什么：持仓因为"流动性被撤走 / dev 清仓"紧急卖出，或者开仓 6 小时内亏掉 55% 以上（价格直接砸穿止损）时，
把这个币的 名字、dev 地址、带单钱包 记下来。

拦什么（买入前检查）：
- 同名币：72 小时内同一条链上有同名（不分大小写、去掉符号）的币撤过池子 / 被砸穿 → 不买
- dev：这个 dev 之前的币撤过池子 → 不买；7 天内用同一个 dev 地址发了 3 个以上的币（连环发币）→ 不买
- 带单钱包：7 天内 2 次把我们带进撤池子的项目 → 移出监听（status=blacklist）
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from ..utils import f

if TYPE_CHECKING:
    from ..app import App


def norm_symbol(s: str | None) -> str:
    return re.sub(r"[^0-9a-z一-鿿]", "", str(s or "").lower())


def is_rug(p: dict, reason: str, realized: float) -> str:
    """返回撤池子 / 砸穿的说明；不是就返回空。"""
    cost = f(p.get("cost_usd"))
    if "流动性被撤走" in reason or "dev 清仓" in reason:
        return reason.replace("🚨紧急卖出：", "")
    held = time.time() - f(p.get("opened_at"))
    if cost > 0 and realized / cost <= 0.45 and held < 6 * 3600:
        return f"开仓 {held / 3600:.1f} 小时内亏了 {(1 - realized / cost) * 100:.0f}%，价格砸穿止损"
    return ""


async def mark(app: "App", p: dict, why: str) -> None:
    db = app.db
    now = time.time()
    chain, token = p["chain"], p["token"]
    tok = await db.fetchone("SELECT symbol, creator FROM tokens WHERE chain=? AND address=?", (chain, token))
    sym = norm_symbol((tok["symbol"] if tok else None) or p.get("symbol"))
    rows = []
    if sym:
        rows.append((now, chain, "symbol", sym, token, why))
    if tok and tok["creator"]:
        rows.append((now, chain, "creator", str(tok["creator"]), token, why))
    import json
    for w in json.loads(p.get("source_wallets") or "[]"):
        rows.append((now, chain, "wallet", w, token, why))
    await db.executemany("INSERT INTO rug_marks(ts,chain,kind,key,token,reason) VALUES(?,?,?,?,?,?)", rows)
    # 2 次把我们带进撤池子项目的钱包：移出监听
    for w in json.loads(p.get("source_wallets") or "[]"):
        n = await db.fetchone("SELECT COUNT(DISTINCT token) n FROM rug_marks WHERE chain=? AND kind='wallet' AND key=? "
                              "AND ts>?", (chain, w, now - 7 * 86400))
        if n and n["n"] >= 2:
            await db.execute("UPDATE wallets SET status='blacklist' WHERE chain=? AND address=? AND status='active'",
                             (chain, w))
            if getattr(app, "tg", None):
                await app.tg.send(f"🚫 带单钱包 {w[:6]}… 7 天内 {n['n']} 次带进撤池子 / 被砸穿的项目，已移出监听")


async def check(app: "App", chain: str, token: str, rep: dict) -> str:
    """买入前检查，返回拦截理由（空 = 通过）。"""
    db = app.db
    now = time.time()
    sym = norm_symbol(rep.get("symbol"))
    if sym:
        r = await db.fetchone("SELECT token, reason FROM rug_marks WHERE chain=? AND kind='symbol' AND key=? AND ts>? "
                              "AND token<>? ORDER BY ts DESC LIMIT 1", (chain, sym, now - 72 * 3600, token))
        if r:
            return f"同名币 {rep.get('symbol')} 72 小时内撤过池子 / 被砸穿（{r['reason'][:30]}），项目方连环收割，不碰"
    creator = str(rep.get("creator") or "")
    if creator:
        r = await db.fetchone("SELECT reason FROM rug_marks WHERE chain=? AND kind='creator' AND key=? AND token<>? "
                              "ORDER BY ts DESC LIMIT 1", (chain, creator, token))
        if r:
            return f"同名 dev 之前的币撤过池子 / 被砸穿（{r['reason'][:30]}），不碰"
        n = await db.fetchone("SELECT COUNT(DISTINCT token) n FROM address_tags WHERE chain=? AND address=? AND tag='dev' "
                              "AND created_at>?", (chain, creator, now - 7 * 86400))
        lim = int(app.cfg.get("trading.max_dev_launches_7d", 3))
        if lim and n and n["n"] >= lim:
            return f"同名 dev 7 天内发了 {n['n']} 个币（连环发币），不碰"
    return ""
