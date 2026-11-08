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
NOT_QUALITY = ("交易总开关", "该链执行器", "已手动暂停", "当日亏损", "连亏", "已有持仓", "持仓数量已满", "冷却期",
               "市场冷", "总敞口")
SKIP_CATS = [("论证", "主动建仓论证未通过"), ("确认期：带单钱包", "带单钱包很快卖出"), ("确认期", "确认期价格异动"), ("这个钱包", "快手/战绩差钱包"),
             ("跟这个钱包", "快手/战绩差钱包"), ("KOL 单独", "KOL 单独买入"), ("致命风险", "安全风险"), ("流动性", "流动性低"), ("捆绑", "捆绑多"),
             ("埋伏", "埋伏多"), ("前10", "筹码集中"), ("卖出税", "卖出税高"), ("代币上线", "上线太久"),
             ("1 小时已涨", "已经涨太多"), ("市值虚高", "市值虚高"), ("卖盘", "卖压大"), ("持有人", "持有人太少"),
             ("从 2 小时内高点", "接飞刀"), ("从发币价", "涨幅已兑现"), ("最近 5 分钟", "正在砸盘"), ("项目方", "坏项目方"), ("同名币", "同名币"), ("下单前复查", "复查拿不到数据"), ("团伙盘", "团伙盘"), ("阴谋地址持有", "阴谋地址多"), ("带单钱包体检", "钱包体检不过")]


def norm_symbol(s) -> str:
    return str(s or "").strip().upper().lstrip("$")


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
        self._bal_warned: dict[str, float] = {}
        self._launch: dict[str, float] = {}    # 发币价缓存（判断"涨幅已兑现"用）

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
        from .regime import regime
        rg = await regime(self.app)
        if rg.get("pause"):
            return "市场冷，暂停买入（最近买入 1 小时后大多在跌）"
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
        from ..discovery.audit import token_roles
        roles = token_roles(rep)      # 这个币的项目方 / 捆绑 / 老鼠仓 / 狙击不算"聪明钱"
        n_smart = len((set(wallets or []) | set(rep.get("smart_buyers") or [])) - set(roles))
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
        # 不接飞刀：从 2 小时内的高点已经跌掉一大半的币，多半是被砸完的盘（放最后，要多查一次 K 线）
        # 正在砸盘：最近 5 分钟跌超 20% 的不买（不管几个聪明钱）
        dd_max = float(t.get("max_drawdown_pct", 60))
        px = float(rep.get("price") or 0)
        if px > 0:
            hi, px5 = await self.app.data.recent_high_and_ago(chain, token, 120, 5)
            drop5 = float(t.get("max_drop_5m_pct", 20))
            if drop5 and px5 > 0 and (1 - px / px5) * 100 >= drop5:
                return f"最近 5 分钟跌了 {(1 - px / px5) * 100:.0f}%，正在砸盘"
            if dd_max and n_smart < int(t.get("chase_ok_smart", 3)) and hi > 0:
                dd = (1 - px / hi) * 100
                if dd >= dd_max:
                    return f"从 2 小时内高点已经跌了 {dd:.0f}%，不接飞刀"
        cap = int(t.get("max_open_positions", 10))
        if self.cfg.dry_run and t.get("max_open_positions_dry"):
            cap = int(t["max_open_positions_dry"])    # 模拟盘多条链一起跑，名额放宽，多积累数据
        if len(await db.open_positions()) >= cap:
            return "持仓数量已满"
        cd = float(t.get("token_cooldown_min", 120)) * 60
        if time.time() - await db.last_closed_at(chain, token) < cd:
            return "冷却期内"
        # 涨幅已经兑现：刚发不久的币，价格已经是发币价的很多倍（默认 10 倍）还没回调，多半是在山顶接盘
        lx = float(t.get("max_launch_multiple", 10))
        if lx and 0 < age <= 24 and n_smart < int(t.get("chase_ok_smart", 3)) and float(rep.get("price") or 0) > 0:
            base = await self.launch_price(chain, token, time.time() - age * 3600)
            if base > 0:
                rep["launch_x"] = float(rep["price"]) / base
                if rep["launch_x"] >= lx:
                    return (f"从发币价已经涨了 {rep['launch_x']:.0f} 倍（上限 {lx:g} 倍），涨幅大部分已经兑现，"
                            f"不在山顶接（3 个以上聪明钱一起买才追）")
        # 阴谋团伙 / 阴谋地址：早期可疑买家里有同一个团伙的 3 个以上成员；已知阴谋地址持仓太多
        gmax = int(t.get("max_gang_members", 3))
        if gmax and int(rep.get("gang_n") or 0) >= gmax:
            return (f"团伙盘：早期买家里有同一个团伙的 {int(rep['gang_n'])} 个成员"
                    f"（这个团伙最近一起出现在 {int(rep.get('gang_tokens') or 0)} 个币里）")
        cmax = float(t.get("max_cabal_pct", 20))
        if cmax and float(rep.get("cabal_pct") or 0) >= cmax:
            return f"阴谋地址持有 {float(rep['cabal_pct']):.0f}%（上限 {cmax:g}%）"
        # 坏人记忆 + 批量发币：让我们亏光过的项目方 / 同名币不再买；发过一大堆币的项目方不碰
        bad = await self.bad_actor(chain, token, rep)
        if bad:
            return bad
        # 止损 / 紧急卖出过的币 24 小时内不再买
        last = await db.fetchone("SELECT close_reason, closed_at FROM positions WHERE chain=? AND token=? "
                                 "AND status='closed' ORDER BY closed_at DESC LIMIT 1", (chain, token))
        if last and str(last["close_reason"] or "").startswith(("止损", "🚨", "取不到价格")) and \
                time.time() - float(last["closed_at"] or 0) < float(t.get("stoploss_cooldown_hours", 24)) * 3600:
            return "冷却期内（这个币止损过，24 小时内不再买）"
        return ""

    async def bad_actor(self, chain: str, token: str, rep: dict) -> str:
        t = self.cfg.get("trading", {}) or {}
        db = self.app.db
        creator = str(rep.get("creator") or "")
        sym = norm_symbol(rep.get("symbol"))
        now = time.time()
        if creator:
            r = await db.fetchone("SELECT token, reason FROM bad_actors WHERE chain=? AND kind='creator' AND key=? AND ts>?",
                                  (chain, creator, now - 30 * 86400))
            if r:
                return f"项目方黑名单：这个项目方的币以前让我们亏光过（{r['reason']}），不再碰它发的币"
        if sym:
            r = await db.fetchone("SELECT reason FROM bad_actors WHERE kind='symbol' AND key=? AND ts>?",
                                  (sym, now - float(t.get("bad_symbol_hours", 48)) * 3600))
            if r:
                return f"同名币黑名单：{rep.get('symbol')} 这个名字 48 小时内刚亏光过（{r['reason']}），换个合约重发多半是同一伙人"
            r = await db.fetchone("SELECT token FROM positions WHERE UPPER(TRIM(symbol))=? AND NOT (chain=? AND token=?) "
                                  "AND opened_at>?", (sym, chain, token, now - 24 * 3600))
            if r:
                return f"同名币：24 小时内买过另一个叫 {rep.get('symbol')} 的币，同名重发多半是跟风盘或同一伙人"
        mx = int(t.get("max_dev_launches", 15))
        if mx and int(rep.get("dev_launches") or 0) >= mx:
            return f"项目方批量发币：以前已经发过 {int(rep['dev_launches'])} 个币（上限 {mx}），撤池子的高发人群"
        if creator:
            n = await db.fetchone("SELECT COUNT(*) n FROM tokens WHERE chain=? AND creator=? AND first_seen>?",
                                  (chain, creator, now - 7 * 86400))
            if n and n["n"] >= int(t.get("max_dev_tokens_7d", 3)) + 1:
                return f"项目方批量发币：7 天内机器人见过它发的 {n['n']} 个币"
        return ""

    async def launch_price(self, chain: str, token: str, created: float) -> float:
        k = f"{chain}:{token}"
        if k not in self._launch:
            from .history import launch_price
            if len(self._launch) > 3000:
                self._launch.clear()
            self._launch[k] = await launch_price(self.app, chain, token, created)
        return self._launch[k]

    def explain(self, rep: dict, reason: str = "强叙事") -> list[str]:
        """把买入前的每一条风控过滤逐条列出来（✅ 通过 / ❌ 不通过），给 /whynot 用。"""
        t = self.cfg.get("trading", {}) or {}
        age = float(rep.get("age_hours") or 0)
        chg = float(rep.get("change_1h") or 0)
        b, sl = int(rep.get("buys_1h") or 0), int(rep.get("sells_1h") or 0)
        young = age < 1
        rows = [
            (not rep.get("risks"), "安全风险：" + ("；".join(rep.get("risks") or []) or "无")),
            (rep["liquidity"] >= float(t.get("min_liquidity_usd", 8000)),
             f"流动性 ${rep['liquidity']:,.0f}（要 ≥ ${float(t.get('min_liquidity_usd', 8000)):,.0f}）"),
            (rep["bundle_held_pct"] <= float(t.get("max_bundle_pct", 25)),
             f"捆绑地址持有 {rep['bundle_held_pct']}%（要 ≤ {t.get('max_bundle_pct', 25)}%）"),
            (rep["insider_held_pct"] <= float(t.get("max_insider_pct", 20)),
             f"埋伏地址持有 {rep['insider_held_pct']}%（要 ≤ {t.get('max_insider_pct', 20)}%）"),
            (rep["top10_pct"] <= float(t.get("max_top10_pct", 45)),
             f"前10持仓 {rep['top10_pct']}%（要 ≤ {t.get('max_top10_pct', 45)}%）"),
            (young or chg <= float(t.get("max_change_1h_pct", 300)),
             f"1 小时涨幅 {chg:.0f}%（要 ≤ {t.get('max_change_1h_pct', 300)}%" + ("，发币不到 1 小时不查）" if young else "）")),
            (not looks_fake(rep, t), looks_fake(rep, t) or "市值和成交量匹配"),
            (not (sl >= 30 and sl > b * float(t.get("max_sell_buy_ratio", 1.5))), f"1 小时买 {b} 笔 / 卖 {sl} 笔"),
            (int(rep.get("holders") or 0) >= int(t.get("min_holders", 80)),
             f"持有人 {rep.get('holders')}（要 ≥ {t.get('min_holders', 80)}）"),
        ]
        if rep.get("launch_x"):
            lx = float(t.get("max_launch_multiple", 10))
            rows.append((rep["launch_x"] < lx, f"从发币价涨了 {rep['launch_x']:.1f} 倍（要 ＜ {lx:g} 倍）"))
        return [("✅ " if ok else "❌ ") + txt for ok, txt in rows]

    async def consider(self, chain: str, token: str, rep: dict, reason: str, wallets: list[str],
                       tier: str | None = None) -> None:
        key = f"{chain}:{token}"
        if key in self._buying:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._buying.add(key)
            try:
                rep = await self.fresh(chain, token, rep)
                if rep is None:
                    return
                why = await self.check(chain, token, rep, wallets, reason)
                if why:
                    await self.skip(chain, token, rep, reason, why)
                    return
                await self._buy(chain, token, rep, reason, wallets, tier)
            finally:
                self._buying.discard(key)
                if len(self._locks) > 5000:
                    self._locks = {k: v for k, v in self._locks.items() if v.locked()}

    async def fresh(self, chain: str, token: str, rep: dict) -> dict | None:
        """下单前复查：报告超过 20 秒就重新完整分析一次（价格、持有人、流动性、筹码、还拿着的聪明钱都是现在的），
        所有过滤条件都用最新数据判断，不用几分钟前的旧数据下单。"""
        age = time.time() - float(rep.get("analyzed_at") or 0)
        if age <= float(self.cfg.get("trading.max_rep_age_sec", 20)):
            return rep
        an = getattr(self.app, "analyzer", None)
        if not an:
            return rep
        try:
            new = await an.analyze(chain, token, source="下单前复查", force=True)
        except Exception as e:  # noqa: BLE001
            log.warning("下单前复查失败 %s: %s", token, e)
            new = None
        if not new:
            log.info("下单前复查拿不到数据，放弃 %s", rep.get("symbol"))
            await self.skip(chain, token, rep, "下单前复查", "下单前复查拿不到最新数据，放弃")
            return None
        return {**rep, **new}

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
        price = float(rep.get("price") or 0)
        if price <= 0:
            try:
                price = await self.app.data.price(chain, token)
            except Exception:  # noqa: BLE001
                price = 0.0
        await db.execute(
            "INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), chain, token, rep.get("symbol", ""), trigger, skip_cat(why), why, price))

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

    @property
    def sizes_key(self) -> str:
        """实盘和模拟盘的三档金额分开存：模拟盘调大金额，切回实盘不会跟着变大。"""
        return "sizes_dry" if self.cfg.dry_run else "sizes"

    async def sizes(self, chain: str) -> list[float]:
        """三档金额 [一般, 较好, 优质]。TG 里 /size 设置过就用设置的，否则按配置（一般档 × 2 × 4）。"""
        saved = (await self.app.db.kv_get(self.sizes_key, {}) or {}).get(chain)
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
        from .regime import regime
        rg = await regime(self.app)
        if rg.get("mult", 1) != 1:
            amount = round(amount * rg["mult"], 4)
            reason += f" | 市场{ {'cold': '冷', 'hot': '热'}.get(rg['state'], '一般') }，仓位 ×{rg['mult']:g}"
        from .sizing import exposure_ok, size_mult
        sm, why = await size_mult(self.app, reason, tier)       # 仓位按各买入来源 / 档位的真实战绩调整
        if sm != 1:
            amount = round(amount * sm, 4)
            reason += f" | {why}"
        ex = self.exec[chain]
        npx_e = await self.app.native_price(chain)
        ok, why = await exposure_ok(self.app, amount * npx_e)
        if not ok:
            await self.skip(chain, token, rep, "总敞口", f"总敞口已到上限：{why}")
            return
        # 单笔不超过池子流动性的 3%：池子太小时自动减少买入量（大额模拟盘、以后加大实盘金额时有用）
        liq_pct = float(self.cfg.get("trading.max_liquidity_pct", 3))
        npx0 = await self.app.native_price(chain)
        liq = float(rep.get("liquidity") or 0)
        if liq_pct and liq > 0 and npx0 > 0 and amount * npx0 > liq * liq_pct / 100:
            new_amt = round(liq * liq_pct / 100 / npx0, 4)
            if new_amt < float(self.cfg.get("trading.min_buy_native", 0.01)):
                log.info("池子太小（$%.0f），%s 不买", liq, rep.get("symbol"))
                return
            reason += f" | 池子小，减到 {new_amt:g}"
            amount = new_amt
        if not self.cfg.dry_run:
            reserve = float(self.cfg.per_chain("trading.reserve_native", chain, 0))
            bal = await ex.native_balance()
            if bal - amount < reserve:
                log.warning("%s 余额不足：%.4f（需 %.4f + 预留 %.4f）", chain, bal, amount, reserve)
                if time.time() - self._bal_warned.get(chain, 0) > 3600:   # 一小时只提醒一次，不刷屏
                    self._bal_warned[chain] = time.time()
                    await self.app.tg.send(f"⚠️ {chain} 钱包余额不足（{bal:.4f}，每笔需要 {amount:g} + 预留 {reserve:g}），"
                                           f"买不了。充值后会自动恢复；这一小时内不再重复提醒")
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
            "INSERT INTO trades(ts,chain,token,side,native_amount,token_raw,price_usd,usd_value,tx,dry_run,ok,error,"
            "quote_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), chain, token, "buy", fill.native, str(fill.token_raw), entry, cost_usd, fill.tx,
             int(self.cfg.dry_run), int(fill.ok), fill.error, fill.quote or None))
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
        await self.app.db.execute("UPDATE token_outcomes SET bought=1 WHERE chain=? AND token=? AND ts>?",
                                  (chain, token, time.time() - 3600))
        await self.app.tg.trade_buy(pid, chain, rep, fill, entry, cost_usd, reason,
                                    exit_rules(self.cfg.get("exit", {}) or {}, tier))

    async def on_smart_sell(self, s: "Signal") -> None:
        """带单的聪明钱卖出时跟卖。多个钱包一起带单的，要过半数卖出才跟——
        不然一个快进快出的钱包先走，就会把其他钱包还拿着的好单子提前卖掉。"""
        if not self.cfg.get("exit.follow_smart_sell", True):
            return
        for p in await self.app.db.open_positions():
            sources = json.loads(p["source_wallets"] or "[]")
            if p["chain"] != s.chain or p["token"] != s.token or s.wallet not in sources:
                continue
            rows = await self.app.db.fetchall(
                "SELECT wallet, side FROM signals WHERE chain=? AND token=? AND ts>=? ORDER BY ts",
                (s.chain, s.token, float(p["opened_at"] or 0) - 3600))
            last = {r["wallet"]: r["side"] for r in rows if r["wallet"] in sources}
            last[s.wallet] = "sell"
            sold = sum(1 for w in sources if last.get(w) == "sell")
            if len(sources) > 1 and sold * 2 <= len(sources):
                log.info("#%s %s 带单钱包 %d/%d 卖出，未过半，继续持有", p["id"], p["symbol"], sold, len(sources))
                continue
            pct = float(exit_rules(self.cfg.get("exit", {}) or {}, p.get("tier")).get("follow_smart_sell_pct", 100))
            if pct > 0:
                tail = "" if pct >= 100 else f"，跟卖 {pct:.0f}%"
                who = f"{s.wallet[:6]}…" if len(sources) == 1 else f"{sold}/{len(sources)} 个"
                await self.app.positions.sell(p, pct, f"带单聪明钱 {who} 卖出{tail}")
