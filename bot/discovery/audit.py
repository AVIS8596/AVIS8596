"""钱包体检：跟单之前先查这个"聪明钱"是不是项目方的号、老鼠仓或狙击机器人。

GMGN 的聪明钱标签只看胜率和利润，割人的号胜率往往最高。体检看它的真实交易：
1. 割完即死：它赚钱的币（至少 6 个取得到现价），80% 以上在它卖出之后跌掉 90% 以上，而且中位只拿 1 小时以内
   → 它赚的是撤池子 / 砸盘的钱（项目方 / 老鼠仓）；取不到现价的币不算（不等于归零）
2. 同名多合约：买了 3 个以上同名的不同合约，或 2 个且这个名字让我们亏光过 → 跟着同一伙人反复发币
3. 被标记过：机器人分析过的币里，3 次以上被认定为捆绑 / 埋伏 / dev
4. 跟项目方走得近：买了同一个项目方发的 2 个以上的币，而且这个项目方让我们亏光过、或发了 3 个以上
5. 开盘狙击：大多数币在发币 30 秒内就买 → 跟单时早已晚了（狙击机器人或内部号）
有 1~4 任意一条 → 标为"可疑"：不再监听、不计入聪明钱人数、不能当买入理由；7 天后自动复查。
另外每次跟单时还会看：带单钱包是不是就是这个币的项目方 / 捆绑 / 老鼠仓 / 开盘狙击（这次的筹码分析），是就不跟。
只有第 4 条 → 只记录，不跟它单独买（已有快手钱包规则会处理）。
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING

from ..learn import build_positions
from ..utils import dig, f

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.audit")

FRESH = 24 * 3600        # 体检结果有效期
SUSPECT_RECHECK = 7 * 86400


def sell_prices(acts: list[dict]) -> dict[str, float]:
    """每个币的卖出均价（按金额加权）。"""
    usd: dict[str, float] = {}
    amt: dict[str, float] = {}
    for a in acts:
        if str(a.get("event_type") or a.get("type") or "").lower() != "sell":
            continue
        tok = str(dig(a, "token.address", "token.token_address", default=""))
        px, c = f(a.get("price_usd")), f(a.get("cost_usd"))
        if tok and px > 0 and c > 0:
            usd[tok] = usd.get(tok, 0) + c
            amt[tok] = amt.get(tok, 0) + c / px
    return {t: usd[t] / amt[t] for t in usd if amt.get(t)}


def judge(pos: list[dict], sells: dict[str, float], now_px: dict[str, float], tagged: int,
          creator_hit: str = "", bad_symbols: set[str] | None = None) -> tuple[str, list[str]]:
    """返回 (结论 ok / suspect / sniper, 说明)。"""
    notes: list[str] = []
    bad = False
    if creator_hit:
        bad = True
        notes.append(creator_hit)
    wins = [p for p in pos if p.get("pnl") is not None and p["pnl"] > 0.3 and p["token"] in sells]
    # 只看取到了现价的币：取不到价格 ≠ 归零（老币、冷门链经常取不到），不能算进"死了"
    priced = [p for p in wins if now_px.get(p["token"], 0) > 0]
    dead = [p for p in priced if now_px[p["token"]] <= sells[p["token"]] * 0.1]
    holds = sorted(float(p["hold_min"]) for p in dead if p.get("hold_min") is not None)
    quick = bool(holds) and holds[len(holds) // 2] <= 60
    # 好币大多最终也会归零，所以要"几乎全部"在它卖出后死掉（≥ 80%），而且是快进快出（中位持有 ≤ 1 小时）才算
    if len(priced) >= 6 and len(dead) >= 0.8 * len(priced) and quick:
        bad = True
        notes.append(f"割完即死：它赚钱的 {len(priced)} 个币里 {len(dead)} 个在它卖出后跌了 90% 以上，"
                     f"中位只拿 {holds[len(holds) // 2]:.0f} 分钟")
    syms = Counter(str(p.get("symbol") or "").strip().upper().lstrip("$") for p in pos if p.get("symbol"))
    bad_symbols = bad_symbols or set()
    # 同名跟风盘很常见（好几个 PEPE / TRUMP），要 3 个以上同名合约，或 2 个且这个名字让我们亏光过
    dup = [(s, n) for s, n in syms.items() if s and s != "?" and (n >= 3 or (n >= 2 and s in bad_symbols))]
    if dup:
        bad = True
        notes.append("同名多合约：" + "、".join(f"{s}×{n}" for s, n in dup[:3]))
    if tagged >= 3:
        bad = True
        notes.append(f"被标记过：机器人分析的币里 {tagged} 次被认定为捆绑 / 埋伏 / dev")
    ages = [p["entry_age_min"] for p in pos if p.get("entry_age_min") is not None]
    snipe = len(ages) >= 5 and sum(1 for a in ages if a < 0.5) / len(ages) >= 0.6
    if snipe:
        notes.append(f"开盘狙击：{len(ages)} 个币里 {sum(1 for a in ages if a < 0.5)} 个在发币 30 秒内买入")
    if bad:
        return "suspect", notes
    if snipe:
        return "sniper", notes
    return "ok", notes or [f"正常（看了 {len(pos)} 个币）"]


async def audit(app: "App", chain: str, wallet: str, force: bool = False) -> dict:
    db = app.db
    row = await db.fetchone("SELECT status, audit_at, audit_verdict, audit_notes FROM wallets WHERE chain=? AND address=?",
                            (chain, wallet))
    if row and not force and row["audit_at"] and time.time() - float(row["audit_at"]) < FRESH:
        return {"verdict": row["audit_verdict"], "notes": json.loads(row["audit_notes"] or "[]"), "cached": True}
    from ..owner import fetch
    acts, holds = await fetch(app, chain, wallet, pages=2)
    pos = build_positions(acts, holds)
    sells = sell_prices(acts)
    toks = [p["token"] for p in pos if p.get("pnl") is not None and p["pnl"] > 0.3 and p["token"] in sells][:30]
    now_px = await app.data.prices(chain, toks) if toks else {}     # 取不到价格的不算（不等于归零）
    tg = await db.fetchone("SELECT COUNT(DISTINCT token) n FROM address_tags WHERE chain=? AND address=? "
                           "AND tag IN ('bundle','insider','dev')", (chain, wallet))
    bad_syms = {r["key"] for r in await db.fetchall("SELECT key FROM bad_actors WHERE kind='symbol'")}
    verdict, notes = judge(pos, sells, now_px, int(tg["n"]) if tg else 0,
                           await creator_affinity(app, chain, [p["token"] for p in pos]), bad_syms)
    if len(pos) < 3:
        verdict, notes = "ok", [f"交易太少（{len(pos)} 个币），暂时看不出问题"]
    await db.execute("UPDATE wallets SET audit_at=?, audit_verdict=?, audit_notes=? WHERE chain=? AND address=?",
                     (time.time(), verdict, json.dumps(notes, ensure_ascii=False), chain, wallet))
    if row:
        if verdict == "suspect" and row["status"] == "active":
            await db.execute("UPDATE wallets SET status='suspect' WHERE chain=? AND address=?", (chain, wallet))
            log.warning("钱包体检不过，标为可疑：%s %s %s", chain, wallet, notes)
        elif verdict != "suspect" and row["status"] == "suspect":
            await db.execute("UPDATE wallets SET status='active' WHERE chain=? AND address=?", (chain, wallet))
    return {"verdict": verdict, "notes": notes, "cached": False, "n": len(pos)}


async def creator_affinity(app: "App", chain: str, tokens: list[str]) -> str:
    """它买过的币里，同一个项目方发的有几个（只看机器人分析过、知道项目方的币）。"""
    if not tokens:
        return ""
    db = app.db
    marks = ",".join("?" * len(tokens))
    rows = await db.fetchall(f"SELECT creator, COUNT(*) n FROM tokens WHERE chain=? AND address IN ({marks}) "
                             f"AND creator IS NOT NULL AND creator<>'' GROUP BY creator HAVING n>=2",
                             (chain, *tokens))
    for r in rows:
        black = await db.fetchone("SELECT 1 FROM bad_actors WHERE chain=? AND kind='creator' AND key=?", (chain, r["creator"]))
        if black or r["n"] >= 3:
            return (f"跟项目方走得近：买了同一个项目方发的 {r['n']} 个币"
                    + ("（这个项目方让我们亏光过）" if black else ""))
    return ""


def token_roles(rep: dict | None) -> dict[str, str]:
    """这个币里的可疑身份（来自这次的筹码分析）：地址 → 项目方 / 捆绑 / 老鼠仓 / 开盘狙击。"""
    out: dict[str, str] = {}
    if not rep:
        return out
    for cls, name in (("bundle", "捆绑"), ("insider", "老鼠仓"), ("sniper", "开盘狙击")):
        for w in rep.get(f"{cls}_wallets") or []:
            a = w.get("wallet") if isinstance(w, dict) else w
            if a:
                out[str(a)] = name
    if rep.get("creator"):
        out[str(rep["creator"])] = "项目方"
    return out


def text(chain: str, wallet: str, r: dict) -> str:
    name = {"ok": "✅ 体检通过", "suspect": "🚫 可疑（不跟、不计入聪明钱）", "sniper": "⚠️ 狙击型（不单独跟）"}
    return (f"<b>钱包体检</b> {chain} {wallet[:6]}…{wallet[-4:]}：{name.get(r['verdict'], r['verdict'])}"
            + ("（24 小时内的结果）" if r.get("cached") else "") + "\n" + "\n".join("  " + n for n in r["notes"]))


class Auditor:
    """后台慢慢体检：新钱包优先，其次 24 小时没体检过的；可疑钱包 7 天后复查。"""

    def __init__(self, app: "App"):
        self.app = app

    async def recheck_v1(self) -> int:
        """一次性：第一版体检太严（取不到价格当归零、同名 2 个就算），把它标的可疑钱包恢复，按新规则重新体检。
        因为"带机器人买进亏光的币"被标的不恢复。"""
        db = self.app.db
        if await db.kv_get("audit_recheck_v1", False):
            return 0
        rows = await db.fetchall("SELECT chain, address, audit_notes FROM wallets WHERE status='suspect'")
        n = 0
        for r in rows:
            if "亏光" in str(r["audit_notes"] or ""):
                continue
            await db.execute("UPDATE wallets SET status='active', audit_at=NULL, audit_verdict=NULL WHERE chain=? "
                             "AND address=?", (r["chain"], r["address"]))
            n += 1
        await db.kv_set("audit_recheck_v1", True)
        if n:
            log.warning("钱包体检规则放宽：%d 个可疑钱包恢复，按新规则重新体检", n)
            await self.app.tg.send(f"🩺 钱包体检第一版太严，误伤了不少（比如取不到价格的币被当成归零）。"
                                   f"已恢复 {n} 个钱包，按新规则重新体检：跟单前会先体检，后台每 10 分钟审 20 个", low=True)
        return n

    async def run(self) -> None:
        import asyncio
        try:
            await self.recheck_v1()
        except Exception as e:  # noqa: BLE001
            log.warning("体检恢复失败: %s", e)
        await asyncio.sleep(600)
        while True:
            try:
                db = self.app.db
                now = time.time()
                rows = await db.fetchall(
                    "SELECT chain, address FROM wallets WHERE "
                    "((status='active' AND COALESCE(kind,'smart') IN ('smart','early','winner') "
                    "  AND (audit_at IS NULL OR audit_at<?)) OR (status='suspect' AND audit_at<?)) "
                    "ORDER BY audit_at IS NOT NULL, audit_at LIMIT ?",
                    (now - FRESH, now - SUSPECT_RECHECK, int(self.app.cfg.get("trading.audit_batch", 20))))
                flagged = []
                for r in rows:
                    if r["chain"] not in self.app.cfg.enabled_chains:
                        continue
                    try:
                        res = await audit(self.app, r["chain"], r["address"], force=True)
                        if res["verdict"] == "suspect":
                            flagged.append((r["chain"], r["address"], res["notes"][0]))
                    except Exception as e:  # noqa: BLE001
                        log.debug("体检失败 %s: %s", r["address"], e)
                    await asyncio.sleep(3)          # 慢慢来，别和交易抢 GMGN 额度
                if flagged:
                    await self.app.tg.send("🩺 钱包体检：" + str(len(flagged)) + " 个聪明钱被标为可疑，已停止跟单\n" + "\n".join(
                        f"  {c} {a[:6]}…{a[-4:]}：{n}" for c, a, n in flagged[:5]), low=True)
            except Exception as e:  # noqa: BLE001
                log.exception("钱包体检任务出错: %s", e)
            await asyncio.sleep(600)
