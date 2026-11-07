"""向顶级交易者学习：拆解他们的交易习惯，找共同规律，给出参数建议。

钱包来源：
  - 手动：TG 发 /learn 地址 [备注]（比如 GMGN 排行榜上的人）
  - 自动：每天从 GMGN 的 KOL / 聪明钱交易流里收集钱包，按最近 30 天已实现利润取前 20 名

每个钱包拉最近约 200 笔交易流水 + 100 个持仓（含已清仓），算出：
  入场（发币后多久买、入场市值、是否在 bonding curve 上买）、仓位、持有时长、
  止盈（第一次卖出时涨了多少、分几次卖、盈利单最后赚多少）、止损（亏损单亏多少就走）、
  利润集中度（是不是靠一两个大单）、偏好的发射平台。
只学"可以从链上统计出来的规律"；他们的消息来源（私密群、推特、项目方关系）学不到。
参数建议只给出来，不会自动改配置——先放进观察模式验证。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING

from .chains import norm_addr
from .utils import dig, f, fmt_usd

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.learn")

def _q(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def _med(xs: list[float]) -> float:
    return _q(xs, 0.5)


def build_positions(activities: list[dict], holdings: list[dict]) -> list[dict]:
    """把交易流水按代币归成一笔笔"仓位"，并用持仓数据补上发币时间、供应量、最终盈亏。"""
    hold = {}
    for h in holdings:
        a = dig(h, "token.token_address", "token.address", default="")
        if a:
            hold[a] = h
    pos: dict[str, dict] = {}
    for a in sorted(activities, key=lambda x: f(x.get("timestamp"))):
        side = str(a.get("event_type") or a.get("type") or "").lower()
        tok = dig(a, "token.address", "token.token_address", default="")
        if side not in ("buy", "sell") or not tok:
            continue
        px, cost, ts = f(a.get("price_usd")), f(a.get("cost_usd")), f(a.get("timestamp"))
        p = pos.setdefault(tok, {"token": tok, "symbol": dig(a, "token.symbol", default="?"), "buys": [], "sells": [],
                                 "launchpad": a.get("launchpad_platform") or ""})
        p["buys" if side == "buy" else "sells"].append((ts, px, cost))
    out = []
    for tok, p in pos.items():
        if not p["buys"]:
            continue   # 只看到卖出（买入在更早的流水里），信息不全，跳过
        h = hold.get(tok, {})
        first_ts, first_px = p["buys"][0][0], p["buys"][0][1]
        amt = sum(c / px for _, px, c in p["buys"] if px > 0)
        bought = sum(c for _, _, c in p["buys"])
        avg_px = bought / amt if amt > 0 else first_px
        created = f(dig(h, "token.creation_timestamp", default=0))
        supply = f(dig(h, "token.total_supply", "token.max_supply", default=0))
        cost_all = f(h.get("history_bought_cost")) or bought
        pnl = f(h.get("total_profit_pnl"), default=None) if h.get("total_profit_pnl") not in (None, "") else None
        if pnl is None and cost_all > 0:
            sold = f(h.get("history_sold_income")) or sum(c for _, _, c in p["sells"])
            pnl = (sold + f(h.get("usd_value")) - cost_all) / cost_all
        end = f(h.get("end_holding_at")) or (p["sells"][-1][0] if p["sells"] else 0)
        closed = bool(p["sells"]) and (f(h.get("balance")) <= 0 or f(h.get("usd_value")) < 1)
        out.append({
            "token": tok, "symbol": html.escape(str(p["symbol"])), "launchpad": p["launchpad"] or dig(h, "token.launchpad_platform", default=""),
            "entry_age_min": (first_ts - created) / 60 if created and first_ts >= created else None,
            "entry_mcap": first_px * supply if first_px and supply else None,
            "cost": cost_all, "buys": len(p["buys"]), "sells": len(p["sells"]),
            "first_sell_mult": (p["sells"][0][1] / avg_px) if p["sells"] and avg_px > 0 and p["sells"][0][1] > 0 else None,
            "hold_min": (end - first_ts) / 60 if closed and end > first_ts else None,
            "pnl": pnl, "closed": closed,
        })
    return out


def style_label(entry_age: float, hold: float) -> str:
    a = "狙击型" if entry_age < 5 else "早期型" if entry_age < 60 else "趋势型"
    h = "秒进秒出" if hold < 5 else "快进快出" if hold < 30 else "日内波段" if hold < 24 * 60 else "长拿"
    return f"{a}·{h}"


def profile_from(positions: list[dict]) -> dict:
    """单个钱包的交易画像（全部用中位数，不被个别极端单带偏）。"""
    ages = [p["entry_age_min"] for p in positions if p["entry_age_min"] is not None]
    mcaps = [p["entry_mcap"] for p in positions if p["entry_mcap"]]
    holds = [p["hold_min"] for p in positions if p["hold_min"] is not None]
    done = [p for p in positions if p["closed"] and p["pnl"] is not None]
    wins = [p["pnl"] * 100 for p in done if p["pnl"] > 0]
    losses = [p["pnl"] * 100 for p in done if p["pnl"] <= 0]
    first_sell = [(p["first_sell_mult"] - 1) * 100 for p in positions if p["first_sell_mult"]]
    profits = sorted((p["pnl"] * p["cost"] for p in done if p["pnl"] > 0), reverse=True)
    lp = Counter(p["launchpad"] or "其他" for p in positions)
    return {
        "n": len(positions), "closed": len(done),
        "win_rate": len(wins) / len(done) if done else 0.0,
        "entry_age_min": _med(ages), "entry_mcap": _med(mcaps),
        "entry_mcap_lo": _q(mcaps, 0.25), "entry_mcap_hi": _q(mcaps, 0.75),
        "early_pct": sum(1 for m in mcaps if m < 70_000) / len(mcaps) if mcaps else 0.0,
        "size": _med([p["cost"] for p in positions if p["cost"] > 0]),
        "hold_min": _med(holds), "sells_per_pos": _med([p["sells"] for p in done]) if done else 0,
        "first_sell_gain": _med(first_sell), "win_gain": _med(wins), "loss_cut": _med(losses),
        "top3_share": sum(profits[:3]) / sum(profits) if profits and sum(profits) > 0 else 0.0,
        "launchpads": [k for k, _ in lp.most_common(3)],
        "style": style_label(_med(ages) if ages else 999, _med(holds) if holds else 999),
    }


def profile_text(label: str, addr: str, prof: dict, stats: dict | None = None) -> str:
    s = stats or {}
    head = f"🧠 <b>{html.escape(label or addr[:6] + '…')}</b> {prof['style']}"
    if s:
        head += f"\n30 天已实现 {fmt_usd(s.get('realized_usd', 0))}，胜率 {s.get('win_rate', 0) * 100:.0f}%"
    if prof["n"] < 5:
        return head + f"\n最近交易太少（{prof['n']} 个币），画像不可靠"
    return "\n".join([
        head,
        f"样本：最近 {prof['n']} 个币（已清仓 {prof['closed']} 个，赚钱 {prof['win_rate'] * 100:.0f}%）",
        f"入场：发币后中位 {prof['entry_age_min']:.0f} 分钟买；入场市值中位 {fmt_usd(prof['entry_mcap'])}"
        f"（多数在 {fmt_usd(prof['entry_mcap_lo'])}~{fmt_usd(prof['entry_mcap_hi'])}），"
        f"{prof['early_pct'] * 100:.0f}% 在毕业前买",
        f"仓位：中位 {fmt_usd(prof['size'])}/笔",
        f"持有：中位 {prof['hold_min']:.0f} 分钟",
        f"止盈：第一次卖出时中位 +{prof['first_sell_gain']:.0f}%；盈利单最后中位 +{prof['win_gain']:.0f}%；"
        f"一般分 {prof['sells_per_pos']:.0f} 次卖",
        f"止损：亏损单中位 {prof['loss_cut']:.0f}% 离场",
        f"利润集中度：前 3 个大单占 {prof['top3_share'] * 100:.0f}% 利润"
        + ("（靠少数大单）" if prof["top3_share"] > 0.6 else "（比较均匀）"),
        "常用平台：" + "、".join(prof["launchpads"]),
    ])


def suggestions(profiles: list[dict], cfg) -> list[str]:
    """把多个高手的共同规律和机器人当前参数对比，给出建议（只建议，不自动改）。"""
    ps = [p for p in profiles if p.get("n", 0) >= 5]
    if len(ps) < 3:
        return [f"有效画像只有 {len(ps)} 个，至少 3 个才能总结共同规律"]
    out = []
    age = _med([p["entry_age_min"] for p in ps])
    hold = _med([p["hold_min"] for p in ps])
    fs = _med([p["first_sell_gain"] for p in ps])
    loss = _med([p["loss_cut"] for p in ps])
    early = _med([p["early_pct"] for p in ps])
    out.append(f"共同规律（{len(ps)} 人的中位数）：发币后 {age:.0f} 分钟入场、{early * 100:.0f}% 在毕业前买、"
               f"拿 {hold:.0f} 分钟、第一次卖在 +{fs:.0f}%、亏 {-loss:.0f}% 砍仓")
    if age < 5 or early > 0.6:
        out.append("• 他们大多在发币几分钟内、毕业前就买（狙击/内盘），机器人通过 API 下单慢几秒，这部分学不来，"
                   "直接跟他们的单容易买在被抬高的价格上")
    stop = float(cfg.get("exit.stop_loss_pct", -35))
    if loss and loss > stop + 5:
        out.append(f"• 他们亏 {-loss:.0f}% 左右就走，机器人止损是 {stop:.0f}%：可以考虑收紧止损")
    tps = cfg.get("exit.take_profit") or [{"gain_pct": 100}]
    tp1 = float(tps[0]["gain_pct"]) if tps else 100
    if fs and fs < tp1 * 0.7:
        out.append(f"• 他们第一次止盈中位在 +{fs:.0f}%，机器人第一档是 +{tp1:.0f}%：可以考虑增加一档更早的小止盈")
    if hold and hold < 60:
        out.append(f"• 他们中位只拿 {hold:.0f} 分钟，比机器人"
                   f"一般档的 1 小时观察期短：跟这类人要更快止盈")
    if len(out) == 1:
        out.append("• 机器人现在的参数和他们的习惯差不多，暂时不用改")
    out.append("建议先放进观察模式对比，确认有效再改正式参数")
    return out


class Learner:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg

    async def profile(self, chain: str, addr: str) -> tuple[dict, dict, str]:
        """拉数据并生成画像。返回 (画像, 30 天统计, 名字)。"""
        gm = self.app.gmgn
        acts: list[dict] = []
        cursor = None
        for _ in range(4):
            d = await gm.wallet_activity(chain, addr, 50, cursor)
            rows = d.get("activities") or [] if isinstance(d, dict) else []
            acts += rows
            cursor = d.get("next") if isinstance(d, dict) else None
            if not cursor or len(rows) < 50:
                break
        holds: list[dict] = []
        cursor = None
        for _ in range(2):
            try:
                d = await gm.wallet_holdings(chain, addr, 50, cursor)
            except Exception as e:  # noqa: BLE001
                log.debug("持仓查询失败 %s: %s", addr, e)
                break
            rows = d.get("list") or [] if isinstance(d, dict) else []
            holds += rows
            cursor = d.get("next") if isinstance(d, dict) else None
            if not cursor or len(rows) < 50:
                break
        st = await gm.wallet_stats(chain, addr, "30d")
        ps = st.get("pnl_stat") or {}
        win = f(dig(ps, "winrate", default=st.get("winrate")))
        stats = {"realized_usd": f(st.get("realized_profit")), "win_rate": win / 100 if win > 1 else win}
        name = str(dig(st, "common.twitter_username", "common.name", default="") or "")
        return profile_from(build_positions(acts, holds)), stats, name

    async def learn(self, chain: str, addr: str, label: str = "", source: str = "manual") -> str:
        addr = norm_addr(chain, addr)
        prof, stats, name = await self.profile(chain, addr)
        label = label or (f"@{name}" if name else "")
        await self.app.db.execute(
            "INSERT INTO learn_wallets(chain,address,label,source,added_at,profiled_at,profile) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chain,address) DO UPDATE SET label=CASE WHEN excluded.label!='' THEN excluded.label "
            "ELSE learn_wallets.label END, profiled_at=excluded.profiled_at, profile=excluded.profile",
            (chain, addr, label, source, time.time(), time.time(), json.dumps({**prof, "stats": stats})))
        if source == "manual":
            # 最近 30 天依然达标的，加入监听名单（照常过聪明钱标准，不达标只学习不监听）
            await self.app.smart.evaluate_many(chain, {addr: "learn_manual"})
            w = await self.app.db.wallet(chain, addr)
            tail = "\n✅ 最近 30 天达标，已加入聪明钱监听" if w and w["status"] == "active" else \
                "\n只学习、不监听（最近 30 天没达到聪明钱标准）"
            return profile_text(label, addr, prof, stats) + tail
        return profile_text(label, addr, prof, stats)

    async def summary(self) -> str:
        rows = await self.app.db.fetchall("SELECT * FROM learn_wallets ORDER BY profiled_at DESC LIMIT 50")
        if not rows:
            return "还没有学习任何钱包。发 /learn 钱包地址 添加，或发 /learn auto 让机器人自动找"
        profs = []
        lines = [f"<b>学习中的交易者 {len(rows)} 个</b>"]
        for r in sorted(rows, key=lambda r: -f((json.loads(r["profile"] or "{}").get("stats") or {}).get("realized_usd"))):
            p = json.loads(r["profile"] or "{}")
            profs.append(p)
            st = p.get("stats") or {}
            if len(lines) <= 15:
                lines.append(f"{html.escape(r['label'] or r['address'][:6] + '…')}（{'手动' if r['source'] == 'manual' else '自动'}）"
                             f" {p.get('style', '?')} | 30天 {fmt_usd(st.get('realized_usd', 0))}"
                             f" | 拿 {p.get('hold_min', 0):.0f} 分钟 | 第一次卖 +{p.get('first_sell_gain', 0):.0f}%")
        lines.append("")
        lines += suggestions(profs, self.cfg)
        lines.append("\n查看某个人的详细画像：/learn 钱包地址")
        return "\n".join(lines)

    async def auto(self, chain: str = "solana", top: int = 20) -> int:
        """从 GMGN 的 KOL / 聪明钱交易流收集钱包，按 30 天已实现利润取前 N 名学习。返回学习人数。"""
        gm = self.app.gmgn
        makers: dict[str, str] = {}
        for src, fn in (("kol", gm.kol), ("smart", gm.smartmoney)):
            try:
                for t in await fn(chain, 200):
                    if t.get("maker"):
                        makers.setdefault(norm_addr(chain, t["maker"]), src)
            except Exception as e:  # noqa: BLE001
                log.warning("收集 %s 钱包失败: %s", src, e)
        addrs = list(makers)
        ranked: list[tuple[float, str]] = []
        for i in range(0, len(addrs), 100):
            try:
                for r in await gm.wallet_profits(chain, addrs[i:i + 100], "30d"):
                    a = r.get("wallet_address")
                    if a:
                        ranked.append((f(r.get("realized_profit")), norm_addr(chain, a)))
            except Exception as e:  # noqa: BLE001
                log.warning("批量查盈亏失败: %s", e)
        ranked.sort(reverse=True)
        n = 0
        for profit, a in ranked[:top]:
            if profit <= 0:
                break
            try:
                await self.learn(chain, a, source="auto")
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("学习 %s 失败: %s", a, e)
            await asyncio.sleep(1)
        if ranked:
            # 达标的自动进监听名单（照常过聪明钱标准）
            await self.app.smart.evaluate_many(chain, {a: "learn_top" for _, a in ranked[:top]})
        return n

    async def run(self) -> None:
        """每 24 小时自动学习一轮；手动添加的钱包每 24 小时重新画像。"""
        await asyncio.sleep(300)
        while True:
            for chain in self.cfg.enabled_chains:
                if chain != "solana":
                    continue
                try:
                    n = await self.auto(chain)
                    log.info("自动学习完成：%d 个钱包", n)
                    for r in await self.app.db.fetchall(
                            "SELECT address, label FROM learn_wallets WHERE chain=? AND source='manual' "
                            "AND profiled_at<?", (chain, time.time() - 20 * 3600)):
                        await self.learn(chain, r["address"], r["label"] or "", "manual")
                except Exception as e:  # noqa: BLE001
                    log.warning("自动学习失败: %s", e)
            await asyncio.sleep(24 * 3600)
