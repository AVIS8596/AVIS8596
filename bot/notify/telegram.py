"""Telegram 推送 + 命令控制。只响应你自己的 chat id，其他人发命令一律忽略。"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING, Any

from ..chains import CHAINS, norm_addr
from ..http import ApiError
from ..utils import f, fmt_price, fmt_usd, short

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
/audit 钱包地址 钱包体检：是不是项目方的号 / 老鼠仓 / 狙击机器人（跟单前机器人会自动体检）
/me 你自己手动的实盘：画像、每笔期望、你买过机器人没买的币卡在哪，成交也算进实盘成本；/me add 链 地址 加别的钱包
/golive 上实盘标准：6 项考核的进度、达标后的加钱计划；实盘期间对比实盘 vs 模拟盘
/costs 实盘真实成本（滑点、失败率），模拟盘按它扣成本
/diamond 钻石手打法：学靠一个币拿得住、赚几万 U 的人（他们什么时候买什么样的币、拿多久），模拟盘按学到的打法下单对比；/diamond go 立即找一轮
/imitate 学来的打法：机器人从 GMGN 盈利地址的交易里自己学的买卖方式，学到了什么、自己的成绩、和其他来源 / 现在的卖出规则对比；/imitate train 立即重新学
/autopsy [sol|bsc] [人数] 高收益地址解剖：GMGN 赚钱的地址，跟着它买的人能分到多少（晚 30/60/120 秒）、哪类钱包和单子跟得上（后台跑 1~2 小时，跑完自动发）；不带参数看上次结果
/lag [天数] 延迟和实盘成本实测：比带单的聪明钱晚多少秒、这段时间涨了多少、晚到的单子是不是更亏；换成实盘小金额（Solana 0.05 SOL）后固定费用吃掉多少、模拟成绩还剩多少
/backup 立即把数据备份发到这里（每天也会自动发）；/restore 用备份恢复
/model 自己的选币模型：训练成绩、影子期选了哪些币、表现如何；/model train 立即训练
/tune 自我学习：学到了哪些参数、样本多少；/tune now 立即学一次；/tune reset 全部恢复
/history 代币地址 这个币的时间线：发币后多久被看到、当时涨了几倍、被哪条规则拦过、什么时候买卖
/learn 学习中的高手列表 + 共同规律 + 参数建议
/learn 钱包地址 [备注] 学习这个人（生成交易画像）
/learn auto 立即自动找一轮高手 | /learn del 地址 不再学习
/learn token 代币地址 拆解这个币的盈利前 20 名（什么时候、什么市值买的，是不是内部人）
/learn data [15|60|240] 从结果档案里学习：各项特征分档后，之后 15 分钟/1 小时/4 小时的涨跌
/learn sample 影子采样：通过防收割检查的币，按真实卖出规则模拟到底，各项特征分档后每笔赚多少
/learn early 早期赢家 vs 早期骗局：之后涨了 3 倍的币和归零 / 撤池子的币，刚被发现时的数据有什么不同
/dynamic 动态管理状态和新旧规则对比
/dynamic on | shadow | off 接管卖出 / 只观察 / 关闭
/review 立即看今日复盘
/problems 体检：现在有哪些问题（信号源断线、节点额度、GMGN 限流、长时间没买入、连续亏损、硬盘、日志报错）。这些问题出现时机器人也会主动提醒
/diag [天数] 自检：按链拆开每天的买入和盈亏、模拟买入失败的次数和原因、成绩靠不靠运气、止损情况、自我学习改过的参数（默认 4 天）
/funnel [天数] 漏斗：每天进来多少信号、被拦在哪、买了多少、赚没赚（回答"为什么买得少 / 条件是不是太严"，默认 4 天）
/sell 编号 [百分比] 手动卖出，例：/sell 3 50
/alerts off | on 关闭 / 打开聪明钱和新币提醒（交易、风控、复盘照常推送）
/backtest [天数] 回放回测：用真实入场按 8 套卖出规则重新模拟，看哪套最赚（默认最近 3 天）
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
链名：solana / bsc / ethereum / base / robinhood（可简写 sol / eth / hood）
/whynot、/learn token 也可以在地址前加链名，例：/whynot hood 0x...；0x 地址不写链名会自动识别"""


CHAIN_ALIAS = {"sol": "solana", "eth": "ethereum", "bnb": "bsc", "hood": "robinhood", "rh": "robinhood"}

# 在 Telegram 输入框点 "/" 或左下角菜单按钮就能看到、点选的命令（按常用程度排序）
MENU = [
    ("help", "全部命令和用法"), ("status", "运行状态"), ("positions", "当前持仓"), ("review", "今日复盘"),
    ("funnel", "[天数]：每天信号 → 被拦 → 买入的漏斗"),
    ("diag", "[天数]：自检（按链盈亏、买入失败、止损、参数变化）"),
    ("problems", "体检：现在有哪些问题"),
    ("history", "代币地址：这个币的时间线（为什么买 / 没买）"), ("whynot", "代币地址：这个币为什么没买"),
    ("why", "编号：持仓的健康分和卖出规则"), ("sell", "编号 [百分比]：手动卖出"),
    ("audit", "钱包地址：钱包体检（是不是项目方的号 / 老鼠仓）"), ("wallets", "[链]：监听的聪明钱"),
    ("model", "自己的选币模型：成绩和影子期表现"), ("tune", "自我学习：学到了哪些参数"),
    ("golive", "上实盘标准的进度"), ("backtest", "[天数]：回放回测卖出规则"), ("costs", "实盘真实成本"), ("lag", "[天数]：比聪明钱晚多少、换成实盘小金额还剩多少"),
    ("imitate", "学来的打法：学到了什么、成绩对比"), ("diamond", "钻石手打法：学拿得住的大赢家"),
    ("autopsy", "[sol/bsc] [人数]：高收益地址解剖，跟着买能分到多少"),
    ("me", "你自己手动实盘的画像和对照"), ("learn", "向高手学习 / 结果档案学习"),
    ("size", "查看 / 修改每笔买入金额"), ("pause", "暂停买入（止盈止损照常）"), ("resume", "恢复买入"),
    ("alerts", "off / on：关闭 / 打开聪明钱和新币提醒"), ("dynamic", "动态持仓管理"),
    ("check", "链 代币地址：立即分析一个币"), ("tag", "链 地址：这个地址被标记过什么"),
    ("addwallet", "链 地址：手动添加聪明钱"), ("delwallet", "链 地址：拉黑地址"),
    ("backup", "立即备份数据到这里"), ("restore", "用备份恢复数据"),
]


class Telegram:
    def __init__(self, app: "App"):
        self.app = app
        self.token = app.cfg.secrets.telegram_token
        self.chat = str(app.cfg.secrets.telegram_chat_id)
        self.base = f"https://api.telegram.org/bot{self.token}"
        # 优先级队列：交易、风控、命令回复（0）永远排在聪明钱/新币提醒（1）前面
        self.q: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=500)
        self._seq = 0
        self._low_pending = 0
        self.muted = False   # /alerts off 后只推交易和重要消息
        self._err_sent: dict[str, float] = {}
        self._cmd_tasks: set[asyncio.Task] = set()

    # ================================================================ 发送
    async def send(self, text: str, low: bool = False) -> None:
        """low=True 是聪明钱 / 新币这类提醒：静音时不发，积压超过 30 条时丢弃，保证交易消息不被堵住。"""
        if len(text) > 4000:
            text = text[:3990] + "…"
        if low and (self.muted or self._low_pending >= 30):
            return
        self._seq += 1
        try:
            self.q.put_nowait((1 if low else 0, self._seq, text))
            self._low_pending += int(low)
        except asyncio.QueueFull:
            log.warning("TG 消息队列已满，丢弃一条")

    async def sender(self) -> None:
        """串行发送，每条间隔 1.1 秒，避免触发 Telegram 限流。"""
        while True:
            prio, _, text = await self.q.get()
            if prio:
                self._low_pending = max(0, self._low_pending - 1)
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
        marks = [f"{n} {rep.get(k + '_n')} 个持 {rep.get(k + '_pct')}%" for k, n in
                 (("cabal", "🕵️阴谋地址"), ("early_finder", "🔭早期发现者"), ("winner", "🎯精准钱包"))
                 if rep.get(k + "_n")]
        if marks:
            L.append("｜".join(marks))
        if c != "solana":
            L.append(f"税 买{rep.get('buy_tax', 0):.1f}% 卖{rep.get('sell_tax', 0):.1f}%")
        else:
            from ..trade.trader import pullable_pool
            lp = rep.get("lp_burn")
            pool = e(rep.get("exchange") or "?")
            L.append(f"🔥 池子已烧（{pool}）" if lp == "burn" else
                     (f"⚠️ {e(pullable_pool(rep))}" if pullable_pool(rep) else
                      (f"池子没烧（{pool}，还在发射平台曲线上 / 交易所不明）" if lp == "" else "池子烧没烧：不知道（GMGN 没返回）")))
        if rep.get("risks"):
            L.append("⛔ " + e("；".join(rep["risks"])))
        if rep.get("warnings"):
            L.append("⚠️ " + e("；".join(rep["warnings"][:4])))
        for cl in rep.get("clusters", [])[:2]:
            tag = "（与dev关联）" if cl.get("dev_linked") else ""
            L.append(f"🔗 同一出资人 {self.addr_link(c, cl['funder'])} 控制 {len(cl['wallets'])} 个早期钱包{tag}")
        return L

    async def token_report(self, rep: dict, title: str = "📊 代币分析", low: bool = False) -> None:
        lines = [f"<b>{e(title)}</b>"] + self._report_lines(rep)
        if rep.get("reasons"):
            lines.append("<i>" + e(" / ".join(rep["reasons"][:6])) + "</i>")
        await self.send("\n".join(lines), low=low)

    async def smart_trade(self, s: "Signal", w: dict, rep: dict | None) -> None:
        c = s.chain
        act = "🟢 买入" if s.side == "buy" else "🔴 卖出"
        sym = (rep or {}).get("symbol") or short(s.token)
        amt = f" {s.native_amount:.3f} {CHAINS[c].native_symbol}" if s.native_amount else ""
        from ..discovery.expand import KIND_NAME
        kind = KIND_NAME.get(w.get("kind") or "smart", "🧠聪明钱")
        head = (f"{kind}{act}{amt} {self.tok_link(c, s.token, sym)}\n"
                f"钱包 {self.addr_link(c, s.wallet)} 评分{w['score']:.0f} 胜率{w['win_rate'] * 100:.0f}% "
                f"30天{fmt_usd(w['realized_usd'])} | {self.tx_link(c, s.tx)}")
        if rep and s.side == "buy":
            head += "\n" + "\n".join(self._report_lines(rep)[1:])
        await self.send(head, low=True)

    async def cluster(self, chain: str, token: str, wallets: list[str], rep: dict | None) -> None:
        sym = (rep or {}).get("symbol") or short(token)
        lines = [f"🔥🔥 <b>聪明钱共振</b>：{len(wallets)} 个聪明钱买入 {self.tok_link(chain, token, sym)}",
                 " ".join(self.addr_link(chain, w) for w in wallets[:8])]
        if rep:
            lines += self._report_lines(rep)[1:]
        await self.send("\n".join(lines), low=True)

    async def trade_buy(self, pid: int, chain: str, rep: dict, fill: "Fill", entry: float, cost_usd: float,
                        reason: str, rules: dict | None = None) -> None:
        plan = ""
        if rules:
            plan = f"卖出计划：止损 {float(rules.get('stop_loss_pct', -35)):.0f}%"
            if rules.get("stall_hours"):
                plan += f" | {float(rules['stall_hours']):g} 小时涨不到 +{float(rules.get('stall_min_gain_pct', 0)):g}% 就卖"
            plan += f" | 最长拿 {float(rules.get('max_hold_hours', 24)):g} 小时\n"
            tps = rules.get("take_profit") or []
            if tps:
                rn = rules.get("runner") or {}
                t0 = tps[0]
                if rn.get("enabled") and rules.get("recover_cost_only", True) and float(t0["gain_pct"]) >= 100:
                    # 强势长拿开着时，出本之后的固定止盈档位不会用到，不再列出来免得误会
                    plan += (f"止盈：涨到 +{float(t0['gain_pct']):g}% 只卖刚好回本的部分（翻倍卖约一半，"
                             f"涨得越高卖得越少：4 倍约 1/4、5 倍约 1/5）")
                else:
                    plan += "止盈：" + "，".join(f"+{float(t['gain_pct']):g}% 卖{float(t['sell_pct']):g}%" for t in tps)
                if rn.get("enabled"):
                    lad = rn.get("ladder") or []
                    plan += (" | 翻倍出本后强势长拿：从最高点回撤 "
                             + "/".join(f"{float(x['drop_pct']):g}%" for x in lad) + " 分批卖，"
                             f"回落到 +{float(rn.get('floor_gain_pct', 20)):g}% 全卖")
                ts = float(rules.get("trailing_start_pct", 60))
                plan += (f" | 涨过 +{ts:g}% 后回撤 {float(rules.get('trailing_drop_pct', 30)):g}% 先卖一半\n"
                         if ts < 1000 else "\n")
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
    async def set_menu(self) -> None:
        try:
            await self.app.http.post("tg", f"{self.base}/setMyCommands", retries=1, json_body={
                "commands": [{"command": c, "description": d} for c, d in MENU]})
        except Exception as ex:  # noqa: BLE001
            log.debug("setMyCommands: %s", ex)

    async def poller(self) -> None:
        await self.set_menu()          # 输入框点 "/" 就能看到全部命令
        self.muted = bool(await self.app.db.kv_get("alerts_muted", False))
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
                if msg.get("document") and str(msg.get("caption") or "").strip().startswith("/restore"):
                    t = asyncio.create_task(self._restore_doc(msg))      # 发备份文件 + 说明 /restore = 恢复
                    self._cmd_tasks.add(t)
                    t.add_done_callback(self._cmd_tasks.discard)
                    continue
                if text.startswith("/"):
                    # 放到后台执行，卖出/分析这类慢命令不会卡住后续命令
                    t = asyncio.create_task(self._safe_command(text))
                    self._cmd_tasks.add(t)
                    t.add_done_callback(self._cmd_tasks.discard)

    async def _restore_doc(self, msg: dict) -> None:
        from ..safety import restore_receive
        try:
            await self.send(await restore_receive(self.app, msg))
        except Exception as ex:  # noqa: BLE001
            log.exception("接收备份出错")
            await self.send(f"接收备份出错：{e(str(ex)[:200])}")

    async def _safe_command(self, text: str) -> None:
        try:
            await self.command(text)
        except Exception as ex:  # noqa: BLE001
            log.exception("命令出错")
            await self.send(f"命令出错：{e(str(ex)[:200])}")

    async def command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = [CHAIN_ALIAS.get(a.lower(), a) for a in parts[1:]]
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
        elif cmd == "/learn":
            await self._learn(args)
        elif cmd == "/backtest":
            from ..trade.replay import run as replay
            days = float(args[0]) if args and args[0].replace(".", "").isdigit() else 3
            await self.send(f"回测中：拉最近 {days:g} 天每笔入场后的 K 线并模拟 8 套卖出规则，大约 1~3 分钟……")
            await self.send(await replay(self.app, days))
        elif cmd == "/alerts":
            if args and args[0].lower() in ("off", "on"):
                self.muted = args[0].lower() == "off"
                await self.app.db.kv_set("alerts_muted", self.muted)
            await self.send("🔕 已关闭聪明钱/新币提醒，只推交易、风控和复盘" if self.muted
                            else "🔔 聪明钱/新币提醒已打开（发 /alerts off 关闭）")
        elif cmd == "/whynot":
            await self._whynot(args)
        elif cmd == "/audit":
            chain, rest = await self._pick_chain(args)
            if not rest:
                await self.send("格式：/audit 钱包地址（可在地址前加链名）")
                return
            from ..discovery.audit import audit, text as audit_text
            w = norm_addr(chain, rest[0])
            await self.send("正在体检：拉它最近的交易、看它赚钱的币现在怎么样……")
            await self.send(audit_text(chain, w, await audit(self.app, chain, w, force=True)))
        elif cmd == "/me":
            from ..owner import report as me_report
            sub = args[0].lower() if args else ""
            if sub == "add" and len(args) >= 3 and args[1].lower() in self.app.cfg.enabled_chains:
                from ..owner import owner_wallets
                ws = await owner_wallets(self.app)
                c = args[1].lower()
                ws.append((c, norm_addr(c, args[2])))
                await self.app.db.kv_set("owner_wallets", [list(x) for x in dict.fromkeys(ws)])
                await self.send(f"已加入你的钱包：{c} {e(args[2][:8])}…，发 /me 查看")
            elif sub == "reset":
                await self.app.db.kv_set("owner_wallets", None)
                await self.send("已恢复为默认（GMGN 绑定的 Solana 钱包 + EVM 钱包在罗宾汉链）")
            else:
                await self.send("正在拉你的交易记录、逐笔对 K 线算成本，大概要 1 分钟……")
                await self.send(await me_report(self.app))
        elif cmd == "/golive":
            from ..golive import text as golive_text
            await self.send(await golive_text(self.app))
        elif cmd == "/diamond":
            dm = getattr(self.app, "diamond", None)
            if not dm:
                await self.send("GMGN 没启用，学不了")
                return
            if args and args[0].lower() == "go":
                if dm.start():
                    await self.send("💎 开始找钻石手：最近涨得最多的币，每个看盈利前 50 名（后台慢慢跑，大约 20~40 分钟，跑完自动发）")
                else:
                    await self.send("💎 正在找，跑完会自动发")
                return
            await self.send(await dm.text())
        elif cmd == "/imitate":
            im = getattr(self.app, "imitate", None)
            if not im:
                await self.send("GMGN 没启用，学不了")
                return
            if args and args[0].lower() == "train":
                await self.send("开始重新学（用最近 14 天解剖存下的交易）……")
                await self.send(e("\n".join(await im.train()) or "没有数据：先发 /autopsy go 收集"))
            await self.send(await im.text())
        elif cmd == "/autopsy":
            ap = getattr(self.app, "autopsy", None)
            if not ap:
                await self.send("GMGN 没启用，做不了解剖")
                return
            alias = {"sol": "solana", "solana": "solana", "bsc": "bsc", "bnb": "bsc"}
            chains = [alias[x.lower()] for x in args if x.lower() in alias]
            top = next((int(x) for x in args if x.isdigit()), 0)
            if not chains and not top:
                if ap.task and not ap.task.done():
                    await self.send(f"🔬 解剖还在跑：{ap.progress or '准备中'}，跑完会自动发结果")
                    return
                last = await ap.last()
                if last:
                    for sec in last:
                        await self.send(sec)
                    await self.send("重新跑一次：/autopsy go（或 /autopsy sol 20 只看 Solana、每类 20 个钱包）")
                    return
            chains = [c for c in (chains or ["solana", "bsc"]) if c in self.app.cfg.enabled_chains]
            top = min(max(top or 20, 5), 40)
            if not chains:
                await self.send("这条链没启用")
                return
            if not ap.start(chains, top):
                await self.send(f"🔬 解剖已经在跑：{ap.progress or '准备中'}")
                return
            await self.send(f"🔬 开始解剖：{'、'.join(CHAINS[c].name for c in chains)}，每条链 GMGN 高收益地址前 {top} 个"
                            f" + 我们监听排名前 {top} 个。后台慢慢跑（不挤占机器人额度），大约 1~2 小时，跑完自动发结果")
        elif cmd == "/lag":
            from ..trade.latency import lag_text
            days = int(args[0]) if args and args[0].isdigit() else 7
            await self.send("正在查 K 线算延迟，大概要半分钟……")
            await self.send(await lag_text(self.app, min(max(days, 1), 14)))
        elif cmd == "/costs":
            from ..trade.costs import calibrate, text as cost_text
            self.app.live_costs = await calibrate(self.app)
            await self.send(cost_text(self.app.live_costs))
        elif cmd == "/backup":
            from ..safety import backup_to_tg
            await self.send("正在打包备份……")
            err = await backup_to_tg(self.app)
            if err:
                await self.send(err)
        elif cmd == "/restore":
            from ..safety import restore_confirm
            if args and args[0].lower() == "confirm":
                await self.send(await restore_confirm(self.app))
            else:
                await self.send("恢复数据：把机器人每天发的备份文件（memebot-日期-slim.db.gz）发给我，说明文字写 /restore；"
                                "我检查没问题后，再发 /restore confirm")
        elif cmd == "/model":
            md = getattr(self.app, "model", None)
            if not md:
                await self.send("选币模型未启用")
                return
            sub = args[0].lower() if args else ""
            if sub in ("shadow", "on", "off"):
                await self.app.db.kv_set("model_mode", sub)
                await self.app.db.kv_set("model_manual", sub)     # 手动选的：不再自动上岗（自动退回影子照常）
                note = "（实盘不会自动下单）" if sub == "on" and not self.app.cfg.dry_run else ""
                await self.send(f"选币模型已改为：{ {'shadow': '影子模式（只记录不下单）', 'on': '模拟盘自己下单', 'off': '关闭'}[sub] }{note}")
            elif sub == "train":
                await self.send("开始训练（用最近 10 天的结果档案和 7 天的早期快照），大概要几十秒……")
                await self.send(e("\n".join(await md.train())))
            else:
                await self.send(await md.status_text())
        elif cmd == "/tune":
            br = getattr(self.app, "brain", None)
            if not br:
                await self.send("自我学习未启用")
                return
            sub = args[0].lower() if args else ""
            if sub in ("auto", "suggest", "off"):
                await self.app.db.kv_set("tune_mode", sub)
                await self.send(f"自我调参已改为：{ {'auto': '自动（模拟盘自动生效，实盘只建议）', 'suggest': '只建议', 'off': '关闭'}[sub] }")
            elif sub == "now":
                await self.send("开始学习：用最近 4 天的结果档案和早期观察样本，逐个参数试放宽 / 收紧一档……")
                msgs = await br.tune_once(force=True)
                await self.send("\n".join(msgs) or await br.status_text())   # 没有新建议：列出还在等确认的
            elif sub == "apply":
                ps = await br.pending()
                if not ps:
                    await self.send("没有待确认的建议")
                    return
                idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
                pend = next((p for p in ps if int(p.get("id") or 0) == idx), None)
                if not pend:                      # 必须带编号：免得同意错了哪一条
                    await self.send("请带上建议的编号，比如 /tune apply " + str(ps[0]["id"]) + "：\n"
                                    + "\n".join(f"{p['id']}. {e(p['why'])}" for p in ps))
                    return
                await br.apply(pend["key"], pend["new"], pend["why"])
                await self.app.db.kv_set("tune_pending", [p for p in ps if p is not pend] or None)
                await self.send("✅ 已采纳：" + e(pend["why"]))
            elif sub == "no":
                idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
                hit = await br.reject(idx)
                if not hit:
                    ps = await br.pending()
                    await self.send("没有这个编号的建议。" + ("待确认的：\n" + "\n".join(f"{p['id']}. {e(p['why'])}" for p in ps)
                                                       if ps else "现在没有待确认的建议"))
                    return
                await self.send("🚫 已否决，7 天内不再提：" + e(hit["why"]))
            elif sub == "reset":
                n = await br.reset()
                await self.send(f"已恢复 {n} 个参数为配置文件里的值")
            else:
                await self.send(await br.status_text())
        elif cmd in ("/history", "/timeline"):
            chain, rest = await self._pick_chain(args)
            if not rest:
                await self.send("格式：/history 代币地址（可在地址前加链名）")
                return
            from ..trade.history import history
            await self.send(await history(self.app, chain, norm_addr(chain, rest[0])))
        elif cmd == "/review":
            from ..trade.review import daily_review
            await self.send(await daily_review(self.app, self.app.day_start()))
        elif cmd == "/problems":
            dog = getattr(self.app, "watchdog", None)
            await self.send(await dog.report() if dog else "体检没有启用")
        elif cmd == "/diag":
            from ..trade.diag import diag_text
            days = int(args[0]) if args and args[0].isdigit() else 4
            await self.send(await diag_text(self.app, min(max(days, 1), 10)))
        elif cmd == "/funnel":
            from ..trade.review import funnel_text
            alias = {"sol": "solana", "solana": "solana", "bsc": "bsc", "bnb": "bsc", "eth": "ethereum", "base": "base"}
            chain = next((alias[x.lower()] for x in args if x.lower() in alias), "")
            days = next((int(x) for x in args if x.isdigit()), 4)
            await self.send(await funnel_text(self.app, min(max(days, 1), 7), chain))
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

    async def _learn(self, args: list[str]) -> None:
        ln = getattr(self.app, "learner", None)
        if not ln:
            await self.send("学习功能需要 GMGN 接口")
            return
        if not args:
            await self.send(await ln.summary())
        elif args[0].lower() == "auto":
            await self.send("开始自动找高手：收集 KOL / 聪明钱钱包 → 按 30 天利润取前 20 → 逐个生成画像，大约需要几分钟……")
            n = await ln.auto("solana")
            await self.send(f"自动学习完成，新增/更新 {n} 个画像。发 /learn 查看汇总")
        elif args[0].lower() == "sample":
            from ..trade.sampler import sample_report
            await self.send(await sample_report(self.app))
        elif args[0].lower() == "early":
            from ..trade.early_report import early_report
            await self.send(await early_report(self.app))
        elif args[0].lower() == "data":
            from ..trade.insight import feature_report
            h = {"15": "p15", "60": "p60", "240": "p240"}.get(args[1] if len(args) > 1 else "60", "p60")
            await self.send(await feature_report(self.app, h))
        elif args[0].lower() == "token" and len(args) > 1:
            await self.send("拆解中：拉盈利前 20 名 → 逐个看入场时间/市值/标签 → 给能学的人生成画像，大约 1~2 分钟……")
            chain, rest = await self._pick_chain(args[1:])
            if not rest:
                await self.send("格式：/learn token [链] 代币地址")
                return
            await self.send(await ln.learn_token(chain, norm_addr(chain, rest[0])))
        elif args[0].lower() == "del" and len(args) > 1:
            await self.app.db.execute("DELETE FROM learn_wallets WHERE address=?", (args[1],))
            await self.send("已删除")
        else:
            await self.send("分析中，大约需要 20~40 秒……")
            await self.send(await ln.learn("solana", args[0], " ".join(args[1:])))

    async def _pick_chain(self, args: list[str]) -> tuple[str, list[str]]:
        """参数里写了链名就用它；0x 地址没写链名时，按启用的 EVM 链逐个查，哪条链上有这个币就是哪条。"""
        chains = self.app.cfg.enabled_chains
        if args and args[0].lower() in chains:
            return args[0].lower(), args[1:]
        if args and args[0].lower().startswith("0x"):
            evm = self.app.cfg.evm_chains
            for c in evm:
                try:
                    if f((await self.app.data.overview(c, args[0].lower())).get("price")) > 0:
                        return c, args
                except Exception:  # noqa: BLE001
                    continue
            if evm:
                return evm[0], args
        return ("solana" if "solana" in chains else chains[0]), args

    async def _whynot(self, args: list[str]) -> None:
        chain, args = await self._pick_chain(args)
        if not args:
            await self.send("格式：/whynot 代币地址，例：/whynot 6NeJ...Loat（完整地址）")
            return
        token = norm_addr(chain, args[0])
        await self.send("检查中，大约 10~30 秒……")
        rep = await self.app.analyzer.analyze(chain, token, source="whynot", force=True)
        if not rep:
            await self.send("分析失败（接口没数据），稍后再试")
            return
        nsmart = len(rep.get("smart_buyers") or [])
        lines = [f"<b>{e(rep.get('symbol') or '?')} 为什么没买</b>",
                 f"代币评分 {rep['score']}（强叙事要 ≥ 55、早期建仓要 ≥ 50）| 聪明钱 {nsmart} 个",
                 "要买，得先有一个<b>买入理由</b>（跟单 / 新币高评分 / 主动建仓），再过下面的风控；风控全过但没有买入理由，也不会买。"]
        # 新币高评分 / 实验仓（只对新币列表里新出现的币）
        smin, cmin = float(self.app.cfg.get("trading.auto_min_score", 75)), int(self.app.cfg.get("trading.auto_min_cluster", 2))
        exp = self.app.cfg.get("trading.experiment", {}) or {}
        if nsmart < cmin:
            lines.append(f"\n<b>新币高评分</b>\n❌ 聪明钱 {nsmart} 个（要 ≥ {cmin}）")
        elif rep["score"] >= smin:
            lines.append(f"\n<b>新币高评分</b>\n✅ 评分 {rep['score']}（要 ≥ {smin:g}）——只对刚出现在新币列表里的币，错过第一次分析就不会再走这条")
        elif exp.get("enabled") and rep["score"] >= float(exp.get("min_score", 60)):
            lines.append(f"\n<b>新币高评分</b>\n🧪 评分 {rep['score']}（正常要 ≥ {smin:g}），够实验仓（≥ {exp.get('min_score', 60)}，¼ 仓位）"
                         "——只对刚出现在新币列表里的币")
        else:
            lines.append(f"\n<b>新币高评分</b>\n❌ 评分 {rep['score']}（要 ≥ {smin:g}）")
        lines += ["", "<b>主动建仓</b>"]
        strat = getattr(self.app, "strategies", None)
        lines += strat.explain(chain, token) if strat else ["主动建仓未启用"]
        age = f(rep.get("age_hours"))
        if 0 < age <= 24 and f(rep.get("price")) > 0:
            base = await self.app.trader.launch_price(chain, token, time.time() - age * 3600)
            if base > 0:
                rep["launch_x"] = f(rep["price"]) / base
        lines += ["", "<b>风控过滤</b>（所有买入方式都要过）"] + self.app.trader.explain(rep)
        sk = await self.app.db.fetchone("SELECT reason, ts FROM skipped WHERE chain=? AND token=? ORDER BY id DESC LIMIT 1",
                                        (chain, token))
        if sk:
            lines.append(f"\n<b>跟单</b> / 最近一次被拦：{e(sk['reason'])}（{self.app.fmt_time(sk['ts'])}）")
        else:
            lines.append("\n<b>跟单</b>：没有聪明钱的跟单信号触发过")
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
            key = self.app.trader.sizes_key
            saved = await self.app.db.kv_get(key, {}) or {}
            saved[chain] = vals
            await self.app.db.kv_set(key, saved)
        sym = CHAINS[chain].native_symbol
        n, g, b = await self.app.trader.sizes(chain)
        t = self.app.cfg.get("trading.tiers", {}) or {}
        await self.send(
            f"<b>{CHAINS[chain].name} 每笔买入金额（{'🧪 模拟盘' if self.app.cfg.dry_run else '💰 实盘'}）</b>"
            f"{'（已更新）' if args else ''}\n"
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
            lines.append(f"{CHAINS[c].name}：聪明钱库 {n['n']} 个 | 交易地址 {short(self.app.trader.address(c))}")
        pos = await db.open_positions()
        pnl = await db.realized_today_usd(self.app.day_start(), self.app.cfg.dry_run)
        lines.append(f"持仓 {len(pos)} 个 | 今日已实现 {fmt_usd(pnl)}")
        r = await db.fetchone("SELECT COUNT(*) n FROM signals WHERE ts>?", (time.time() - 3600,))
        by = await db.fetchall("SELECT chain, COUNT(*) n FROM signals WHERE ts>? GROUP BY chain ORDER BY n DESC",
                               (time.time() - 3600,))
        lines.append(f"近1小时信号 {r['n']} 条" + ("（" + "、".join(f"{CHAINS[x['chain']].name} {x['n']}" for x in by
                                                              if x["chain"] in CHAINS) + "）" if by else ""))
        sm = getattr(self.app, "solmon", None)
        if sm:
            lines.append(sm.status_text())
            for ep in getattr(self.app.sol, "eps", []):      # 每个 Solana 节点通不通（额度用完会写明）
                lines.append(f"Solana 节点 {e(ep.name)}：{e(await self.app.sol.probe(ep))}")
        for m in (getattr(self.app, "evmmons", {}) or {}).values():   # BSC 等 EVM 链的链上监听
            try:
                lines.append(e(m.status_text()))
            except Exception:  # noqa: BLE001
                pass
        dog = getattr(self.app, "watchdog", None)
        if dog:
            try:
                for k, text in (await dog.problems()).items():
                    if k.startswith("quiet_"):
                        lines.append(text.split("\n")[0])
            except Exception as ex:  # noqa: BLE001
                log.debug("信号源体检失败: %s", ex)
        from ..trade.regime import regime, text as rg_text
        lines.append(rg_text(await regime(self.app)))
        sk = await db.fetchone("SELECT COUNT(*) n FROM skipped WHERE ts>?", (time.time() - 3600,))
        top = await db.fetchall("SELECT cat, COUNT(*) n FROM skipped WHERE ts>? GROUP BY cat ORDER BY n DESC LIMIT 4",
                                (time.time() - 3600,))
        lines.append(f"近1小时被风控拦下 {sk['n']} 个币" + ("：" + "、".join(f"{r['cat']} {r['n']}" for r in top) if top else ""))
        last = await db.fetchone("SELECT MAX(opened_at) t FROM positions WHERE dry_run=?", (int(self.app.cfg.dry_run),))
        if last and last["t"]:
            lines.append(f"最近一次买入：{(time.time() - float(last['t'])) / 60:.0f} 分钟前")
        ws = await db.fetchall("SELECT status, COUNT(*) n FROM wallets GROUP BY status")
        wd = {r["status"]: r["n"] for r in ws}
        if wd.get("suspect"):
            lines.append(f"钱包体检：{wd['suspect']} 个可疑（不跟）")
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
            from ..discovery.expand import KIND_NAME
            kinds = await self.app.db.fetchall(
                "SELECT COALESCE(kind,'smart') k, COUNT(*) n FROM wallets WHERE chain=? AND status IN ('active','manual') "
                "GROUP BY k", (c,))
            lines.append(f"<b>{CHAINS[c].name} 监听中</b>：" + "、".join(f"{KIND_NAME.get(r['k'], r['k'])} {r['n']}" for r in kinds))
            rows = await self.app.db.fetchall(
                "SELECT * FROM wallets WHERE chain=? AND status IN ('active','manual') "
                "ORDER BY (sig_n>=8) DESC, COALESCE(sig_ret,-9) DESC, score DESC LIMIT 15", (c,))
            lines.append("按「跟它买 1 小时后的中位涨跌」排序（样本 ≥ 8 的在前）：")
            for r in rows:
                q = (f" | 跟买{r['sig_n']}次 中位{float(r['sig_ret']) * 100:+.0f}%" if r.get("sig_n") and r.get("sig_ret") is not None
                     else "")
                lines.append(f"{KIND_NAME.get(r.get('kind') or 'smart', '')[:2]}{self.addr_link(c, r['address'])} "
                             f"分{r['score']:.0f} 胜率{(r['win_rate'] or 0) * 100:.0f}% {fmt_usd(r['realized_usd'] or 0)}"
                             f" 跟单{r['follow_wins']}胜{r['follow_losses']}负{q}")
        return "\n".join(lines) or "暂无"
