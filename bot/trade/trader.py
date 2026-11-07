"""交易决策：风控检查 → 下单 → 建仓记录。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from .executors import DryRunExecutor, EvmExecutor, Fill, GmgnExecutor, SolanaExecutor
from .health import snapshot
from .positions import exit_rules

if TYPE_CHECKING:
    from ..app import App
    from ..signals import Signal

log = logging.getLogger("memebot.trader")
TIER_NAME = {"best": "🔥优质", "good": "👍较好", "normal": "一般"}
# 这些拒绝原因和标的好坏无关（开关、持仓满、冷却……），不记入"被过滤的信号"统计
NOT_QUALITY = ("交易总开关", "该链执行器", "已手动暂停", "当日亏损", "连亏", "已有持仓", "持仓数量已满", "冷却期")
SKIP_CATS = [("论证", "主动建仓论证未通过"), ("确认期：带单钱包", "带单钱包很快卖出"), ("确认期", "确认期价格异动"), ("这个钱包", "快手/战绩差钱包"),
             ("跟这个钱包", "快手/战绩差钱包"), ("致命风险", "安全风险"), ("流动性", "流动性低"), ("捆绑", "捆绑多"),
             ("埋伏", "埋伏多"), ("前10", "筹码集中"), ("卖出税", "卖出税高"), ("代币上线", "上线太久"),
             ("1 小时已涨", "已经涨太多"), ("市值虚高", "市值虚高"), ("卖盘", "卖压大"), ("持有人", "持有人太少")]


def skip_cat(why: str) -> str:
    for pre, name in SKIP_CATS:
        if why.startswith(pre):
            return name
    return "其他"


def looks_fake(rep: dict, t: dict) -> str:
    """市值和成交量严重不匹配（比如市值上亿、1 小时只成交 1 万）= 虚高市值的盘子。返回原因，正常返回空。"""
    mcap, vol = float(rep.get("mcap") or 0), float(rep.get("volume_1h") or 0)
    ratio = float(t.get("fake_mcap_vol_ratio", 500))
    if mcap >= float(t.get("fake_mcap_min_usd", 3_000_000)) and mcap > max(vol, 1) * ratio:
        return f"市值虚高（市值 ${mcap / 1e6:.1f}M，1 小时成交只有 ${vol:,.0f}）"
    return ""


class Trader:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.exec: dict = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._buying: set[str] = set()

    def setup(self) -> list[str]:
        """创建执行器。返回不可用的链（缺 key 等）。"""
        bad = []
        for c in self.cfg.enabled_chains:
            if self.cfg.dry_run:
                self.exec[c] = DryRunExecutor(self.app, c)
                continue
            try:
                if self.cfg.get("trading.executor", "gmgn") == "gmgn":
                    self.exec[c] = GmgnExecutor(self.app, c)
                else:
                    self.exec[c] = SolanaExecutor(self.app) if c == "solana" else EvmExecutor(self.app, c)
            except Exception as e:  # noqa: BLE001
                log.error("%s 执行器初始化失败: %s", c, e)
                bad.append(c)
        return bad

    def address(self, chain: str) -> str:
        ex = self.exec.get(chain)
        return getattr(ex, "pub", None) or getattr(ex, "addr", None) or "(模拟盘)"

    async def is_paused(self) -> str:
        if await self.app.db.kv_get("paused", False):
            return "已手动暂停"
        until = float(await self.app.db.kv_get("pause_until", 0) or 0)
        if until > time.time():
            return "当日亏损达到上限，暂停到明天"
        su = float(await self.app.db.kv_get("streak_pause_until", 0) or 0)
        if su > time.time():
            return f"连亏熔断，暂停买入到 {self.app.fmt_time(su)}"
        return ""

    async def check(self, chain: str, token: str, rep: dict, wallets: list[str] | None = None, reason: str = "") -> str:
        """返回拒绝原因；空字符串表示可以买。"""
        t = self.cfg.get("trading", {}) or {}
        if not t.get("enabled", False):
            return "交易总开关关闭"
        if chain not in self.exec:
            return "该链执行器不可用"
        p = await self.is_paused()
        if p:
            return p
        if rep.get("risks"):
            return "致命风险：" + "；".join(rep["risks"])
        if rep["liquidity"] < float(t.get("min_liquidity_usd", 8000)):
            return f"流动性 ${rep['liquidity']:.0f} 太低"
        if rep["bundle_held_pct"] > float(t.get("max_bundle_pct", 25)):
            return f"捆绑地址持有 {rep['bundle_held_pct']}% 超限"
        if rep["insider_held_pct"] > float(t.get("max_insider_pct", 20)):
            return f"埋伏地址持有 {rep['insider_held_pct']}% 超限"
        if rep["top10_pct"] > float(t.get("max_top10_pct", 45)):
            return f"前10持仓 {rep['top10_pct']}% 超限"
        if chain != "solana" and rep.get("sell_tax", 0) > float(t.get("max_sell_tax_pct", 10)):
            return f"卖出税 {rep['sell_tax']:.1f}% 过高"
        age = rep.get("age_hours", 0) or 0
        if age and age > float(t.get("max_token_age_hours", 72)):
            return f"代币上线 {age:.0f} 小时，超过上限"
        n_smart = len(set(wallets or []) | set(rep.get("smart_buyers") or []))
        chg = float(rep.get("change_1h") or 0)
        # 发币不到 1 小时的币，"1h 涨幅"其实是从发币价算起，天然很大，这种只靠确认期防追高；
        # 早期建仓都是刚发的币；强叙事的币发出来超过 1 小时就照常检查追高
        young = reason.startswith("早期建仓") or (reason.startswith("强叙事") and age < 1)
        if not young and chg > float(t.get("max_change_1h_pct", 300)) and n_smart < int(t.get("chase_ok_smart", 3)):
            return f"1 小时已涨 {chg:.0f}%，不追高（3 个以上聪明钱一起买才追）"
        fake = looks_fake(rep, t)
        if fake:
            return fake
        b, sl = int(rep.get("buys_1h") or 0), int(rep.get("sells_1h") or 0)
        if sl >= 30 and sl > b * float(t.get("max_sell_buy_ratio", 1.5)):
            return f"卖盘明显大于买盘（1 小时买 {b} 笔 / 卖 {sl} 笔）"
        if 0 < int(rep.get("holders") or 0) < int(t.get("min_holders", 80)):
            return f"持有人只有 {rep['holders']} 个，太少"
        db = self.app.db
        if await db.has_open_position(chain, token):
            return "已有持仓"
        if len(await db.open_positions()) >= int(t.get("max_open_positions", 10)):
            return "持仓数量已满"
        cd = float(t.get("token_cooldown_min", 120)) * 60
        if time.time() - await db.last_closed_at(chain, token) < cd:
            return "冷却期内"
        return ""

    async def consider(self, chain: str, token: str, rep: dict, reason: str, wallets: list[str],
                       tier: str | None = None) -> None:
        key = f"{chain}:{token}"
        if key in self._buying:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._buying.add(key)
            try:
                why = await self.check(chain, token, rep, wallets, reason)
                if why:
                    await self.skip(chain, token, rep, reason, why)
                    return
                await self._buy(chain, token, rep, reason, wallets, tier)
            finally:
                self._buying.discard(key)
                if len(self._locks) > 5000:
                    self._locks = {k: v for k, v in self._locks.items() if v.locked()}

    async def skip(self, chain: str, token: str, rep: dict, trigger: str, why: str) -> None:
        """记录一次被拦下的买入信号。和标的质量有关的会存下来，1 小时后回看价格，用于复盘过滤条件是否太严。"""
        log.info("跳过 %s %s（%s）：%s", chain, rep.get("symbol"), trigger, why)
        if self.cfg.get("trading.notify_skips", False):
            await self.app.tg.send(f"⏭ 跳过 {rep.get('symbol')}（{trigger}）：{why}")
        if why.startswith(NOT_QUALITY):
            return
        db = self.app.db
        if await db.fetchone("SELECT 1 FROM skipped WHERE chain=? AND token=? AND ts>?",
                             (chain, token, time.time() - 1800)):
            return   # 同一个币半小时内只记一次
        await db.execute(
            "INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), chain, token, rep.get("symbol", ""), trigger, skip_cat(why), why,
             float(rep.get("price") or 0)))

    def tier(self, rep: dict, wallets: list[str]) -> str:
        """按标的质量分档：best 优质 / good 较好 / normal 一般。"""
        t = self.cfg.get("trading.tiers", {}) or {}
        score = float(rep.get("score", 0))
        n = len(set(wallets or []) | set(rep.get("smart_buyers") or []))
        bundle = float(rep.get("bundle_held_pct", 0))
        if (score >= float(t.get("best_min_score", 85)) or n >= int(t.get("best_min_smart", 3))) \
                and bundle < float(t.get("best_max_bundle_pct", 10)):
            return "best"
        if score >= float(t.get("good_min_score", 70)) or n >= int(t.get("good_min_smart", 2)):
            return "good"
        return "normal"

    async def sizes(self, chain: str) -> list[float]:
        """三档金额 [一般, 较好, 优质]。TG 里 /size 设置过就用设置的，否则按配置（一般档 × 2 × 4）。"""
        saved = (await self.app.db.kv_get("sizes", {}) or {}).get(chain)
        if saved and len(saved) == 3:
            return [float(x) for x in saved]
        base = float(self.cfg.per_chain("trading.buy_amount_native", chain, 0))
        m = self.cfg.get("trading.size_multiplier", {}) or {}
        return [base, base * float(m.get("good", 2)), base * float(m.get("best", 4))]

    async def _buy(self, chain: str, token: str, rep: dict, reason: str, wallets: list[str],
                   tier: str | None = None) -> None:
        tier = tier or self.tier(rep, wallets)
        amount = (await self.sizes(chain))[{"normal": 0, "good": 1, "best": 2}[tier]]
        if amount <= 0:
            return
        reason = f"{reason} | {TIER_NAME[tier]}"
        ex = self.exec[chain]
        if not self.cfg.dry_run:
            reserve = float(self.cfg.per_chain("trading.reserve_native", chain, 0))
            bal = await ex.native_balance()
            if bal - amount < reserve:
                log.warning("%s 余额不足：%.4f（需 %.4f + 预留 %.4f）", chain, bal, amount, reserve)
                await self.app.tg.send(f"⚠️ {chain} 钱包余额不足（{bal:.4f}），无法买入 {rep.get('symbol')}")
                return
        slip = int(self.cfg.per_chain("trading.slippage_bps", chain, 1000))
        try:
            fill: Fill = await ex.buy(token, amount, slip)
        except Exception as e:  # noqa: BLE001
            log.exception("买入异常: %s", e)
            fill = Fill(False, error=str(e)[:200])
        npx = await self.app.native_price(chain)
        cost_usd = fill.native * npx
        ui = fill.token_raw / 10 ** fill.decimals if fill.decimals is not None else 0
        entry = cost_usd / ui if ui > 0 else rep.get("price", 0)
        await self.app.db.execute(
            "INSERT INTO trades(ts,chain,token,side,native_amount,token_raw,price_usd,usd_value,tx,dry_run,ok,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), chain, token, "buy", fill.native, str(fill.token_raw), entry, cost_usd, fill.tx,
             int(self.cfg.dry_run), int(fill.ok), fill.error))
        if not fill.ok:
            await self.app.tg.trade_failed(chain, rep, "买入", fill)
            return
        pid = await self.app.db.execute(
            """INSERT INTO positions(chain,token,symbol,status,dry_run,reason,source_wallets,entry_price,cost_native,
                 cost_usd,amount_raw,decimals,remaining_raw,high_price,opened_at,tier,entry_snap,health_at)
               VALUES(?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chain, token, rep.get("symbol", ""), int(self.cfg.dry_run), reason, json.dumps(wallets[:20]),
             entry, fill.native, cost_usd, str(fill.token_raw), fill.decimals, str(fill.token_raw), entry, time.time(),
             tier, json.dumps(snapshot(rep), ensure_ascii=False), time.time()))
        await self.app.tg.trade_buy(pid, chain, rep, fill, entry, cost_usd, reason,
                                    exit_rules(self.cfg.get("exit", {}) or {}, tier))

    async def on_smart_sell(self, s: "Signal") -> None:
        if not self.cfg.get("exit.follow_smart_sell", True):
            return
        for p in await self.app.db.open_positions():
            if p["chain"] == s.chain and p["token"] == s.token and s.wallet in json.loads(p["source_wallets"] or "[]"):
                pct = float(exit_rules(self.cfg.get("exit", {}) or {}, p.get("tier")).get("follow_smart_sell_pct", 100))
                if pct > 0:
                    tail = "" if pct >= 100 else f"，跟卖 {pct:.0f}%"
                    await self.app.positions.sell(p, pct, f"带单聪明钱 {s.wallet[:6]}… 卖出{tail}")
