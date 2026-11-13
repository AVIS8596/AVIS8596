"""对照仓：只要不是阴谋盘 / 貔貅 / 池子能撤 / 刷量盘这些硬坑，机器人分析到的币就直接买（¼ 仓位），一直买卖、多攒真实成交数据，
也当"对照组"：卖法和原有来源一样（高手规则），所以它和跟单 / 新币 / 早期建仓的成绩一比，
就知道这些来源"挑币"到底有没有比"随便买"好。

买入：机器人每分析完一个币（任何来源、任何发币时间），按 sample_rate 抽（1 = 全要），走买卖力量打法那套只排硬坑的检查
（trader.check_lite：致命风险、池子能撤、捆绑 ≥ 30%、埋伏多、阴谋地址、团伙、坏项目方、项目方在卖、流动性太小、
刷量盘 / 钓鱼钱包多 / 市值是池子几十倍、拉盘太平滑），再过 30 秒确认期。

11-13 起不限每天 / 每小时笔数（max_per_day / max_per_hour = 0），唯一的上限是"同时最多拿 max_open 个"：
每个持仓都要不停取价、定期复查，这些都花 GMGN 的接口额度，拿太多会把分析新币、/check 挤慢。卖掉一个就能再买一个。
为了省额度，对照仓的持仓：价格最多用 20 秒前的（别的持仓 5 秒）、健康复查间隔 ×4、不做新旧卖法的虚拟对比。
买卖太多，不逐笔推送（control.notify: true 可以打开），每小时发一条汇总；/positions、/compare、日报里都有它。
同一个币 6 小时内只考虑一次。配置 control.*，关掉：control.enabled: false。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.control")

PREFIX = "对照仓"
DEFAULTS = {"enabled": True, "sample_rate": 1.0, "max_per_day": 0, "max_per_hour": 0, "max_open": 40,
            "notify": False, "summary_min": 60, "price_max_age": 20, "health_every_x": 4}


def is_control(reason: str | None) -> bool:
    return str(reason or "").startswith(PREFIX)


def conf(cfg) -> dict:
    return {**DEFAULTS, **((cfg.get("control", {}) if cfg else {}) or {})}


def quiet(cfg, reason: str | None) -> bool:
    """这笔单子的买卖消息要不要静音（对照仓买卖太多，默认不逐笔推送）。"""
    return is_control(reason) and not conf(cfg).get("notify")


class ControlLane:
    def __init__(self, app: "App"):
        self.app = app
        self._seen: dict[str, float] = {}
        self._pending = 0            # 已经交给确认期、还没出结果的笔数（算进名额，免得一下子买超）

    def c(self) -> dict:
        return conf(self.app.cfg)

    async def room(self) -> bool:
        """还有没有名额：同时拿着的 + 确认期里的 < max_open；每天 / 每小时上限为 0 表示不限。"""
        c = self.c()
        db = self.app.db
        now = time.time()
        like = PREFIX + "%"
        for since, cap in ((self.app.day_start(), c["max_per_day"]), (now - 3600, c["max_per_hour"])):
            if int(cap or 0) <= 0:
                continue
            r = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?", (like, since))
            if r and r["n"] >= int(cap):
                return False
        r = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND status='open'", (like,))
        return (r["n"] if r else 0) + self._pending < int(c["max_open"])

    async def on_analysis(self, chain: str, token: str, rep: dict) -> None:
        c = self.c()
        if not c.get("enabled") or rep.get("risks"):
            return
        now = time.time()
        key = f"{chain}:{token}"
        if now - self._seen.get(key, 0) < 6 * 3600:
            return
        if random.random() >= float(c["sample_rate"]) or not await self.room():
            return                    # 没名额时不记"看过"：等卖掉腾出名额，这个币下次被分析时还能买
        self._seen[key] = now
        if len(self._seen) > 50000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 6 * 3600}
        age = float(rep.get("age_hours") or 0)
        when = f"发币 {age * 60:.0f} 分钟" if age < 2 else f"发币 {age:.0f} 小时"
        reason = f"{PREFIX}（实验仓，发现就买、只排阴谋盘，{when}）"
        hub = self.app.hub
        self._pending += 1

        async def go() -> None:
            try:
                # wallets 不传：这笔不是跟谁买的，亏了不能算到聪明钱头上
                await hub.confirm_and_buy(chain, token, rep, reason, wallets=[], tier="normal")
            finally:
                self._pending -= 1
        t = asyncio.create_task(go())
        hub._tasks.add(t)
        t.add_done_callback(hub._tasks.discard)

    async def summary(self, hours: float = 1) -> str:
        db = self.app.db
        now = time.time()
        like = PREFIX + "%"
        since = now - hours * 3600
        dry = int(bool(self.app.cfg.dry_run))
        b = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND dry_run=? AND opened_at>=?",
                              (like, dry, since))
        rows = await db.fetchall("SELECT cost_usd, realized_usd FROM positions WHERE reason LIKE ? AND dry_run=? "
                                 "AND status='closed' AND closed_at>=? AND cost_usd>0", (like, dry, since))
        o = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND status='open'", (like,))
        day = await db.fetchall("SELECT cost_usd, realized_usd FROM positions WHERE reason LIKE ? AND dry_run=? "
                                "AND status='closed' AND closed_at>=? AND cost_usd>0", (like, dry, self.app.day_start()))

        def part(rs: list[dict]) -> str:
            if not rs:
                return "0 笔"
            rets = sorted(float(r["realized_usd"] or 0) / float(r["cost_usd"]) - 1 for r in rs)
            pnl = sum(float(r["realized_usd"] or 0) - float(r["cost_usd"]) for r in rs)
            return (f"{len(rs)} 笔，每笔平均 {sum(rets) / len(rets) * 100:+.1f}%、中位 {rets[len(rets) // 2] * 100:+.0f}%、"
                    f"赚钱 {sum(1 for x in rets if x > 0) / len(rets) * 100:.0f}%，合计 ${pnl:+.2f}")
        return (f"🧪 <b>对照仓</b>（发现就买、只排阴谋盘；不逐笔推送）\n"
                f"最近 {hours:g} 小时：买入 {b['n'] if b else 0} 笔 | 平仓 {part(rows)}\n"
                f"今天平仓 {part(day)}\n"
                f"现在拿着 {o['n'] if o else 0} / {int(self.c()['max_open'])} 个（/positions 看明细，/compare 和别的打法比）")

    async def run(self) -> None:
        """每小时发一条汇总（这一小时没买也没卖就不发）。"""
        while True:
            c = self.c()
            await asyncio.sleep(max(600.0, float(c.get("summary_min", 60)) * 60))
            try:
                if not c.get("enabled") or c.get("notify"):
                    continue
                txt = await self.summary(float(c.get("summary_min", 60)) / 60)
                if "买入 0 笔 | 平仓 0 笔" not in txt:
                    await self.app.tg.send(txt, low=True)
            except Exception as e:  # noqa: BLE001
                log.warning("对照仓汇总失败: %s", e)
