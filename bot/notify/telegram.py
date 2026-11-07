"""Telegram 推送 + 命令控制。只响应你自己的 chat id，其他人发命令一律忽略。"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING, Any

from ..chains import CHAINS, norm_addr
from ..http import ApiError
from ..utils import fmt_price, fmt_usd, short

if TYPE_CHECKING:
    from ..app import App
    from ..signals import Signal
    from ..trade.executors import Fill

log = logging.getLogger("memebot.tg")
e = html.escape

HELP = """<b>命令列表</b>
/status 运行状态
/positions 当前持仓（🔥优质 👍较好，带健康分）
/why 编号 查看某个持仓的健康分、加减分原因和卖出规则，例：/why 3
/whynot 代币地址 这个币为什么没买（逐条列出每项检查）
/dynamic 动态管理状态和新旧规则对比
/dynamic on | shadow | off 接管卖出 / 只观察 / 关闭
/review 立即看今日复盘
/sell 编号 [百分比] 手动卖出，例：/sell 3 50
/size 查看每笔买入金额（一般 / 较好 / 优质 三档）
/size 0.05 0.1 0.2 修改 Solana 三档金额（单位 SOL）
/size bsc 0.01 0.02 0.04 修改其他链
/pause 暂停买入（止盈止损照常）
/resume 恢复买入
/wallets [链] 查看监听的聪明钱
/addwallet 链 地址 手动添加聪明钱
/delwallet 链 地址 拉黑地址
/check 链 代币地址 立即分析一个币
/tag 链 地址 查这个地址被标记过什么（捆绑/埋伏/dev）
链名：solana / bsc / ethereum / base"""


class Telegram:
    def __init__(self, app: "App"):
        self.app = app
        self.token = app.cfg.secrets.telegram_token
        self.chat = str(app.cfg.secrets.telegram_chat_id)
        self.base = f"https://api.telegram.org/bot{self.token}"
        self.q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._err_sent: dict[str, float] = {}
        self._cmd_tasks: set[asyncio.Task] = set()

    # ================================================================ 发送
    async def send(self, text: str) -> None:
        if len(text) > 4000:
            text = text[:3990] + "…"
        try:
            self.q.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("TG 消息队列已满，丢弃一条")

    async def sender(self) -> None:
        """串行发送，每条间隔 1.1 秒，避免触发 Telegram 限流。"""
        while True:
            text = await self.q.get()
            for _ in range(5):
                try:
                    r = await self.app.http.post("tg", f"{self.base}/sendMessage", retries=2, json_body={
                        "chat_id": self.chat, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": True})
                    if r and r.get("ok"):
                        break
                except ApiError as ex:
                    if ex.status == 400 and "parse" in ex.body:   # HTML 格式问题，改纯文本再发
                        text = html.unescape(text.replace("<b>", "").replace("</b>", "")
                                             .replace("<code>", "").replace("</code>", ""))
                        import re
                        text = re.sub(r"<[^>]+>", "", text)
                        await self.app.http.post("tg", f"{self.base}/sendMessage", retries=1,
                                                 json_body={"chat_id": self.chat, "text": text})
                        break
                    log.warning("TG 发送失败: %s", ex)
                    await asyncio.sleep(3)
                except Exception as ex:  # noqa: BLE001
                    log.warning("TG 发送异常: %s", ex)
                    await asyncio.sleep(3)
            await asyncio.sleep(1.1)

    async def error(self, name: str, ex: BaseException) -> None:
        """模块异常提醒，同类错误 30 分钟内只推一次。"""
        k = f"{name}:{type(ex).__name__}"
        if time.time() - self._err_sent.get(k, 0) < 1800:
            return
        self._err_sent[k] = time.time()
        await self.send(f"⚠️ 模块 <b>{e(name)}</b> 出错，已自动重启：<code>{e(str(ex)[:300])}</code>")

    # ================================================================ 链接
    @staticmethod
    def tok_link(chain: str, addr: str, label: str) -> str:
        return f'<a href="{CHAINS[chain].explorer_token.format(addr)}">{e(label)}</a>'

    @staticmethod
    def addr_link(chain: str, addr: str) -> str:
        return f'<a href="{CHAINS[chain].explorer_addr.format(addr)}">{short(addr)}</a>'

    @staticmethod
    def tx_link(chain: str, tx: str) -> str:
        if not tx:
            return ""
        if tx == "DRYRUN":
            return "模拟"
        return f'<a href="{CHAINS[chain].explorer_tx.format(tx)}">查看交易</a>'

    def mode(self) -> str:
        return "🧪模拟" if self.app.cfg.dry_run else "💰实盘"

    # ================================================================ 消息模板
    def _report_lines(self, rep: dict) -> list[str]:
        c = rep["chain"]
        L = [
            f"<b>{self.tok_link(c, rep['address'], rep.get('symbol') or '?')}</b> {e(rep.get('name', '')[:30])} "
            f"| {CHAINS[c].name}",
            f"<code>{rep['address']}</code>",
            f"价格 {fmt_price(rep['price'])} | 市值 {fmt_usd(rep['mcap'])} | 池子 {fmt_usd(rep['liquidity'])}",
            f"持有人 {rep['holders']} | 1h量 {fmt_usd(rep['volume_1h'])} | 1h {rep['change_1h']:+.0f}% "
            f"| 上线 {rep['age_hours']:.1f}h",
            f"🎯 评分 <b>{rep['score']}</b> | 聪明钱 {len(rep.get('smart_buyers', []))} 个",
            f"📦 捆绑持有 {rep['bundle_held_pct']}%（开盘买入 {rep.get('bundle_bought_pct', 0)}%，"
            f"{len(rep.get('bundle_wallets', []))} 个地址）",
            f"🕵️ 埋伏持有 {rep['insider_held_pct']}%（{len(rep.get('insider_wallets', []))} 个）"
            f" | 狙击 {rep.get('sniper_held_pct', 0)}% | dev {rep.get('dev_held_pct', 0)}% | 前10 {rep['top10_pct']}%",
        ]
        if c != "solana":
            L.append(f"税 买{rep.get('buy_tax', 0):.1f}% 卖{rep.get('sell_tax', 0):.1f}%")
        if rep.get("risks"):
            L.append("⛔ " + e("；".join(rep["risks"])))
        if rep.get("warnings"):
            L.append("⚠️ " + e("；".join(rep["warnings"][:4])))
        for cl in rep.get("clusters", [])[:2]:
            tag = "（与dev关联）" if cl.get("dev_linked") else ""
            L.append(f"🔗 同一出资人 {self.addr_link(c, cl['funder'])} 控制 {len(cl['wallets'])} 个早期钱包{tag}")
        return L

    async def token_report(self, rep: dict, title: str = "📊 代币分析") -> None:
        lines = [f"<b>{e(title)}</b>"] + self._report_lines(rep)
        if rep.get("reasons"):
            lines.append("<i>" + e(" / ".join(rep["reasons"][:6])) + "</i>")
        await self.send("\n".join(lines))

    async def smart_trade(self, s: "Signal", w: dict, rep: dict | None) -> None:
        c = s.chain
        act = "🟢 买入" if s.side == "buy" else "🔴 卖出"
        sym = (rep or {}).get("symbol") or short(s.token)
        amt = f" {s.native_amount:.3f} {CHAINS[c].native_symbol}" if s.native_amount else ""
        head = (f"🧠 聪明钱{act}{amt} {self.tok_link(c, s.token, sym)}\n"
                f"钱包 {self.addr_link(c, s.wallet)} 评分{w['score']:.0f} 胜率{w['win_rate'] * 100:.0f}% "
                f"30天{fmt_usd(w['realized_usd'])} | {self.tx_link(c, s.tx)}")
        if rep and s.side == "buy":
            head += "\n" + "\n".join(self._report_lines(rep)[1:])
        await self.send(head)

    async def cluster(self, chain: str, token: str, wallets: list[str], rep: dict | None) -> None:
        sym = (rep or {}).get("symbol") or short(token)
        lines = [f"🔥🔥 <b>聪明钱共振</b>：{len(wallets)} 个聪明钱买入 {self.tok_link(chain, token, sym)}",
                 " ".join(self.addr_link(chain, w) for w in wallets[:8])]
        if rep:
            lines += self._report_lines(rep)[1:]
        await self.send("\n".join(lines))

    async def trade_buy(self, pid: int, chain: str, rep: dict, fill: "Fill", entry: float, cost_usd: float,
                        reason: str, rules: dict | None = None) -> None:
        plan = ""
        if rules:
            plan = f"卖出计划：止损 {float(rules.get('stop_loss_pct', -35)):.0f}%"
            if rules.get("stall_hours"):
                plan += f" | {float(rules['stall_hours']):g} 小时涨不到 +{float(rules.get('stall_min_gain_pct', 0)):g}% 就卖"
            plan += f" | 最长拿 {float(rules.get('max_hold_hours', 24)):g} 小时\n"
        await self.send(
            f"{self.mode()} ✅ <b>买入 #{pid}</b> {self.tok_link(chain, rep['address'], rep.get('symbol') or '?')}\n"
            f"原因：{e(reason)} | 评分 {rep['score']}\n"
            f"花费 {fill.native:.4f} {CHAINS[chain].native_symbol}（{fmt_usd(cost_usd)}）| 成本价 {fmt_price(entry)}\n"
            f"{plan}{self.tx_link(chain, fill.tx)}")

    async def trade_sell(self, p: dict, fill: "Fill", usd: float, reason: str, closed: bool, realized: float) -> None:
        c = p["chain"]
        tail = ""
        if closed:
            pnl = realized - float(p["cost_usd"] or 0)
            pct = pnl / float(p["cost_usd"]) * 100 if p["cost_usd"] else 0
            tail = f"\n{'🎉' if pnl >= 0 else '💸'} 已清仓，总盈亏 <b>{fmt_usd(pnl)}（{pct:+.1f}%）</b>"
        await self.send(
            f"{self.mode()} 🔴 <b>卖出 #{p['id']}</b> {self.tok_link(c, p['token'], p['symbol'] or '?')}\n"
            f"原因：{e(reason)}\n得到 {fill.native:.4f} {CHAINS[c].native_symbol}（{fmt_usd(usd)}）| "
            f"{self.tx_link(c, fill.tx)}{tail}")

    async def trade_failed(self, chain: str, rep: dict, side: str, fill: "Fill") -> None:
        if "PLAN_UPGRADE" in fill.error:   # 套餐问题同类提醒 30 分钟只发一次，避免刷屏
            if time.time() - self._err_sent.get("plan_upgrade", 0) < 1800:
                return
            self._err_sent["plan_upgrade"] = time.time()
            await self.send(f"{self.mode()} ❌ {side}失败 {e(rep.get('symbol') or '?')}：GMGN 提示当前套餐不能用 API 交易。\n"
                            f"GMGN 原话：{e(fill.error[:300])}\n机器人会每 30 分钟自动重试一次，期间同类提醒不再重复发送。")
            return
        extra = "\n❗ 交易状态未知，请到区块浏览器确认" if fill.unknown else ""
        await self.send(f"{self.mode()} ❌ {side}失败 {e(rep.get('symbol') or '?')}：{e(fill.error[:300])} "
                        f"{self.tx_link(chain, fill.tx)}{extra}")

    # ================================================================ 命令
    async def poller(self) -> None:
        offset = int(await self.app.db.kv_get("tg_offset", 0) or 0)
        while True:
            try:
                r = await self.app.http.get("tg_poll", f"{self.base}/getUpdates", timeout=40, retries=1,
                                            params={"timeout": 30, "offset": offset,
                                                    "allowed_updates": '["message"]'})
            except Exception as ex:  # noqa: BLE001
                log.debug("getUpdates: %s", ex)
                await asyncio.sleep(5)
                continue
            for u in (r or {}).get("result", []):
                offset = u["update_id"] + 1
                await self.app.db.kv_set("tg_offset", offset)
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != self.chat:
                    continue   # 只听主人的
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    # 放到后台执行，卖出/分析这类慢命令不会卡住后续命令
                    t = asyncio.create_task(self._safe_command(text))
                    self._cmd_tasks.add(t)
                    t.add_done_callback(self._cmd_tasks.discard)

    async def _safe_command(self, text: str) -> None:
        try:
            await self.command(text)
        except Exception as ex:  # noqa: BLE001
            log.exception("命令出错")
            await self.send(f"命令出错：{e(str(ex)[:200])}")

    async def command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        db = self.app.db
        if cmd in ("/start", "/help"):
            await self.send(HELP)
        elif cmd == "/status":
            await self.send(await self._status())
        elif cmd == "/positions":
            await self.send(await self._positions())
        elif cmd == "/size":
            await self._size(args)
        elif cmd == "/why":
            if not args or not args[0].isdigit():
                await self.send("格式：/why 持仓编号，例：/why 3（编号在 /positions 里看）")
                return
            from ..trade.review import why_text
            await self.send(await why_text(self.app, int(args[0])))
        elif cmd == "/whynot":
            await self._whynot(args)
        elif cmd == "/review":
            from ..trade.review import daily_review
            await self.send(await daily_review(self.app, self.app.day_start()))
        elif cmd == "/dynamic":
            await self._dynamic(args)
        elif cmd == "/pause":
            await db.kv_set("paused", True)
            await self.send("⏸ 已暂停买入（持仓止盈止损继续运行）")
        elif cmd == "/resume":
            await db.kv_set("paused", False)
            await db.kv_set("pause_until", 0)
            await db.kv_set("streak_pause_until", 0)
            await db.kv_set("streak_reset_at", time.time())
            await self.send("▶️ 已恢复买入")
        elif cmd == "/sell" and args:
            p = await db.fetchone("SELECT * FROM positions WHERE id=? AND status IN ('open','stuck')", (int(args[0]),))
            if not p:
                await self.send("没有这个持仓编号")
                return
            pct = float(args[1]) if len(args) > 1 else 100
            if p["status"] == "stuck":
                await db.execute("UPDATE positions SET status='open', sell_attempts=0 WHERE id=?", (p["id"],))
                p["status"] = "open"
            await self.send(f"正在卖出 #{p['id']} {pct:.0f}%……")
            await self.app.positions.sell(p, pct, "手动卖出")
        elif cmd == "/wallets":
            await self.send(await self._wallets(args[0].lower() if args else None))
        elif cmd in ("/addwallet", "/delwallet") and len(args) >= 2:
            chain, addr = args[0].lower(), args[1]
            if chain not in self.app.cfg.enabled_chains:
                await self.send("链名不对或未启用")
                return
            if cmd == "/addwallet":
                await self.app.smart.add_manual(chain, addr)
                await self.send(f"✅ 已添加 {e(addr)}，开始监听")
            else:
                await db.execute(
                    "INSERT INTO wallets(chain,address,status,source,created_at) VALUES(?,?,'blacklist','manual',?) "
                    "ON CONFLICT(chain,address) DO UPDATE SET status='blacklist'",
                    (chain, norm_addr(chain, addr), time.time()))
                self.app.wallets_changed[chain].set()
                await self.send(f"🚫 已拉黑 {e(addr)}")
        elif cmd == "/check" and len(args) >= 2:
            chain = args[0].lower()
            if chain not in self.app.cfg.enabled_chains:
                await self.send("链名不对或未启用")
                return
            await self.send("分析中，大约需要 10~30 秒……")
            rep = await self.app.analyzer.analyze(chain, args[1], source="manual", force=True)
            if rep:
                await self.token_report(rep, "📊 手动分析")
                await self._send_tagged(rep)
            else:
                await self.send("分析失败（接口无数据或预算用完），看日志了解详情")
        elif cmd == "/tag" and len(args) >= 2:
            chain = args[0].lower()
            rows = await db.tags_for(chain, norm_addr(chain, args[1]))
            if not rows:
                await self.send("这个地址暂无标记")
            else:
                names = {"bundle": "捆绑", "insider": "埋伏", "sniper": "狙击", "dev": "dev"}
                cnt: dict[str, int] = {}
                for r in rows:
                    cnt[r["tag"]] = cnt.get(r["tag"], 0) + 1
                await self.send("地址标记记录：" + "，".join(f"{names.get(k, k)} {v} 次" for k, v in cnt.items()))
        else:
            await self.send("看不懂这个命令。\n" + HELP)

    async def _whynot(self, args: list[str]) -> None:
        chains = self.app.cfg.enabled_chains
        chain = "solana" if "solana" in chains else chains[0]
        if args and args[0].lower() in chains:
            chain, args = args[0].lower(), args[1:]
        if not args:
            await self.send("格式：/whynot 代币地址，例：/whynot 6NeJ...Loat（完整地址）")
            return
        token = norm_addr(chain, args[0])
        await self.send("检查中，大约 10~30 秒……")
        rep = await self.app.analyzer.analyze(chain, token, source="whynot", force=True)
        if not rep:
            await self.send("分析失败（接口没数据），稍后再试")
            return
        lines = [f"<b>{e(rep.get('symbol') or '?')} 为什么没买</b>",
                 f"代币评分 {rep['score']}（强叙事要 ≥ 55、早期建仓要 ≥ 50）| 聪明钱 {len(rep.get('smart_buyers') or [])} 个",
                 "", "<b>主动建仓</b>"]
        strat = getattr(self.app, "strategies", None)
        lines += strat.explain(chain, token) if strat else ["主动建仓未启用"]
        lines += ["", "<b>风控过滤</b>（所有买入方式都要过）"] + self.app.trader.explain(rep)
        sk = await self.app.db.fetchone("SELECT reason, ts FROM skipped WHERE chain=? AND token=? ORDER BY id DESC LIMIT 1",
                                        (chain, token))
        if sk:
            lines.append(f"\n最近一次被拦：{e(sk['reason'])}（{self.app.fmt_time(sk['ts'])}）")
        await self.send("\n".join(lines))

    async def _dynamic(self, args: list[str]) -> None:
        from ..trade.review import MODE_TEXT, shadow_compare
        db = self.app.db
        names = {"on": "live", "live": "live", "shadow": "shadow", "off": "off"}
        if args:
            m = names.get(args[0].lower())
            if not m:
                await self.send("格式：/dynamic on（接管卖出）| /dynamic shadow（只观察）| /dynamic off（关闭）")
                return
            await db.kv_set("dyn_mode", m)
            if m == "shadow":
                await db.kv_set("dyn_since", time.time())
                await db.kv_set("dyn_reported", False)
        mode = await self.app.positions.mode()
        dc = self.app.positions.dcfg
        tip = {"shadow": "想让新规则接管，发 /dynamic on", "live": "想退回只观察，发 /dynamic shadow",
               "off": "想重新开启观察，发 /dynamic shadow"}[mode]
        await self.send(
            f"<b>动态管理：{MODE_TEXT[mode]}</b>{'（已切换）' if args else ''}\n"
            f"每 {int(dc['check_sec']) // 60} 分钟复查一次持仓，健康分低于 {dc['reduce_below']} 卖一半、"
            f"低于 {dc['exit_below']} 全卖（连续 {dc['confirm_checks']} 次才动手），高于 {dc['strong_above']} 放宽止盈；"
            f"涨过 +{dc['breakeven_after_pct']}% 后保本；撤池子/dev 清仓立刻卖\n\n"
            f"{await shadow_compare(self.app)}\n\n{tip}")

    async def _size(self, args: list[str]) -> None:
        chains = self.app.cfg.enabled_chains
        chain = "solana" if "solana" in chains else chains[0]
        if args and not args[0].replace(".", "", 1).isdigit():
            chain = args[0].lower()
            args = args[1:]
            if chain not in chains:
                await self.send("链名不对或未启用")
                return
        if args:
            try:
                vals = [float(x) for x in args]
            except ValueError:
                vals = []
            if len(vals) != 3 or any(v <= 0 for v in vals) or not (vals[0] <= vals[1] <= vals[2]):
                await self.send("格式：/size 一般 较好 优质，例如 /size 0.05 0.1 0.2（三个数从小到大）")
                return
            saved = await self.app.db.kv_get("sizes", {}) or {}
            saved[chain] = vals
            await self.app.db.kv_set("sizes", saved)
        sym = CHAINS[chain].native_symbol
        n, g, b = await self.app.trader.sizes(chain)
        t = self.app.cfg.get("trading.tiers", {}) or {}
        await self.send(
            f"<b>{CHAINS[chain].name} 每笔买入金额</b>{'（已更新）' if args else ''}\n"
            f"🔥 优质：{b:g} {sym}（评分 ≥ {t.get('best_min_score', 85)} 或 ≥ {t.get('best_min_smart', 3)} 个聪明钱，"
            f"且捆绑 &lt; {t.get('best_max_bundle_pct', 10)}%）\n"
            f"👍 较好：{g:g} {sym}（评分 ≥ {t.get('good_min_score', 70)} 或 ≥ {t.get('good_min_smart', 2)} 个聪明钱）\n"
            f"一般：{n:g} {sym}\n修改：/size {n:g} {g:g} {b:g}")

    async def _send_tagged(self, rep: dict) -> None:
        c = rep["chain"]
        lines = []
        for cls, name in (("bundle_wallets", "📦 捆绑地址"), ("insider_wallets", "🕵️ 埋伏地址")):
            ws = rep.get(cls) or []
            if not ws:
                continue
            lines.append(f"<b>{name}（{len(ws)}）</b>")
            for w in ws[:10]:
                lines.append(f"{self.addr_link(c, w['wallet'])} {e('；'.join(w['evidence'][:2]))}")
        if lines:
            await self.send("\n".join(lines))

    async def _status(self) -> str:
        db = self.app.db
        paused = await self.app.trader.is_paused()
        lines = [f"<b>运行状态</b> {self.mode()} {'⏸ ' + paused if paused else '▶️ 正常'}",
                 f"已运行 {(time.time() - self.app.started_at) / 3600:.1f} 小时"]
        for c in self.app.cfg.enabled_chains:
            n = await db.fetchone("SELECT COUNT(*) n FROM wallets WHERE chain=? AND status IN ('active','manual')", (c,))
            lines.append(f"{CHAINS[c].name}：监听 {n['n']} 个聪明钱 | 交易地址 {short(self.app.trader.address(c))}")
        pos = await db.open_positions()
        pnl = await db.realized_today_usd(self.app.day_start())
        lines.append(f"持仓 {len(pos)} 个 | 今日已实现 {fmt_usd(pnl)}")
        r = await db.fetchone("SELECT COUNT(*) n FROM signals WHERE ts>?", (time.time() - 3600,))
        lines.append(f"近1小时信号 {r['n']} 条")
        sk = await db.fetchone("SELECT COUNT(*) n FROM skipped WHERE ts>?", (time.time() - 3600,))
        lines.append(f"近1小时被风控拦下 {sk['n']} 个币")
        strat = getattr(self.app, "strategies", None)
        if strat:
            for c in self.app.cfg.enabled_chains:
                lines.append(strat.status_text(c))
        usage = self.app.data.usage_text()
        if usage:
            lines.append(usage)
        return "\n".join(lines)

    async def _positions(self) -> str:
        pos = await self.app.db.fetchall("SELECT * FROM positions WHERE status IN ('open','stuck') ORDER BY id")
        if not pos:
            return "当前没有持仓"
        lines = ["<b>当前持仓</b>"]
        by_chain: dict[str, list[dict]] = {}
        for p in pos:
            by_chain.setdefault(p["chain"], []).append(p)
        for c, lst in by_chain.items():
            prices = await self.app.data.prices(c, [p["token"] for p in lst])
            for p in lst:
                px = prices.get(p["token"]) or prices.get(p["token"].lower()) or 0
                g = (px / p["entry_price"] - 1) * 100 if px and p["entry_price"] else 0
                flag = " ⚠️无法卖出" if p["status"] == "stuck" else ""
                tier = {"best": "🔥", "good": "👍"}.get(p.get("tier") or "", "")
                held = (time.time() - float(p["opened_at"] or time.time())) / 3600
                hp = f" 健康{p['health']}" if p.get("health") is not None else ""
                lines.append(f"#{p['id']} {tier}{self.tok_link(c, p['token'], p['symbol'] or '?')} {CHAINS[c].name} "
                             f"成本{fmt_usd(p['cost_usd'])} 现{g:+.1f}% 已拿{held:.1f}h{hp}{flag}")
        lines.append("发 /why 编号 查看详情")
        return "\n".join(lines)

    async def _wallets(self, chain: Any) -> str:
        chains = [chain] if chain in self.app.cfg.enabled_chains else self.app.cfg.enabled_chains
        lines = []
        for c in chains:
            rows = await self.app.db.fetchall(
                "SELECT * FROM wallets WHERE chain=? AND status IN ('active','manual') ORDER BY score DESC LIMIT 15", (c,))
            lines.append(f"<b>{CHAINS[c].name} 前 {len(rows)} 名</b>")
            for r in rows:
                lines.append(f"{self.addr_link(c, r['address'])} 分{r['score']:.0f} 胜率{r['win_rate'] * 100:.0f}% "
                             f"{fmt_usd(r['realized_usd'])} 跟单{r['follow_wins']}胜{r['follow_losses']}负")
        return "\n".join(lines) or "暂无"
