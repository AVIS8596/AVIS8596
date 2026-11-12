"""对照仓（11-12）：只要不是阴谋盘 / 貔貅 / 池子能撤 / 刷量盘这些硬坑，机器人分析到的币随机抽一部分直接买（¼ 仓位），
多收集真实成交数据，也当"对照组"：卖法和原有来源一样（高手规则），所以它和跟单 / 新币 / 早期建仓的成绩一比，
就知道这些来源"挑币"到底有没有比"随便买"好。

买入：机器人每分析完一个币（任何来源、任何发币时间），先按 sample_rate 随机抽，抽中再走买卖力量打法那套只排硬坑的检查
（trader.check_lite：致命风险、池子能撤、捆绑 ≥ 30%、埋伏多、阴谋地址、团伙、坏项目方、项目方在卖、流动性太小、
刷量盘 / 钓鱼钱包多 / 市值是池子几十倍、拉盘太平滑），再过 30 秒确认期。
上限：每天 max_per_day 笔、每小时 max_per_hour 笔、同时最多拿 max_open 个（不挤占其他来源的持仓名额）。
同一个币 6 小时内只考虑一次。配置 control.*，关掉：control.enabled: false。
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App

PREFIX = "对照仓"
DEFAULTS = {"enabled": True, "sample_rate": 0.2, "max_per_day": 40, "max_per_hour": 3, "max_open": 12}


class ControlLane:
    def __init__(self, app: "App"):
        self.app = app
        self._seen: dict[str, float] = {}

    def c(self) -> dict:
        return {**DEFAULTS, **(self.app.cfg.get("control", {}) or {})}

    async def room(self) -> bool:
        """今天 / 这一小时 / 同时拿着的对照仓还有没有名额。"""
        c = self.c()
        db = self.app.db
        now = time.time()
        like = PREFIX + "%"
        for since, cap in ((self.app.day_start(), c["max_per_day"]), (now - 3600, c["max_per_hour"])):
            r = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?", (like, since))
            if r and r["n"] >= int(cap):
                return False
        r = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND status='open'", (like,))
        return not (r and r["n"] >= int(c["max_open"]))

    async def on_analysis(self, chain: str, token: str, rep: dict) -> None:
        c = self.c()
        if not c.get("enabled") or rep.get("risks"):
            return
        now = time.time()
        key = f"{chain}:{token}"
        if now - self._seen.get(key, 0) < 6 * 3600:
            return
        self._seen[key] = now
        if len(self._seen) > 50000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 6 * 3600}
        if random.random() >= float(c["sample_rate"]) or not await self.room():
            return
        age = float(rep.get("age_hours") or 0)
        when = f"发币 {age * 60:.0f} 分钟" if age < 2 else f"发币 {age:.0f} 小时"
        reason = f"{PREFIX}（实验仓，发现就买、只排阴谋盘，{when}）"
        hub = self.app.hub
        t = asyncio.create_task(hub.confirm_and_buy(chain, token, rep, reason, wallets=rep.get("smart_buyers") or [],
                                                    tier="normal"))
        hub._tasks.add(t)
        t.add_done_callback(hub._tasks.discard)
