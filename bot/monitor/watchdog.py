"""信号源体检：某条链的聪明钱信号突然没了、或者节点连不上，在 Telegram 报警。

11-10 那次：Helius 拒绝了 Solana 的 WebSocket 连接（HTTP 429），Solana 信号从一天 1.5 万条掉到几百条，
机器人照常运行但几乎不买了，一天半以后才从漏斗里看出来。这个体检就是为了以后 10 分钟内就知道。
同一个问题 6 小时只提醒一次；恢复后会说一声。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..chains import CHAINS

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.watchdog")
DOWN_MIN = 10            # 链上监听断开多少分钟报警
WINDOW_H = 3             # 看最近 3 小时的信号
BASE_DAYS = 3            # 和之前 3 天的平均水平比
MIN_BASE = 30            # 平时每 3 小时至少有 30 条信号的链才判断"骤降"
DROP_TO = 0.15           # 掉到平时的 15% 以下算骤降
REPEAT_SEC = 6 * 3600


def node_hint(err: str) -> str:
    if "429" in err:
        return ("HTTP 429 = Helius 拒绝连接，常见两种原因：① 这个月的额度用完了（免费版每月 100 万）；"
                "② 同时连接数 / 请求频率超限。到 Helius 后台（dashboard.helius.dev）的 Usage 页看额度用了多少：\n"
                "用完了 → 升级套餐，或者等下个计费周期自动恢复；没用完 → 把这条消息发给 Claude。")
    if "401" in err or "403" in err:
        return "HTTP 401 / 403 = Helius 的 key 无效或被停用，到 Helius 后台检查 key。"
    return "多半是节点或网络临时故障，机器人会一直自动重连；持续很久的话把这条消息发给 Claude。"


class Watchdog:
    def __init__(self, app: "App"):
        self.app = app
        self.sent: dict[str, float] = {}        # 问题 → 上次提醒时间
        self.active: set[str] = set()           # 现在还没恢复的问题

    async def run(self) -> None:
        await asyncio.sleep(900)                # 启动后先让各路监听连上、攒一点数据
        while True:
            try:
                for m in await self.check():
                    await self.app.tg.send(m)
            except Exception as e:  # noqa: BLE001
                log.exception("信号源体检出错: %s", e)
            await asyncio.sleep(300)

    async def problems(self, now: float | None = None) -> dict[str, str]:
        """现在有哪些问题：问题编号 → 报警文字。"""
        now = now or time.time()
        out: dict[str, str] = {}
        sm = getattr(self.app, "solmon", None)
        if sm:
            h = sm.health()
            if h["wanted"] and not h["ok"] and h["down_min"] >= DOWN_MIN:
                out["sol_ws"] = (f"🚨 <b>Solana 聪明钱监听断了</b>：已经 {h['down_min']:.0f} 分钟连不上节点\n"
                                 f"原因：{h['err'] or '未知'}\n{node_hint(h['err'])}\n"
                                 f"影响：Solana 的跟单信号基本没有了，新币评分里\"有几个聪明钱在买\"也会偏低，买入会明显变少。")
            elif h["ok"] and h["notes"] >= 50 and h["failed"] * 2 > h["notes"]:
                err = getattr(sm, "last_tx_err", "")
                out["sol_tx"] = (f"🚨 <b>Solana 监听连上了，但取不到交易内容</b>：{h['failed']}/{h['notes']} 条失败\n"
                                 f"原因：{err or '未知'}\n{node_hint(err)}")
        db = self.app.db
        win = WINDOW_H * 3600
        for c in self.app.cfg.enabled_chains:
            recent = await db.fetchone("SELECT COUNT(*) n FROM signals WHERE chain=? AND side='buy' AND ts>=?",
                                       (c, now - win))
            base = await db.fetchone("SELECT COUNT(*) n, MIN(ts) t FROM signals WHERE chain=? AND side='buy' AND ts>=? AND ts<?",
                                     (c, now - win - BASE_DAYS * 86400, now - win))
            span = (now - win) - float(base["t"] or now)
            if span < 12 * 3600:                # 历史不到半天：没法比
                continue
            avg = base["n"] / (span / win)
            if avg >= MIN_BASE and recent["n"] < avg * DROP_TO:
                name = CHAINS[c].name if c in CHAINS else c
                out[f"quiet_{c}"] = (f"⚠️ <b>{name} 的聪明钱信号骤降</b>：最近 {WINDOW_H} 小时只有 {recent['n']} 条，"
                                     f"之前 {BASE_DAYS} 天平均每 {WINDOW_H} 小时 {avg:.0f} 条\n"
                                     f"可能是这条链的监听断了、节点额度用完，或者监听的钱包被大量淘汰。发 /status 和 /funnel 看看，"
                                     f"把结果发给 Claude。")
        return out

    async def check(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        probs = await self.problems(now)
        msgs = []
        for k, text in probs.items():
            if now - self.sent.get(k, 0) >= REPEAT_SEC:
                self.sent[k] = now
                msgs.append(text)
        for k in sorted(self.active - set(probs)):
            if k == "sol_ws":
                msgs.append("✅ Solana 聪明钱监听恢复了")
            elif k.startswith("quiet_"):
                c = k[6:]
                msgs.append(f"✅ {CHAINS[c].name if c in CHAINS else c} 的聪明钱信号恢复正常")
            self.sent.pop(k, None)
        self.active = set(probs)
        return msgs
