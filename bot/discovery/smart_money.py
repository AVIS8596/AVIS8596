"""聪明钱发现与复查。

来源（数据源为 GMGN 时）：
  1. GMGN 聪明钱 / KOL 实时交易流里出现的钱包
  2. GMGN 热门币（1小时榜）里盈利最多的交易者
  3. 手动添加（config / TG 命令）
每个候选地址用钱包统计接口核实"近30天已实现利润 + 胜率 + 交易次数"，达标才入库监听。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING

from ..analysis.analyzer import on_curve
from ..chains import norm_addr
from ..sources.birdeye import BudgetExceeded
from ..db import RANK_SQL
from ..utils import dig

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.smart")


def wallet_score(realized_usd: float, threshold_usd: float, win_rate: float, trades: int,
                 follow_wins: int = 0, follow_losses: int = 0) -> float:
    """0~100。胜率权重最大，其次利润倍数、样本量，再用跟单实际结果修正。"""
    ratio = realized_usd / threshold_usd if threshold_usd > 0 else 0
    s = 60 * min(max(win_rate, 0), 1)
    s += 25 * min(1.0, math.log10(1 + max(ratio, 0)) / math.log10(11))   # 利润是门槛 10 倍拿满分
    s += 15 * min(1.0, trades / 50)
    n = follow_wins + follow_losses
    if n >= 5:
        s += 20 * (follow_wins / n - 0.5)     # 跟单胜率 >50% 加分，<50% 扣分
    return round(max(0.0, min(100.0, s)), 1)


class SmartMoney:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg

    async def run(self) -> None:
        await self.load_manual()
        while True:
            for chain in self.cfg.enabled_chains:
                try:
                    added = await self.refresh_chain(chain)
                    if added:
                        self.app.wallets_changed[chain].set()
                except BudgetExceeded as e:
                    log.warning("聪明钱刷新暂停：%s", e)
                    break
                except Exception as e:  # noqa: BLE001
                    log.exception("刷新 %s 聪明钱失败: %s", chain, e)
            try:
                await self.reevaluate()
            except BudgetExceeded as e:
                log.warning("聪明钱复查暂停：%s", e)
            except Exception as e:  # noqa: BLE001
                log.exception("聪明钱复查失败: %s", e)
            await asyncio.sleep(float(self.cfg.get("smart_money.refresh_hours", 6)) * 3600)

    async def load_manual(self) -> None:
        for item in self.cfg.get("smart_money.manual_wallets", []) or []:
            if ":" not in str(item):
                continue
            chain, addr = str(item).split(":", 1)
            chain, addr = chain.strip().lower(), addr.strip()
            if chain in self.cfg.enabled_chains:
                await self.add_manual(chain, addr)

    async def add_manual(self, chain: str, addr: str) -> None:
        addr = norm_addr(chain, addr)
        await self.app.db.execute(
            """INSERT INTO wallets(chain,address,status,source,score,created_at,evaluated_at) VALUES(?,?,'manual','manual',70,?,0)
               ON CONFLICT(chain,address) DO UPDATE SET status='manual'""", (chain, addr, time.time()))
        self.app.wallets_changed[chain].set()

    async def threshold_usd(self) -> float:
        sol_px = await self.app.native_price("solana")
        if sol_px <= 0:
            sol_px = 150.0   # 兜底，防止取价失败把门槛变成 0
            log.warning("SOL 价格获取失败，门槛按 $150/SOL 估算")
        return float(self.cfg.get("smart_money.min_realized_profit_sol", 50)) * sol_px

    # ------------------------------------------------------------------ Birdeye 候选（备用数据源）
    async def birdeye_candidates(self, chain: str) -> dict[str, str]:
        be = self.app.birdeye
        out: dict[str, str] = {}
        for period in self.cfg.get("smart_money.gainers_types", ["1W"]) or []:
            for offset in (0, 10, 20):
                items = await be.gainers(chain, period, 10, offset)
                for it in items:
                    a = dig(it, "address", "wallet", "owner", default="")
                    if a:
                        out.setdefault(norm_addr(chain, a), f"gainers_{period}")
                if len(items) < 10:
                    break
        n_tok = int(self.cfg.get("smart_money.trending_tokens_per_chain", 20))
        n_tr = int(self.cfg.get("smart_money.top_traders_per_token", 10))
        for tok in (await be.trending(chain, n_tok))[:n_tok]:
            ta = dig(tok, "address", default="")
            if not ta:
                continue
            for tr in await be.top_traders(chain, ta, n_tr):
                a = dig(tr, "owner", "address", "wallet", default="")
                if a:
                    out.setdefault(norm_addr(chain, a), f"top_trader:{dig(tok, 'symbol', default='?')}")
        return out

    # ------------------------------------------------------------------ 刷新
    async def refresh_chain(self, chain: str) -> int:
        db = self.app.db
        cands = await self.app.data.smart_candidates(chain)
        day_ago = time.time() - 24 * 3600
        checked = {r["address"] for r in await db.fetchall(
            "SELECT address FROM candidates WHERE chain=? AND checked_at>?", (chain, day_ago))}
        known = {r["address"] for r in await db.fetchall(
            "SELECT address FROM wallets WHERE chain=? AND status IN ('active','manual','blacklist','suspect')", (chain,))}
        todo = {a: src for a, src in cands.items() if a not in checked and a not in known}
        log.info("%s 聪明钱候选 %d 个，待核实 %d 个", chain, len(cands), len(todo))
        added = await self.evaluate_many(chain, todo)
        await self.trim(chain)
        log.info("%s 新增聪明钱 %d 个", chain, added)
        return added

    async def is_person(self, chain: str, addr: str) -> bool:
        """排除池子、路由合约、程序账户（它们也会出现在排行榜里）。"""
        if chain == "solana":
            return on_curve(addr)
        try:
            return not await self.app.evm[chain].is_contract(addr)
        except Exception:  # noqa: BLE001
            return True    # 查不到就先放行，后面胜率核实会再过滤

    async def evaluate_many(self, chain: str, addrs: dict[str, str]) -> int:
        """批量核实。返回达标数量。"""
        db = self.app.db
        sm = self.cfg.get("smart_money", {}) or {}
        now = time.time()
        people = []
        for a in addrs:
            if await self.is_person(chain, a):
                people.append(a)
            else:
                await db.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?,0)", (chain, a, now))
        thr = await self.threshold_usd()
        added = 0
        for i in range(0, len(people), 100):
            chunk = people[i:i + 100]
            pnl = await self.app.data.wallet_pnl(chain, chunk, sm.get("pnl_duration", "30d"))
            for a in chunk:
                p = pnl.get(a)
                if p is None:
                    continue   # 接口没返回，下次再试
                hold = p.get("avg_hold_sec") or 0
                ok = (p["realized_usd"] >= thr and p["win_rate"] >= float(sm.get("min_win_rate", 0.6))
                      and int(sm.get("min_trades", 10)) <= p["trades"] <= int(sm.get("max_trades", 3000))
                      and not (hold and hold < float(sm.get("min_avg_hold_sec", 60))))
                await db.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)", (chain, a, now, int(ok)))
                existing = await db.wallet(chain, a)
                if ok or existing:
                    fw = existing["follow_wins"] if existing else 0
                    fl = existing["follow_losses"] if existing else 0
                    sc = wallet_score(p["realized_usd"], thr, p["win_rate"], p["trades"], fw, fl)
                    await db.upsert_wallet(chain, a, status="active" if ok else "inactive",
                                           source=addrs.get(a, "") or (existing or {}).get("source", ""),
                                           realized_usd=p["realized_usd"], win_rate=p["win_rate"],
                                           trades=p["trades"], score=sc, avg_hold_sec=float(hold or 0))
                if ok:
                    added += 1
                    log.info("✅ %s 聪明钱 %s 利润$%.0f 胜率%.0f%% 交易%d", chain, a, p["realized_usd"],
                             p["win_rate"] * 100, p["trades"])
        return added

    async def reevaluate(self) -> None:
        hours = float(self.cfg.get("smart_money.reevaluate_hours", 24))
        # 到期复查的 + 还没有平均持仓时间数据的（升级后第一次启动会补齐）
        rows = await self.app.db.fetchall(
            "SELECT chain,address,source,status FROM wallets WHERE status='active' AND COALESCE(kind,'smart')='smart' "
            "AND (evaluated_at<? OR avg_hold_sec IS NULL) LIMIT 1000", (time.time() - hours * 3600,))
        by_chain: dict[str, dict[str, str]] = {}
        for r in rows:
            if r["chain"] in self.cfg.enabled_chains:
                by_chain.setdefault(r["chain"], {})[r["address"]] = r["source"] or ""
        for chain, addrs in by_chain.items():
            await self.evaluate_many(chain, addrs)
            n = await self.app.db.fetchone(
                "SELECT COUNT(*) n FROM wallets WHERE chain=? AND status='inactive' AND evaluated_at>?",
                (chain, time.time() - 600))
            if n and n["n"]:
                self.app.wallets_changed[chain].set()

    async def trim(self, chain: str) -> None:
        """每条链只保留评分最高的一批（手动添加的不受影响）。
        保留数 = 实时监听上限的 2 倍（smart_money.pool_per_chain）：监听时要跳过扫盘型 / 交易太频繁 / 只是被提到的钱包，
        库里只留 300 个的话跳过一批后就不够 300 个了（11-12 一度只订阅了 176 个）。"""
        cap = int(self.cfg.get("smart_money.max_wallets_per_chain", 300))
        n = max(cap, int(self.cfg.get("smart_money.pool_per_chain", cap * 2) or cap * 2))
        await self.app.db.execute(
            """UPDATE wallets SET status='inactive' WHERE chain=? AND status='active' AND address NOT IN (
                 SELECT address FROM wallets WHERE chain=? AND status='active' ORDER BY """ + RANK_SQL + """ DESC LIMIT ?)""",
            (chain, chain, n))
