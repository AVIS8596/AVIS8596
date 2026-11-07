"""主动建仓策略（不等聪明钱）：

1. 早期建仓：快毕业（bonding curve 快满）、结构干净、持有人在快速增长的币，小仓位先上。
2. 强叙事：市场注意力明显集中的币（KOL 持有、X 粉丝、付费推广、同名币扎堆且它是领头的、成交火热），先建仓再观察。

数据全部来自 GMGN trenches 列表本身（一次请求 80 个币，带持有人、KOL、聪明钱、捆绑/老鼠仓比例、
dev 持仓、社交链接等字段），不额外花接口额度。触发后仍然要过完整分析、风控过滤和 30 秒确认期，
买入后交给动态持仓管理（健康分、涨不上去就卖、止损）。

注意："叙事"在这里是用市场注意力的数据来近似，不是理解新闻内容；GMGN 自己的回测也显示叙事评分
和后续涨跌相关性不强，所以这两类只用一般档的小仓位，并且每天限量。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from typing import TYPE_CHECKING

from .chains import norm_addr
from .utils import f

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.strategy")

EARLY = "早期建仓"
NARRATIVE = "强叙事"

EARLY_DEFAULTS = {
    "enabled": True, "max_age_min": 180, "min_holders": 150, "growth_window_min": 3,
    "min_holder_growth_pct": 15, "min_volume_1h": 20000, "max_rug_ratio": 0.2, "max_top10_rate": 0.3,
    "max_bundler_rate": 0.15, "max_insider_rate": 0.15, "max_creator_rate": 0.05, "max_per_day": 6,
}
NARRATIVE_DEFAULTS = {
    "enabled": True, "min_score": 60, "max_age_hours": 24, "min_holders": 200, "min_volume_1h": 30000,
    "max_rug_ratio": 0.3, "max_top10_rate": 0.4, "max_bundler_rate": 0.3, "max_insider_rate": 0.2,
    "max_creator_rate": 0.1, "max_per_day": 6,
}


def _rate(it: dict, *keys: str) -> float:
    """比例字段统一成 0~1（有的接口给百分数）。"""
    for k in keys:
        if it.get(k) not in (None, ""):
            v = f(it.get(k))
            return v / 100 if v > 1 else v
    return 0.0


def structure_problem(it: dict, cfg: dict) -> str:
    """结构是否干净。返回问题描述，干净返回空。"""
    if str(it.get("is_wash_trading", "")).lower() in ("true", "1", "yes"):
        return "刷量"
    checks = [
        (_rate(it, "rug_ratio"), "max_rug_ratio", "跑路风险"),
        (_rate(it, "top_10_holder_rate"), "max_top10_rate", "前10持仓"),
        (_rate(it, "bundler_trader_amount_rate", "bundler_rate"), "max_bundler_rate", "捆绑交易"),
        (max(_rate(it, "rat_trader_amount_rate", "insider_rate"), _rate(it, "suspected_insider_hold_rate")),
         "max_insider_rate", "老鼠仓"),
        (_rate(it, "creator_balance_rate", "dev_team_hold_rate"), "max_creator_rate", "dev 持仓"),
    ]
    for v, key, name in checks:
        if v > float(cfg[key]):
            return f"{name} {v * 100:.0f}%"
    return ""


def early_signal(it: dict, hist: deque, now: float, cfg: dict) -> str:
    """快毕业 + 结构干净 + 持有人快速增长。符合返回买入理由，否则返回空。"""
    created = f(it.get("created_timestamp"))
    if created and now - created > float(cfg["max_age_min"]) * 60:
        return ""
    holders = int(f(it.get("holder_count")))
    if holders < int(cfg["min_holders"]) or f(it.get("volume_1h")) < float(cfg["min_volume_1h"]):
        return ""
    if structure_problem(it, cfg):
        return ""
    win = float(cfg["growth_window_min"]) * 60
    old = [(t, h) for t, h in hist if now - t <= win * 2]
    if not old or now - old[0][0] < win * 0.6 or old[0][1] <= 0:
        return ""   # 观察时间还不够，下一轮再看
    t0, h0 = old[0]
    growth = (holders / h0 - 1) * 100
    mins = (now - t0) / 60
    need = float(cfg["min_holder_growth_pct"]) * (mins / float(cfg["growth_window_min"]))
    if growth < max(need, float(cfg["min_holder_growth_pct"]) * 0.6):
        return ""
    extra = []
    if int(f(it.get("smart_degen_count"))):
        extra.append(f"聪明钱 {int(f(it.get('smart_degen_count')))}")
    if int(f(it.get("renowned_count"))):
        extra.append(f"KOL {int(f(it.get('renowned_count')))}")
    return (f"{EARLY}（快毕业，持有人 {mins:.0f} 分钟 {h0}→{holders} +{growth:.0f}%"
            + ("，" + "，".join(extra) if extra else "") + "）")


def norm_name(it: dict) -> str:
    s = str(it.get("symbol") or it.get("name") or "").lower()
    return re.sub(r"[^0-9a-z一-鿿]", "", s)


def narrative_score(it: dict, same_name: int, is_leader: bool) -> tuple[int, list[str]]:
    """市场注意力评分 0~100。"""
    s = 0
    notes: list[str] = []

    def add(v: int, why: str) -> None:
        nonlocal s
        s += v
        notes.append(why)

    kol = int(f(it.get("renowned_count")))
    if kol:
        add(35 if kol >= 3 else 25 if kol == 2 else 15, f"KOL {kol} 个")
    sm = int(f(it.get("smart_degen_count")))
    if sm:
        add(20 if sm >= 3 else 10 if sm == 2 else 5, f"聪明钱 {sm} 个")
    fol = int(f(it.get("x_user_follower")))
    if it.get("twitter") and fol >= 1000:
        add(15 if fol >= 100_000 else 10 if fol >= 10_000 else 5, f"X 粉丝 {fol / 1000:.0f}K")
    if it.get("twitter") and it.get("telegram") and it.get("website"):
        add(5, "社交齐全")
    if str(it.get("dexscr_ad", "")).lower() in ("true", "1") or f(it.get("dexscr_boost_fee")) > 0 \
            or str(it.get("dexscr_trending_bar", "")).lower() in ("true", "1"):
        add(10, "项目方在付费推广")
    if same_name >= 3 and is_leader:
        add(10, f"同名币 {same_name} 个、它领跑")
    if f(it.get("volume_1h")) >= 100_000:
        add(10, f"1h 成交 ${f(it.get('volume_1h')) / 1000:.0f}K")
    if int(f(it.get("swaps_1h"))) >= 1000:
        add(5, "交易活跃")
    if int(f(it.get("holder_count"))) >= 500:
        add(5, f"持有人 {int(f(it.get('holder_count')))}")
    return min(s, 100), notes


class Strategies:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.hist: dict[tuple[str, str], deque] = {}
        self.fired: dict[str, float] = {}   # 已触发的币（12 小时内不重复）

    def conf(self, name: str) -> dict:
        base = EARLY_DEFAULTS if name == "early" else NARRATIVE_DEFAULTS
        return {**base, **(self.cfg.get(f"strategies.{name}", {}) or {})}

    async def run(self, chain: str) -> None:
        poll = float(self.cfg.get("strategies.poll_sec", 30))
        while True:
            try:
                await self.poll(chain)
            except Exception as e:  # noqa: BLE001
                log.warning("%s 主动建仓扫描失败: %s", chain, e)
            await asyncio.sleep(poll)

    async def poll(self, chain: str, items: list[dict] | None = None) -> list[tuple[str, str, str]]:
        """扫一轮。返回本轮触发的 [(地址, 理由, 档位)]（测试用）。"""
        ec, nc = self.conf("early"), self.conf("narrative")
        if not (ec["enabled"] or nc["enabled"]):
            return []
        if items is None:
            items = await self.app.gmgn.trenches(chain, ["near_completion", "completed"], 80)
        now = time.time()
        names = Counter(norm_name(it) for it in items if norm_name(it))
        leader: dict[str, tuple[float, str]] = {}
        for it in items:
            n, v = norm_name(it), f(it.get("volume_1h"))
            if n and v > leader.get(n, (-1.0, ""))[0]:
                leader[n] = (v, str(it.get("address")))
        fired: list[tuple[str, str, str]] = []
        for it in items:
            if not it.get("address"):
                continue
            addr = norm_addr(chain, it["address"])
            key = (chain, addr)
            dq = self.hist.setdefault(key, deque(maxlen=40))
            dq_before = deque(dq)
            dq.append((now, int(f(it.get("holder_count")))))
            if now - self.fired.get(f"{chain}:{addr}", 0) < 12 * 3600:
                continue
            reason, tier = "", "normal"
            if ec["enabled"] and it.get("_stage") == "即将毕业":
                reason = early_signal(it, dq_before, now, ec)
                if reason and (int(f(it.get("smart_degen_count"))) >= 2 or int(f(it.get("renowned_count"))) >= 1):
                    tier = "good"
            if not reason and nc["enabled"]:
                created = f(it.get("created_timestamp"))
                n = norm_name(it)
                ok_basic = (not created or now - created <= float(nc["max_age_hours"]) * 3600) \
                    and int(f(it.get("holder_count"))) >= int(nc["min_holders"]) \
                    and f(it.get("volume_1h")) >= float(nc["min_volume_1h"]) \
                    and not structure_problem(it, nc)
                if ok_basic:
                    is_leader = bool(n) and leader.get(n, (0, ""))[1] == str(it.get("address"))
                    if names.get(n, 0) >= 3 and not is_leader:
                        continue   # 同名跟风盘，不碰
                    sc, notes = narrative_score(it, names.get(n, 0), is_leader)
                    if sc >= int(nc["min_score"]):
                        reason = f"{NARRATIVE}（{sc} 分：{'、'.join(notes[:4])}）"
            if reason:
                self.fired[f"{chain}:{addr}"] = now
                fired.append((addr, reason, tier))
                t = asyncio.create_task(self.fire(chain, addr, reason, tier))
                self.app.hub._tasks.add(t)
                t.add_done_callback(self.app.hub._tasks.discard)
        cutoff = now - 3600
        for k in [k for k, d in self.hist.items() if d and d[-1][0] < cutoff]:
            self.hist.pop(k, None)
        for k in [k for k, t in self.fired.items() if now - t > 12 * 3600]:
            self.fired.pop(k, None)
        return fired

    async def fire(self, chain: str, token: str, reason: str, tier: str) -> None:
        name = "early" if reason.startswith(EARLY) else "narrative"
        prefix = EARLY if name == "early" else NARRATIVE
        cap = int(self.conf(name)["max_per_day"])
        n = await self.app.db.fetchone(
            "SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?",
            (prefix + "%", self.app.day_start()))
        if n and n["n"] >= cap:
            log.info("%s 今天已买 %d 笔，达到上限，跳过 %s", prefix, n["n"], token)
            return
        rep = await self.app.analyzer.analyze(chain, token, source=prefix)
        if not rep:
            return
        log.info("触发%s %s：%s", prefix, rep.get("symbol"), reason)
        await self.app.hub.confirm_and_buy(chain, token, rep, reason, wallets=rep.get("smart_buyers") or [],
                                           tier=tier)
