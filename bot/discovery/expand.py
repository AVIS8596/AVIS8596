"""扩大钱包池 + 按"跟它买到底赚不赚"给钱包打分。

1. 信号质量（每小时）：每个钱包最近 7 天买过的币，在它买入后 1 小时涨跌如何（用结果档案算）。
   这才是"对我们来说聪明不聪明"：GMGN 的胜率是它自己的，跟单的人晚几十秒进场，结果可能完全不同。
   - 跟它买的币普遍下跌 → 不单独跟；样本多且明显亏 → 移出监听
   - 跟它买的币普遍上涨 → 即使它是快进快出，也允许单独跟（照样有 30 秒确认期）
2. 大涨币挖矿（每 2 小时）：结果档案里涨了 3 倍以上的币 + GMGN 6h/24h 热门榜里市值 ≥ $50 万的币，
   拉盈利前 50 名，记下每个钱包"赚过哪些大涨币"、是不是早期发现者（发币 3 分钟~6 小时内、市值 ≤ $30 万买入）。
   同一个钱包 7 天内在 3 个以上大涨币里赚钱：
   - 带捆绑 / 老鼠仓 / dev / 狙击标签 → 🕵️ 阴谋地址（往往提前知道消息）
   - 带 KOL 标签 → 📣 KOL
   - 其他 → 🎯 精准钱包
3. KOL（每 6 小时）：GMGN KOL 交易流里的钱包，按 30 天已实现利润取前 30 名。
   KOL 评分给 50（低于跟单门槛 55）：不会因为 KOL 一个人买就跟（KOL 常在粉丝进场后出货），
   但会计入"多个钱包共振"和持仓健康分里的"新聪明钱进场"。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..chains import NO_FEED_CHAINS, norm_addr
from ..utils import f

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.expand")

KIND_NAME = {"smart": "🧠聪明钱", "kol": "📣KOL", "insider": "🕵️阴谋地址", "winner": "🎯精准钱包",
             "early": "🔭早期发现者"}
INSIDER_TAGS = {"bundler", "rat_trader", "dev", "sniper"}
KOL_TAGS = {"renowned", "kol"}


def quality_from(rows: list[dict]) -> dict[str, dict]:
    """rows: wallet, token, price(买入时), p60。同一个钱包同一个币只算第一次。"""
    seen: set[tuple[str, str]] = set()
    by: dict[str, list[float]] = {}
    for r in rows:
        k = (r["wallet"], r["token"])
        if k in seen or f(r["price"]) <= 0 or f(r["p60"]) <= 0:
            continue
        seen.add(k)
        by.setdefault(r["wallet"], []).append(f(r["p60"]) / f(r["price"]) - 1)
    out = {}
    for w, rs in by.items():
        rs.sort()
        out[w] = {"n": len(rs), "ret": rs[len(rs) // 2], "hit": sum(1 for x in rs if x >= 0.3) / len(rs),
                  "dump": sum(1 for x in rs if x <= -0.3) / len(rs)}
    return out


def is_early_finder(t: dict, created: float, supply: float, mcap_now: float) -> bool:
    """早期发现者：发币 3 分钟 ~ 6 小时内、市值 ≤ $30 万时买入，之后涨了 3 倍以上（不算开盘狙击）。"""
    start = f(t.get("start_holding_at"))
    if not created or not start or start < created:
        return False
    age_min = (start - created) / 60
    entry_mcap = f(t.get("avg_cost")) * supply if supply else 0
    return 3 <= age_min <= 360 and 0 < entry_mcap <= 300_000 and (not mcap_now or mcap_now >= entry_mcap * 3)


def classify_hits(hits: list[dict]) -> str:
    """一个钱包的多次大涨币命中 → 类别。"""
    if sum(1 for h in hits if h["insider"]) >= 2:
        return "insider"
    if any(h["kol"] for h in hits):
        return "kol"
    if sum(1 for h in hits if h.get("early")) >= 2:
        return "early"
    return "winner"


class WalletExpander:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg

    # ------------------------------------------------------------ 信号质量
    async def update_quality(self, chain: str = "solana") -> int:
        db = self.app.db
        rows = await db.fetchall(
            "SELECT s.wallet, s.token, o.price, o.p60 FROM signals s JOIN token_outcomes o "
            "ON o.chain=s.chain AND o.token=s.token AND o.ts BETWEEN s.ts - 60 AND s.ts + 600 "
            "WHERE s.chain=? AND s.side='buy' AND s.ts>? AND o.p60>0 ORDER BY s.ts",
            (chain, time.time() - 7 * 86400))
        q = quality_from([dict(r) for r in rows])
        removed = 0
        for w, st in q.items():
            await db.execute("UPDATE wallets SET sig_n=?, sig_ret=?, sig_hit=?, sig_dump=? WHERE chain=? AND address=?",
                             (st["n"], st["ret"], st["hit"], st["dump"], chain, w))
            # 样本够多、跟它买明显亏钱 → 移出监听（手动添加的不动）
            if st["n"] >= 15 and st["ret"] <= -0.2 and st["dump"] > st["hit"] * 2:
                cur = await db.wallet(chain, w)
                if cur and cur["status"] == "active":
                    await db.execute("UPDATE wallets SET status='inactive' WHERE chain=? AND address=?", (chain, w))
                    removed += 1
        if removed:
            self.app.wallets_changed[chain].set()
            log.info("信号质量差，移出 %d 个钱包", removed)
        return len(q)

    # ------------------------------------------------------------ 大涨币挖矿
    async def mine_winners(self, chain: str = "solana", per_run: int = 15) -> int:
        db = self.app.db
        gm = self.app.gmgn
        now = time.time()
        rows = await db.fetchall(
            "SELECT token, MAX(MAX(COALESCE(p60,0), COALESCE(p240,0)) / price) x FROM token_outcomes "
            "WHERE chain=? AND ts>? AND price>0 GROUP BY token HAVING x>=3 ORDER BY x DESC LIMIT 60",
            (chain, now - 2 * 86400))
        mined = set(await db.kv_get("mined_tokens", []) or [])
        cands = [r["token"] for r in rows]
        # 再加上 GMGN 6h / 24h 热门榜里市值 ≥ $50 万的币（结果档案只覆盖机器人分析过的币）
        for iv in ("6h", "24h"):
            try:
                for t in await gm.rank(chain, iv, 50):
                    if t.get("address") and f(t.get("market_cap") or t.get("usd_market_cap")) >= 500_000:
                        cands.append(norm_addr(chain, t["address"]))
            except Exception as e:  # noqa: BLE001
                log.debug("热门榜 %s: %s", iv, e)
        todo = list(dict.fromkeys(c for c in cands if c not in mined))[:per_run]
        for tok in todo:
            try:
                ov = await self.app.data.overview(chain, tok)
                created = f(ov.get("created_ts"))
                supply, mcap_now = f(ov.get("total_supply")), f(ov.get("mcap"))
                for t in await gm.top_traders(chain, tok, 50, order_by="profit"):
                    if int(f(t.get("addr_type"))) == 2 or f(t.get("profit")) <= 0 or not t.get("address"):
                        continue
                    tags = {str(x).lower() for x in (t.get("maker_token_tags") or []) + (t.get("tags") or [])}
                    await db.execute(
                        "INSERT OR REPLACE INTO wallet_hits(chain,wallet,token,ts,profit,early,insider,kol) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (chain, norm_addr(chain, t["address"]), tok, now, f(t.get("profit")),
                         int(is_early_finder(t, created, supply, mcap_now)),
                         int(bool(tags & INSIDER_TAGS)), int(bool(tags & KOL_TAGS))))
            except Exception as e:  # noqa: BLE001
                log.warning("挖矿 %s 失败: %s", tok, e)
            mined.add(tok)
            await asyncio.sleep(0.5)
        await db.kv_set("mined_tokens", list(mined)[-500:])
        # 7 天内在 3 个以上大涨币里赚钱的钱包
        hits = await db.fetchall("SELECT * FROM wallet_hits WHERE chain=? AND ts>?", (chain, now - 7 * 86400))
        by: dict[str, list[dict]] = {}
        for h in hits:
            by.setdefault(h["wallet"], []).append(dict(h))
        added = 0
        for w, hs in by.items():
            n_tok = len({h["token"] for h in hs})
            n_early = len({h["token"] for h in hs if h.get("early") and not h.get("insider")})
            # 3 个以上大涨币赚钱，或者 2 个以上大涨币都是早期发现的
            if n_tok < int(self.cfg.get("smart_money.mining_min_hits", 3)) and n_early < 2:
                continue
            kind = classify_hits(hs)
            score = min(80, 50 + 5 * len(hs)) if kind != "kol" else 50
            cur = await db.wallet(chain, w)
            if cur and cur["status"] in ("manual", "blacklist"):
                continue
            if cur and cur["status"] == "active":
                await db.execute("UPDATE wallets SET kind=?, score=MAX(score, ?) WHERE chain=? AND address=?",
                                 (kind if cur.get("kind") in (None, "smart") else cur["kind"], score, chain, w))
                continue
            await db.execute(
                "INSERT INTO wallets(chain,address,status,source,score,created_at,evaluated_at,kind) "
                "VALUES(?,?,'active',?,?,?,?,?) ON CONFLICT(chain,address) DO UPDATE SET status='active', "
                "kind=excluded.kind, score=excluded.score, source=excluded.source",
                (chain, w, f"mining:{len(hs)}", score, now, now, kind))
            added += 1
        if added:
            self.app.wallets_changed[chain].set()
            log.info("大涨币挖矿：新增 %d 个钱包", added)
        return added

    # ------------------------------------------------------------ KOL
    async def refresh_kols(self, chain: str = "solana", top: int = 30) -> int:
        db = self.app.db
        gm = self.app.gmgn
        makers = list({norm_addr(chain, t["maker"]) for t in await gm.kol(chain, 200) if t.get("maker")})
        ranked = []
        for i in range(0, len(makers), 100):
            for r in await gm.wallet_profits(chain, makers[i:i + 100], "30d"):
                if r.get("wallet_address") and f(r.get("realized_profit")) > 0:
                    ranked.append((f(r.get("realized_profit")), norm_addr(chain, r["wallet_address"])))
        ranked.sort(reverse=True)
        now, added = time.time(), 0
        for profit, w in ranked[:top]:
            cur = await db.wallet(chain, w)
            if cur and cur["status"] in ("manual", "blacklist", "active"):
                continue   # 已在监听（达标的聪明钱保持原类别）或被拉黑
            await db.execute(
                "INSERT INTO wallets(chain,address,status,source,score,realized_usd,created_at,evaluated_at,kind) "
                "VALUES(?,?,'active','kol',50,?,?,?,'kol') ON CONFLICT(chain,address) DO UPDATE SET status='active', "
                "kind='kol', score=50, realized_usd=excluded.realized_usd",
                (chain, w, profit, now, now))
            added += 1
        if added:
            self.app.wallets_changed[chain].set()
        return added

    async def run(self) -> None:
        await asyncio.sleep(120)
        last = {"q": 0.0, "m": 0.0, "k": 0.0}
        while True:
            now = time.time()
            for name, every, fn in (("q", 3600, self.update_quality), ("m", 7200, self.mine_winners),
                                    ("k", 6 * 3600, self.refresh_kols)):
                if now - last[name] >= every:
                    last[name] = now
                    # Solana + 没有 GMGN 聪明钱交易流的链（罗宾汉链）：钱包只能靠挖大涨币
                    chains = ["solana"] + ([] if name == "k" else
                                           [c for c in self.app.cfg.enabled_chains if c in NO_FEED_CHAINS])
                    for c in chains:
                        if c != "solana" or "solana" in self.app.cfg.enabled_chains:
                            try:
                                await fn(c)
                            except Exception as e:  # noqa: BLE001
                                log.warning("钱包扩充任务 %s（%s）失败: %s", name, c, e)
            await asyncio.sleep(300)
