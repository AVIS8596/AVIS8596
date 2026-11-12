"""高收益地址解剖（/autopsy）：GMGN 上赚钱的地址在什么时机、买什么样的币（学来给机器人自己选币用，见 autopsy_learn），
以及跟着它买的人能分到多少。

只读研究，不改任何买卖规则。做法：
1. 选钱包：GMGN 聪明钱 / KOL 交易流里出现的地址，按最近 30 天已实现利润取前 N 名；再加上我们正在监听、排名最靠前的 N 个。
2. 每个钱包拉最近约 200 笔交易流水，按代币拼成一笔笔仓位，只看最近 6 天、已经卖完（卖出 ≥ 90%）的。
3. 每笔仓位算两种收益：
   - 它自己的：卖出所得 / 买入成本 - 1；
   - 跟单的人的：它买入后晚 30 / 60 / 120 秒、按那一刻的 1 分钟 K 线价格买入，它第一次卖出后同样晚这么多秒卖出，
     再扣掉一买一卖的成本（默认 4%，autopsy.cost_pct）。
4. 按钱包汇总：自己赚多少 vs 跟着赚多少 → 分成"跟着也能赚""自己赚、跟不上""不赚"三类，
   再看两类钱包的打法（发币后多久买、入场市值、拿多久）有什么不同；按仓位看哪种单子跟得上。

结论只用来回答"跟单这条路成不成立、该跟谁、会选币的人看什么"。API 用量：每笔仓位查 1~2 次 K 线，
自己限速（默认每 3 秒一次），不会挤占机器人正常运行的额度；全部跑完大约 1~2 小时。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from .chains import CHAINS, norm_addr
from .utils import dig, f, fmt_usd

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.autopsy")

DELAYS = (30, 60, 120)
LOOKBACK_DAYS = 6           # K 线只回看最近几天（再早的 1 分钟 K 线不一定取得到）
MAX_POS_PER_WALLET = 15
CAP = 20.0                  # 单笔收益最多按 +2000% 算，免得一两笔百倍币把平均数撑爆


def _med(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def build_trades(acts: list[dict], holds: list[dict], now: float, days: float = LOOKBACK_DAYS) -> list[dict]:
    """交易流水 → 已经卖完的仓位：第一次买入（时间、价格）、第一次卖出时间、自己的收益、发币时间、供应量。"""
    meta: dict[str, dict] = {}
    for h in holds:
        a = dig(h, "token.token_address", "token.address", default="")
        if a:
            meta[a] = {"created": f(dig(h, "token.creation_timestamp", default=0)),
                       "supply": f(dig(h, "token.total_supply", "token.max_supply", default=0))}
    pos: dict[str, dict] = {}
    for a in sorted(acts, key=lambda x: f(x.get("timestamp"))):
        side = str(a.get("event_type") or a.get("type") or "").lower()
        tok = dig(a, "token.address", "token.token_address", default="")
        px, cost, ts = f(a.get("price_usd")), f(a.get("cost_usd")), f(a.get("timestamp"))
        if side not in ("buy", "sell") or not tok or px <= 0 or cost <= 0 or ts <= 0:
            continue
        p = pos.setdefault(tok, {"token": tok, "symbol": str(dig(a, "token.symbol", default="?")), "buys": [], "sells": []})
        p["buys" if side == "buy" else "sells"].append((ts, px, cost))
    out = []
    for tok, p in pos.items():
        if not p["buys"] or not p["sells"]:
            continue
        t_buy, px_buy = p["buys"][0][0], p["buys"][0][1]
        if now - t_buy > days * 86400:
            continue
        sells = [s for s in p["sells"] if s[0] >= t_buy]
        if not sells:
            continue                          # 卖出都在第一次买入之前：流水不全
        got = sum(c / px for _, px, c in p["buys"])
        sold = sum(c / px for _, px, c in sells)
        if got <= 0 or sold < got * 0.9:
            continue                          # 还没卖完：只看已经卖完的
        cost = sum(c for _, _, c in p["buys"])
        income = sum(c for _, _, c in sells)
        m = meta.get(tok, {})
        out.append({"token": tok, "symbol": p["symbol"], "t_buy": t_buy, "px_buy": px_buy, "t_sell": sells[0][0],
                    "px_sell": sells[0][1], "first_frac": min(1.0, (sells[0][2] / sells[0][1]) / got),
                    "t_last": sells[-1][0], "own": min(CAP, income / cost - 1), "cost": cost,
                    "hold_min": (sells[-1][0] - t_buy) / 60, "created": m.get("created") or None,
                    "supply": m.get("supply") or None,
                    "age_min": (t_buy - m["created"]) / 60 if m.get("created") and t_buy >= m["created"] else None,
                    "mcap": px_buy * m["supply"] if m.get("supply") else None})
    out.sort(key=lambda x: -x["t_buy"])
    return out


def price_at(candles: list[dict], ts: float) -> float:
    """ts 那一分钟的 1 分钟 K 线中间价（取不到返回 0）。"""
    best = None
    for c in candles:
        t = f(c.get("time"))
        t = t / 1000 if t > 1e12 else t
        if t <= ts and (best is None or t > best[0]):
            best = (t, c)
    if not best or ts - best[0] > 120:
        return 0.0
    o, cl = f(best[1].get("open")), f(best[1].get("close"))
    return (o + cl) / 2 if o > 0 and cl > 0 else cl


def follower_returns(tr: dict, entry_c: list[dict], exit_c: list[dict], cost: float) -> dict[int, float] | None:
    """晚 d 秒买入、它第一次卖出后晚 d 秒卖出，扣成本后的收益。价格对不上（单位不对 / 取错池子）返回 None。"""
    ref = price_at(entry_c, tr["t_buy"])
    if ref <= 0 or not 0.2 < tr["px_buy"] / ref < 5:
        return None
    out = {}
    for d in DELAYS:
        a, b = price_at(entry_c, tr["t_buy"] + d), price_at(exit_c, tr["t_sell"] + d)
        if a > 0 and b > 0:
            out[d] = min(CAP, b / a - 1 - cost)
    return out or None


def exit_features(tr: dict, ce: list[dict], cx: list[dict], full: bool) -> dict:
    """它怎么卖：第一次卖出时赚多少（按它自己的成交价）、卖了几成、拿了多久；
    K 线盖住整段持有时，再算持有期间最高涨到多少、卖的时候离最高点回撤多少。"""
    out = {"ex_sell_gain": tr["px_sell"] / tr["px_buy"] - 1 if tr["px_buy"] > 0 else None,
           "ex_first_frac": tr["first_frac"], "ex_hold_min": (tr["t_sell"] - tr["t_buy"]) / 60}
    if full:
        ref = price_at(ce, tr["t_buy"])
        mid = [c for c in cx if tr["t_buy"] - 60 <= f(c.get("time")) / (1000 if f(c.get("time")) > 1e12 else 1) <= tr["t_sell"]]
        hi = max((f(c.get("high")) or f(c.get("close")) for c in mid), default=0.0)
        sp = price_at(cx, tr["t_sell"])
        if ref > 0 and hi > 0:
            out["ex_peak_gain"] = min(CAP, hi / ref - 1)
            if sp > 0:
                out["ex_dd_at_sell"] = max(0.0, 1 - sp / hi)
    return out


def classify(n: int, own: float, fol: float, fol_med: float) -> str:
    if n < 5:
        return "样本少"
    if fol > 0.05 and fol_med >= 0:
        return "跟着也能赚"
    if own > 0.1:
        return "自己赚、跟不上"
    return "不赚"


def wallet_summary(trs: list[dict]) -> dict:
    fol = {d: [t["fol"][d] for t in trs if d in t["fol"]] for d in DELAYS}
    s = {"n": len(trs), "own": _avg([t["own"] for t in trs]), "own_med": _med([t["own"] for t in trs]),
         "own_win": sum(1 for t in trs if t["own"] > 0) / len(trs) if trs else 0.0,
         "hold": _med([t["hold_min"] for t in trs]),
         "age": _med([t["age_min"] for t in trs if t["age_min"] is not None]),
         "mcap": _med([t["mcap"] for t in trs if t["mcap"]])}
    for d in DELAYS:
        s[f"f{d}"] = _avg(fol[d])
        s[f"f{d}_med"] = _med(fol[d])
        s[f"f{d}_win"] = sum(1 for x in fol[d] if x > 0) / len(fol[d]) if fol[d] else 0.0
    s["cls"] = classify(s["n"], s["own"], s["f60"], s["f60_med"])
    return s


def _age(x: list[float]) -> str:
    return f"发币后 {_med(x):.0f} 分钟买" if x else "发币时间不明"


def _mc(x: list[float]) -> str:
    return f"市值 {fmt_usd(_med(x))}" if x else "市值不明"


def bucket(trs: list[dict], key: str, edges: list[tuple[float, str]]) -> list[str]:
    """按仓位的某个特征分档，看跟单（晚 60 秒）每笔赚多少。"""
    out = []
    lo = float("-inf")
    for hi, name in edges:
        xs = [t["fol"][60] for t in trs if 60 in t["fol"] and t.get(key) is not None and lo <= t[key] < hi]
        if len(xs) >= 5:
            out.append(f"{name} {len(xs)} 笔：平均 {_avg(xs) * 100:+.0f}%、中位 {_med(xs) * 100:+.0f}%、"
                       f"赚钱 {sum(1 for x in xs if x > 0) / len(xs) * 100:.0f}%")
        lo = hi
    return out


class Autopsy:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.task: asyncio.Task | None = None
        self.progress = ""
        g = self.cfg.get("autopsy.call_gap_sec", 3)
        self.gap = float(3 if g is None else g)

    async def _kline(self, chain: str, token: str, t0: float, t1: float) -> list[dict]:
        await asyncio.sleep(self.gap)               # 自己限速：不挤占机器人正常运行的 GMGN 额度
        try:
            return await self.app.gmgn.kline(chain, token, "1m", int(t0), int(t1)) or []
        except Exception as e:  # noqa: BLE001
            log.debug("K 线失败 %s: %s", token, e)
            return []

    async def candidates(self, chain: str, top: int) -> list[tuple[str, str, float]]:
        """(地址, 来源, 30 天已实现利润)：GMGN 高收益地址前 top 个 + 我们正在监听排名最靠前的 top 个。"""
        gm = self.app.gmgn
        makers: dict[str, str] = {}
        for src, fn in (("GMGN聪明钱", gm.smartmoney), ("GMGN KOL", gm.kol)):
            try:
                for t in await fn(chain, 200):
                    if t.get("maker"):
                        makers.setdefault(norm_addr(chain, t["maker"]), src)
            except Exception as e:  # noqa: BLE001
                log.warning("收集 %s 失败: %s", src, e)
        ranked: list[tuple[float, str]] = []
        addrs = list(makers)
        for i in range(0, len(addrs), 100):
            try:
                for r in await gm.wallet_profits(chain, addrs[i:i + 100], "30d"):
                    a = r.get("wallet_address")
                    if a and f(r.get("realized_profit")) > 0:
                        ranked.append((f(r.get("realized_profit")), norm_addr(chain, a)))
            except Exception as e:  # noqa: BLE001
                log.warning("批量查盈亏失败: %s", e)
        ranked.sort(reverse=True)
        out = [(a, makers.get(a, "GMGN"), p) for p, a in ranked[:top]]
        seen = {a for a, _, _ in out}
        for w in await self.app.db.active_wallets(chain, top * 2):
            if w["address"] not in seen and len([x for x in out if x[1] == "我们在监听"]) < top:
                out.append((w["address"], "我们在监听", f(w.get("realized_usd"))))
                seen.add(w["address"])
        return out

    async def wallet(self, chain: str, addr: str, cost: float) -> list[dict]:
        gm = self.app.gmgn
        acts: list[dict] = []
        cursor = None
        for _ in range(4):
            d = await gm.wallet_activity(chain, addr, 50, cursor)
            rows = (d.get("activities") or []) if isinstance(d, dict) else []
            acts += rows
            cursor = d.get("next") if isinstance(d, dict) else None
            if not cursor or len(rows) < 50:
                break
        holds: list[dict] = []
        try:
            d = await gm.wallet_holdings(chain, addr, 50)
            holds = (d.get("list") or []) if isinstance(d, dict) else []
        except Exception as e:  # noqa: BLE001
            log.debug("持仓查询失败 %s: %s", addr, e)
        out = []
        from .autopsy_learn import kline_features, snapshot
        for tr in build_trades(acts, holds, time.time())[:MAX_POS_PER_WALLET]:
            span = tr["t_sell"] - tr["t_buy"]
            # 买入前 1 小时的 K 线：还原它买入那一刻这个币的样子（涨了多少、离高点多远、成交额）
            if span <= 4 * 3600:                     # 拿得不久：一次 K 线就能盖住买入前 1 小时到卖出
                c = await self._kline(chain, tr["token"], tr["t_buy"] - 3600, tr["t_sell"] + 300)
                ce = cx = c
            else:
                ce = await self._kline(chain, tr["token"], tr["t_buy"] - 3600, tr["t_buy"] + 300)
                cx = await self._kline(chain, tr["token"], tr["t_sell"] - 180, tr["t_sell"] + 300)
            fol = follower_returns(tr, ce, cx, cost)
            if not fol:
                continue
            x = kline_features(ce, tr["t_buy"], tr["created"], tr["supply"], tr["px_buy"])
            x.update(await snapshot(self.app, chain, tr["token"], tr["t_buy"]))
            x.update(exit_features(tr, ce, cx, ce is cx))
            out.append({**tr, "fol": fol, "x": x})
        return out

    async def run(self, chains: list[str], top: int) -> list[str]:
        cost = float(self.cfg.get("autopsy.cost_pct", 4) or 4) / 100
        db = self.app.db
        await db.execute("CREATE TABLE IF NOT EXISTS autopsy (chain TEXT, address TEXT, ts REAL, source TEXT, "
                         "realized REAL, stats TEXT, PRIMARY KEY(chain, address))")
        # 每笔仓位都存下来（它买入那一刻的特征 + 它自己的收益 + 跟单收益），以后可以拿来训练选币
        await db.execute("CREATE TABLE IF NOT EXISTS autopsy_trades (chain TEXT, wallet TEXT, token TEXT, symbol TEXT, "
                         "t_buy REAL, own REAL, hold_min REAL, x TEXT, fol TEXT, PRIMARY KEY(chain, wallet, token, t_buy))")
        sections = []
        for chain in chains:
            cands = await self.candidates(chain, top)
            all_trs: list[dict] = []
            rows: list[tuple[str, str, float, dict]] = []
            for i, (addr, src, realized) in enumerate(cands, 1):
                self.progress = f"{CHAINS[chain].name}：第 {i}/{len(cands)} 个钱包"
                try:
                    trs = await self.wallet(chain, addr, cost)
                except Exception as e:  # noqa: BLE001
                    log.warning("解剖 %s 失败: %s", addr, e)
                    continue
                if not trs:
                    continue
                st = wallet_summary(trs)
                rows.append((addr, src, realized, st))
                all_trs += [{**t, "src": src} for t in trs]
                await db.executemany(
                    "INSERT OR REPLACE INTO autopsy_trades(chain,wallet,token,symbol,t_buy,own,hold_min,x,fol) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(chain, addr, t["token"], t["symbol"], t["t_buy"], t["own"], t["hold_min"],
                      json.dumps(t["x"], default=str), json.dumps(t["fol"])) for t in trs])
                await db.execute("INSERT OR REPLACE INTO autopsy(chain,address,ts,source,realized,stats) VALUES(?,?,?,?,?,?)",
                                 (chain, addr, time.time(), src, realized, json.dumps(st)))
            sections.append(await self.learn_report(chain, rows, all_trs))
            sections.append(self.report(chain, rows, all_trs, cost))
        await db.kv_set("autopsy_report", {"ts": time.time(), "sections": sections})
        return sections

    async def learn_report(self, chain: str, rows: list, trs: list[dict]) -> str:
        """主要部分：它们在什么时机买什么样的币；哪些特征分得出好坏；学成规则后在全部币上回测。"""
        from .autopsy_learn import baseline, candidate_rules, profile, separating, test_rules
        name = CHAINS[chain].name
        if not trs:
            return f"<b>{name}</b>：没拿到可用的数据（钱包最近 {LOOKBACK_DAYS} 天没有卖完的仓位，或 K 线取不到）"
        L = [f"🧠 <b>{name}：学高收益地址的买币逻辑</b>（{len(rows)} 个钱包、{len(trs)} 笔已卖完的仓位，最近 {LOOKBACK_DAYS} 天）"]
        L.append("<b>1. 它们买入那一刻，币是什么样的</b>")
        L += profile(trs)
        sep = separating(trs)
        if sep:
            L.append("<b>2. 同样是它们挑的币，哪些特征分得出赚钱的单和亏钱的单</b>（胜率 = 赚 30% 以上的比例）")
            L += sep
        rules = candidate_rules(trs)
        if rules:
            L.append(f"<b>3. 学成规则，放到机器人最近 {LOOKBACK_DAYS} 天见过的全部币上回测</b>"
                     "（规则 = 赚钱的单在这个特征上的中间一半范围）")
            L += test_rules(rules, await baseline(self.app, chain, LOOKBACK_DAYS))
        L.append("（只是研究结果，没改任何买卖规则。✅ 的规则可以先放进观察模式验证，再决定要不要用）")
        return "\n".join(L)

    def report(self, chain: str, rows: list[tuple[str, str, float, dict]], trs: list[dict], cost: float) -> str:
        name = CHAINS[chain].name
        if not trs:
            return f"<b>{name}</b>：没拿到可用的数据（钱包最近 {LOOKBACK_DAYS} 天没有卖完的仓位，或 K 线取不到）"
        L = [f"🔬 <b>{name}：跟着它们买能分到多少</b>：{len(rows)} 个钱包、{len(trs)} 笔已卖完的仓位"
             f"（最近 {LOOKBACK_DAYS} 天；跟单收益已扣一买一卖成本 {cost * 100:.0f}%）"]
        own = [t["own"] for t in trs]
        L.append(f"<b>1. 它们自己</b>：每笔平均 {_avg(own) * 100:+.0f}%、中位 {_med(own) * 100:+.0f}%、"
                 f"赚钱 {sum(1 for x in own if x > 0) / len(own) * 100:.0f}%")
        L.append("<b>2. 跟着买（它买后晚几秒买、它第一次卖后晚几秒卖）</b>")
        for d in DELAYS:
            xs = [t["fol"][d] for t in trs if d in t["fol"]]
            if xs:
                L.append(f"  晚 {d} 秒：{len(xs)} 笔，平均 {_avg(xs) * 100:+.0f}%、中位 {_med(xs) * 100:+.0f}%、"
                         f"赚钱 {sum(1 for x in xs if x > 0) / len(xs) * 100:.0f}%")
        for src in sorted({t["src"] for t in trs}):
            xs = [t["fol"][60] for t in trs if t["src"] == src and 60 in t["fol"]]
            if xs:
                L.append(f"  其中 {src} 的钱包（晚 60 秒）：{len(xs)} 笔，平均 {_avg(xs) * 100:+.0f}%、中位 {_med(xs) * 100:+.0f}%")
        # ---- 3. 钱包分类
        groups: dict[str, list[dict]] = {}
        for _, _, _, st in rows:
            groups.setdefault(st["cls"], []).append(st)
        L.append("<b>3. 钱包分三类</b>（按晚 60 秒跟单的结果）")
        for cls in ("跟着也能赚", "自己赚、跟不上", "不赚", "样本少"):
            g = groups.get(cls) or []
            if not g:
                continue
            ages = [s["age"] for s in g if s["age"]]
            mc = [s["mcap"] for s in g if s["mcap"]]
            L.append(f"  {cls} {len(g)} 个：自己每笔平均 {_avg([s['own'] for s in g]) * 100:+.0f}%，"
                     f"跟单平均 {_avg([s['f60'] for s in g]) * 100:+.0f}%；"
                     f"打法中位：{_age(ages)}、{_mc(mc)}、拿 {_med([s['hold'] for s in g]):.0f} 分钟")
        # ---- 4. 哪种单子跟得上
        L.append("<b>4. 哪种单子跟得上</b>（晚 60 秒跟单，按它的打法分档）")
        L += ["  拿多久 · " + x for x in bucket(trs, "hold_min", [(5, "5 分钟内"), (30, "5~30 分钟"), (120, "30 分钟~2 小时"),
                                                               (float("inf"), "2 小时以上")])]
        L += ["  发币后多久买 · " + x for x in bucket(trs, "age_min", [(5, "5 分钟内（狙击）"), (60, "5~60 分钟"),
                                                                   (360, "1~6 小时"), (float("inf"), "6 小时以上")])]
        L += ["  入场市值 · " + x for x in bucket(trs, "mcap", [(30_000, "3 万以下"), (100_000, "3~10 万"),
                                                               (500_000, "10~50 万"), (float("inf"), "50 万以上")])]
        # ---- 5. 跟着也能赚的名单
        good = sorted((r for r in rows if r[3]["cls"] == "跟着也能赚"), key=lambda r: -r[3]["f60"])[:10]
        if good:
            L.append("<b>5. 跟着也能赚的钱包</b>（前 10）")
            for addr, src, realized, st in good:
                L.append(f"  {addr[:4]}…{addr[-4:]}（{src}）{st['n']} 笔：自己 {st['own'] * 100:+.0f}%，"
                         f"跟单晚 60 秒 {st['f60'] * 100:+.0f}%（中位 {st['f60_med'] * 100:+.0f}%），"
                         f"拿 {st['hold']:.0f} 分钟" + (f"、发币后 {st['age']:.0f} 分钟买" if st["age"] else ""))
        L.append("（只是研究结果，没改任何买卖规则。跟单收益里没算交易失败和卖不出去，实盘只会更差）")
        return "\n".join(L)

    def start(self, chains: list[str], top: int) -> bool:
        if self.task and not self.task.done():
            return False

        async def go() -> None:
            tg = getattr(self.app, "tg", None)
            try:
                for sec in await self.run(chains, top):    # 每条链一条消息（Telegram 一条最多 4000 字）
                    if tg:
                        await tg.send(sec)
            except Exception as e:  # noqa: BLE001
                log.exception("解剖失败: %s", e)
                if tg:
                    await tg.send(f"🔬 高收益地址解剖失败：{html.escape(str(e))[:200]}")
            finally:
                self.progress = ""
        self.task = asyncio.create_task(go())
        return True

    async def last(self) -> list[str]:
        r: Any = await self.app.db.kv_get("autopsy_report", None)
        if not r or not r.get("sections"):
            return []
        head = f"（上次解剖：{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}）\n"
        return [head + s for s in r["sections"]]
