"""体检（每 5 分钟）：机器人出了任何问题，主动在 Telegram 提醒，不用等你来问。

查这些（同一个问题默认 6 小时只提醒一次，恢复后说一声）：
- 信号源：Solana / 各条 EVM 链的链上监听断开 10 分钟以上；Solana 连上了但取不到交易；
  某条链最近 3 小时的聪明钱信号掉到之前 3 天平均的 15% 以下；某条链监听的钱包少于 5 个
- 节点 / 数据：Solana 节点全部用不了；某个节点照今天的速度会超出每天的额度预算；GMGN 被限流暂停
- 交易：连续 6 小时没买入（附上这段时间拦下最多的原因）；最近 8 笔连续亏损
- 服务器：硬盘快满
- 日志：任何报错（ERROR）第一次出现就提醒；同一条警告 1 小时内反复出现 20 次以上也提醒

11-10 那次 Helius 额度用完，Solana 信号从一天 1.5 万条掉到几百条，一天半以后才从漏斗里看出来——这个体检就是为了以后 10 分钟内就知道。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import shutil
import time
from collections import deque
from typing import TYPE_CHECKING

from ..chains import CHAINS
from ..utils import _SecretFilter

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.watchdog")
DOWN_MIN = 10            # 链上监听断开多少分钟报警
WINDOW_H = 3             # 看最近 3 小时的信号
BASE_DAYS = 3            # 和之前 3 天的平均水平比
MIN_BASE = 30            # 平时每 3 小时至少有 30 条信号的链才判断"骤降"
DROP_TO = 0.15           # 掉到平时的 15% 以下算骤降
REPEAT_SEC = 6 * 3600
REPEAT = {"few_": 24 * 3600, "streak": 12 * 3600, "budget_": 12 * 3600}   # 这些问题提醒得少一点
MIN_WALLETS = 5
NO_BUY_H = 6
STREAK = 8
WARN_PER_H = 20
# 这些日志已经有专门的体检项（写得更清楚），日志汇总里不再重复
COVERED = ("Solana WS 断开", "%s WS 断开", "Solana 节点 %s 用不了", "Solana 监听：%s 拒绝连接", "任务 %s 异常",
           "处理 Solana 交易", "%s 轮询监听出错", "模拟卖出 %s：池子只剩", "Solana 监听积压")
RECOVER = {"sol_ws": "✅ Solana 聪明钱监听恢复了", "sol_nodes": "✅ Solana 节点恢复了",
           "gmgn": "✅ GMGN 限流结束，恢复正常"}


def chain_name(c: str) -> str:
    return CHAINS[c].name if c in CHAINS else c


class ProblemLog(logging.Handler):
    """收集机器人自己的 WARNING / ERROR 日志，按"同一句话"归类计数（不发消息，体检时再汇总）。"""

    def __init__(self, secrets=()):
        super().__init__(level=logging.WARNING)
        self.addFilter(_SecretFilter(secrets))
        self.items: dict[str, dict] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("memebot"):
                return
            tpl = str(record.msg)
            if tpl.startswith(COVERED):
                return
            key = hashlib.md5(f"{record.name}|{tpl[:120]}".encode()).hexdigest()[:12]
            it = self.items.get(key)
            if it is None:
                if len(self.items) >= 300:      # 只留最近的 300 类
                    oldest = min(self.items, key=lambda k: self.items[k]["last_ts"])
                    self.items.pop(oldest, None)
                it = self.items[key] = {"n": 0, "times": deque(maxlen=500), "level": record.levelno,
                                        "mod": record.name.replace("memebot.", "") or "main", "first_ts": record.created}
            it["n"] += 1
            it["times"].append(record.created)
            it["level"] = max(it["level"], record.levelno)
            it["last_ts"] = record.created
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None and str(record.exc_info[1]) not in msg:
                msg += f"：{record.exc_info[1]}"
            it["msg"] = msg[:300]
        except Exception:  # noqa: BLE001
            pass

    def due(self, now: float) -> dict[str, dict]:
        """值得提醒的：最近 10 分钟出现过的报错；最近 1 小时出现 20 次以上的警告。"""
        out = {}
        for k, it in self.items.items():
            recent = sum(1 for t in it["times"] if t >= now - 3600)
            if it["level"] >= logging.ERROR and it["last_ts"] >= now - 600:
                out[k] = {**it, "recent": recent}
            elif recent >= WARN_PER_H:
                out[k] = {**it, "recent": recent}
        return out


def problem_log() -> ProblemLog | None:
    return next((h for h in logging.getLogger().handlers if isinstance(h, ProblemLog)), None)


def node_hint(err: str) -> str:
    if "429" in err or "402" in err or "max usage" in err.lower():
        return ("HTTP 429 / 402 = 节点拒绝连接，常见两种原因：① 这个月的额度用完了；② 同时连接数 / 请求频率超限。"
                "到这个节点的后台看额度用了多少：\n用完了 → 加一个备用节点（python3 deploy/setup_env.py solana），"
                "或者等下个计费周期自动恢复；没用完 → 把这条消息发给 Claude。")
    if "401" in err or "403" in err:
        return ("HTTP 401 / 403 = 节点地址里的密钥无效：没复制全，或者在节点后台换过密钥。"
                "运行 python3 deploy/setup_env.py solana 重新粘贴完整地址（会先测试能不能用）。")
    return "多半是节点或网络临时故障，机器人会一直自动重连；持续很久的话把这条消息发给 Claude。"


def evm_hint(err: str) -> str:
    if "429" in err or "402" in err or "quota" in err.lower() or "limit" in err.lower():
        return ("节点拒绝了（额度用完或请求太频繁）。QuickNode 免费试用只有 1 个月、1000 万额度，到期或用完就会这样。"
                "到节点后台看一下；把这条消息发给 Claude 一起想办法（可以换别家的免费节点）。")
    if "401" in err or "403" in err:
        return "节点地址里的密钥无效或节点被停用，到节点后台检查；把这条消息发给 Claude。"
    return "多半是节点或网络临时故障，机器人会一直自动重连；持续很久的话把这条消息发给 Claude。"


from ..sources.solana_rpc import DAILY_BUDGET as BUDGET   # 免费版每月额度 / 30


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
                                 f"原因：{html.escape(h['err'] or '未知')}\n{node_hint(h['err'])}\n"
                                 f"影响：Solana 的跟单信号基本没有了，新币评分里\"有几个聪明钱在买\"也会偏低，买入会明显变少。")
            elif h["ok"] and h["notes"] >= 50 and h["failed"] * 2 > h["notes"]:
                err = getattr(sm, "last_tx_err", "")
                out["sol_tx"] = (f"🚨 <b>Solana 监听连上了，但取不到交易内容</b>：{h['failed']}/{h['notes']} 条失败\n"
                                 f"原因：{html.escape(err or '未知')}\n{node_hint(err)}")
        # ---- EVM 链监听 ----
        for c, m in (getattr(self.app, "evmmons", None) or {}).items():
            h = m.health()
            if h["wanted"] and not h["ok"] and h["down_min"] >= DOWN_MIN:
                out[f"evm_{c}"] = (f"🚨 <b>{chain_name(c)} 聪明钱监听断了</b>：已经 {h['down_min']:.0f} 分钟连不上节点\n"
                                   f"原因：{html.escape(h['err'] or '未知')}\n{evm_hint(h['err'])}")
        # ---- Solana 节点 / GMGN ----
        sol = getattr(self.app, "sol", None)
        if sol is not None and getattr(sol, "eps", None):
            if sol.out:
                whys = "；".join(f"{ep.name}：{ep.out_why}" for ep in sol.eps)
                out["sol_nodes"] = (f"🚨 <b>Solana 节点全部用不了</b>（{html.escape(whys)}）\n"
                                    f"期间代币分析改用 GMGN 的数据，Solana 链上监听也会断。\n{node_hint(whys)}")
            budgets = {**BUDGET, **(self.app.cfg.get("safety.node_daily_budget", {}) or {})}
            for ep in sol.eps:
                cap = float(budgets.get(ep.name, 0) or 0)
                start = max(float(getattr(ep, "since", 0) or 0), now - now % 86400)
                if not cap or ep.day != int(now // 86400) or now - start < 3600:
                    continue
                proj = ep.today / ((now - start) / 86400)      # 按开始计数以来的速度折算成一天
                if proj > cap * 1.3:
                    out[f"budget_{ep.name}"] = (
                        f"⚠️ <b>{ep.name} 额度用得太快</b>：今天（UTC）已用约 {ep.today:,} 次，照这个速度一天约 {proj:,.0f} 次，"
                        f"超过每天预算 {cap:,.0f} 次（按免费版每月额度 / 30 算），月底前可能用完。\n"
                        f"可以再加一个备用节点（python3 deploy/setup_env.py solana），或者把这条消息发给 Claude 收紧用量。")
        g = getattr(self.app, "gmgn", None)
        if g is not None and float(getattr(g, "paused_until", 0) or 0) > now + 60:
            until = self.app.fmt_time(g.paused_until) if hasattr(self.app, "fmt_time") else ""
            out["gmgn"] = (f"⚠️ <b>GMGN 被限流，暂停到 {until}</b>（累计被限流 {getattr(g, 'bans', 0)} 次）\n"
                           f"期间价格改用 DexScreener，止盈止损照常；分析新币、发现钱包会暂停。经常出现的话把这条发给 Claude。")
        db = self.app.db
        # ---- 钱包太少 ----
        rows = await db.fetchall("SELECT chain, COUNT(*) n FROM wallets WHERE status IN ('active','manual') GROUP BY chain")
        have = {r["chain"]: r["n"] for r in rows}
        for c in self.app.cfg.enabled_chains:
            if have.get(c, 0) < MIN_WALLETS:
                out[f"few_{c}"] = (f"⚠️ <b>{chain_name(c)} 只监听 {have.get(c, 0)} 个聪明钱</b>：钱包太少，这条链基本不会有跟单信号。"
                                   f"可能是找钱包的数据源在这条链上拿不到排行，或者钱包都被体检 / 淘汰掉了。把这条消息发给 Claude。")
        # ---- 交易 ----
        dry = int(bool(getattr(self.app.cfg, "dry_run", True)))
        started = float(getattr(self.app, "started_at", now) or now)
        last = await db.fetchone("SELECT MAX(opened_at) t FROM positions WHERE dry_run=?", (dry,))
        since = max(float(last["t"] or 0), started)
        if now - since >= NO_BUY_H * 3600:
            cats = await db.fetchall("SELECT cat, COUNT(*) n FROM skipped WHERE ts>=? GROUP BY cat ORDER BY n DESC LIMIT 3",
                                     (now - NO_BUY_H * 3600,))
            top = "、".join(f"{r['cat']} {r['n']}" for r in cats) or "没有拦截记录（可能是根本没有信号进来）"
            out["no_buys"] = (f"⚠️ <b>已经 {(now - since) / 3600:.0f} 小时没买入</b>\n"
                              f"这段时间拦下最多的：{top}\n发 /funnel 看信号有没有进来、拦在哪；有疑问把结果发给 Claude。")
        closed = await db.fetchall("SELECT cost_usd, realized_usd FROM positions WHERE status='closed' AND dry_run=? "
                                   "AND closed_at>=? AND cost_usd>0 ORDER BY closed_at DESC LIMIT ?",
                                   (dry, now - 3 * 86400, STREAK))
        if len(closed) >= STREAK and all(float(p["realized_usd"] or 0) < float(p["cost_usd"]) for p in closed):
            loss = sum(float(p["realized_usd"] or 0) - float(p["cost_usd"]) for p in closed)
            out["streak"] = (f"⚠️ <b>最近 {STREAK} 笔连续亏损</b>（合计 ${loss:,.0f}）\n"
                             f"meme 币连亏几笔很常见，不一定是出了问题；发 /diag 看看亏在哪条链、哪个来源。")
        # ---- 服务器 ----
        try:
            base = getattr(self.app.cfg, "base_dir", None) or "."
            du = shutil.disk_usage(str(base))
            if du.free < 2 * 1024 ** 3 or du.free / du.total < 0.08:
                out["disk"] = (f"🚨 <b>服务器硬盘快满了</b>：只剩 {du.free / 1024 ** 3:.1f} GB（{du.free / du.total * 100:.0f}%）\n"
                               f"满了数据库写不进去，机器人会出错。把这条消息发给 Claude。")
        except Exception:  # noqa: BLE001
            pass
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

    def log_text(self, now: float, force: bool = False) -> str:
        """日志汇总：值得提醒、而且 6 小时内没提醒过的（force=True 时列出全部）。"""
        pl = problem_log()
        if not pl:
            return ""
        items = []
        for k, it in sorted(pl.due(now).items(), key=lambda kv: -kv[1]["recent"]):
            if not force and now - self.sent.get(f"log_{k}", 0) < REPEAT_SEC:
                continue
            if not force:
                self.sent[f"log_{k}"] = now
            kind = "报错" if it["level"] >= logging.ERROR else "警告"
            items.append(f"• [{html.escape(it['mod'])}] {kind}，最近 1 小时 {it['recent']} 次：{html.escape(it['msg'][:200])}")
        if not items:
            return ""
        return ("⚠️ <b>机器人日志里有问题</b>\n" + "\n".join(items[:8]) +
                (f"\n……还有 {len(items) - 8} 类" if len(items) > 8 else "") + "\n不确定要不要紧，就把这条消息发给 Claude。")

    def repeat(self, k: str) -> float:
        return next((v for p, v in REPEAT.items() if k.startswith(p)), REPEAT_SEC)

    async def check(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        probs = await self.problems(now)
        msgs = []
        for k, text in probs.items():
            if now - self.sent.get(k, 0) >= self.repeat(k):
                self.sent[k] = now
                msgs.append(text)
        for k in sorted(self.active - set(probs)):
            if k in RECOVER:
                msgs.append(RECOVER[k])
            elif k.startswith("quiet_"):
                msgs.append(f"✅ {chain_name(k[6:])} 的聪明钱信号恢复正常")
            elif k.startswith("evm_"):
                msgs.append(f"✅ {chain_name(k[4:])} 聪明钱监听恢复了")
            elif k.startswith("few_"):
                msgs.append(f"✅ {chain_name(k[4:])} 监听的聪明钱恢复到 {MIN_WALLETS} 个以上")
            self.sent.pop(k, None)
        self.active = set(probs)
        lt = self.log_text(now)
        if lt:
            msgs.append(lt)
        return msgs

    async def report(self) -> str:
        """/problems：现在所有的问题（不管提醒过没有）。"""
        now = time.time()
        probs = await self.problems(now)
        parts = list(probs.values())
        lt = self.log_text(now, force=True)
        if lt:
            parts.append(lt)
        return "\n\n".join(parts) if parts else "✅ 体检没发现问题（信号源、节点、交易、硬盘、日志都正常）"
