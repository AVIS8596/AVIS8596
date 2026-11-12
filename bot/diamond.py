"""钻石手打法（/diamond）：学那些靠一个币拿得住、赚几万 U 的人——他们在什么时候、什么样的币上下重仓，拿多久、怎么卖。
学完在模拟盘里按"钻石手打法（实验仓）"单独下单记账，和其他来源对比。只读学习 + 模拟盘小仓位，不改现有买卖规则。

1. 找人：最近涨得最多的币（GMGN 6 小时 / 24 小时热门榜市值 ≥ $30 万的，加上机器人最近 7 天见过、之后涨了 5 倍以上的），
   每个币取盈利前 50 名，留下：赚 ≥ $5000（diamond.min_profit_usd）、拿了 ≥ 6 小时（还没卖完的算到现在）、
   不是项目方 / 捆绑 / 老鼠仓 / 开盘狙击的人。
2. 还原他们买入那一刻：发币多久、入场市值、买前 1 小时涨了多少、1 小时内已经拉了多少（K 线），
   机器人当时也分析过这个币的再对上持有人、前 10、捆绑、聪明钱、流动性、推特、评分；以及他们拿了多久、
   卖在几倍、还拿着的比例。
3. 学买入：
   - 他们出手的范围（发币多久、市值、前 1 小时涨跌的 5%~95% 区间）；
   - 范围里哪些币"拿 24 小时能翻倍"：用机器人自己见过的几千个币（结果档案）学——这些币当时的数据，之后 24 小时
     （数据还没攒够 24 小时的先用 4 小时）是不是翻倍。只看钻石手自己赚钱的单会"只看到幸存者"，必须和同样范围里
     死掉的币一起学。按时间 70% 训练 / 30% 检验，合格（AUC ≥ 0.6、分数线以上平均赚 10% 以上）才下单。
4. 学卖出：钻石手的打法是拿住——翻倍先收回本金，剩下的不设固定止盈，从最高点回撤很多才分批卖，
   最长拿多久按他们实际持有时间的 75% 分位（最多 7 天）；止损放宽到 -50%（他们扛得住波动）。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from .autopsy_learn import kline_features
from .chains import norm_addr
from .lanes import lane_score
from .imitate import _clamp, _med, _q, compare_sources
from .model import auc, fit, score
from .analysis.intel import INTEL_KEYS
from .utils import f, fmt_usd

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.diamond")

PREFIX = "钻石手打法"
INSIDER = {"sniper", "bundler", "rat_trader", "dev"}
NAMES = ["age_min", "mcap", "chg60", "holders", "top10_pct", "bundle_held_pct", "smart", "liquidity", "has_x",
         "score", "volume_1h", "buys_1h", "sells_1h", "fresh_pct", "bot_pct",
         *INTEL_KEYS]
GATE_KEYS = ("age_min", "mcap", "chg60")
LABELS = {"age_min": "发币多久（分钟）", "mcap": "市值", "chg60": "前 1 小时涨跌", "run_up": "1 小时内已拉",
          "holders": "持有人", "top10_pct": "前 10 持仓%", "bundle_held_pct": "捆绑%", "smart": "聪明钱数",
          "liquidity": "流动性", "has_x": "推特", "score": "代币评分", "dev_best_mcap": "项目方最好的币市值",
          "dev_good_n": "项目方做到 50 万的币", "dev_tokens_n": "项目方发过几个币", "whale_n": "前 20 里大户",
          "smart_hold_n": "前 20 里聪明钱", "same_funder_n": "同一出资人最大一伙", "tg_members": "TG 人数",
          "tg_growth_h": "TG 每小时增长"}
DEFAULTS = {"enabled": True, "min_profit_usd": 5000, "min_hold_hours": 6, "tokens_per_run": 40, "max_per_day": 10,
            "stop_loss_pct": -50, "min_rows": 400, "min_pos": 20, "retrain_hours": 24}


def vec(x: dict) -> list[float | None]:
    return [None if x.get(k) is None else f(x.get(k)) for k in NAMES]


def is_diamond(t: dict, now: float, min_profit: float, min_hold_h: float) -> dict | None:
    """一个币的盈利前 N 名里的一个人 → 是不是钻石手；是的话返回他这笔的摘要。"""
    tags = {str(x).lower() for x in (t.get("maker_token_tags") or []) + (t.get("tags") or [])}
    if tags & INSIDER or int(f(t.get("addr_type"))) == 2 or not t.get("address"):
        return None
    profit = f(t.get("profit"))
    start = f(t.get("start_holding_at"))
    if profit < min_profit or start <= 0:
        return None
    sold = f(t.get("sell_amount_percentage"))
    holding = sold < 0.9
    end = now if holding else (f(t.get("end_holding_at")) or now)
    hold_h = (end - start) / 3600
    if hold_h < min_hold_h:
        return None
    cost = f(t.get("avg_cost"))
    return {"wallet": t["address"], "t_buy": start, "px_buy": cost, "profit": profit, "hold_h": hold_h,
            "holding": int(holding), "sold": sold,
            "sold_x": f(t.get("avg_sold")) / cost if cost > 0 and f(t.get("avg_sold")) > 0 else None,
            "kol": int(bool(tags & {"kol", "renowned"}))}


def learn_gate(rows: list[dict]) -> dict[str, tuple[float, float]]:
    out = {}
    for k in GATE_KEYS:
        xs = [f(r["x"][k]) for r in rows if r["x"].get(k) is not None]
        if len(xs) >= 20:
            out[k] = (_q(xs, 0.05), _q(xs, 0.95))
    return out


def in_gate(x: dict, gate: dict) -> str:
    for k, (lo, hi) in gate.items():
        v = x.get(k)
        if v is not None and not lo <= f(v) <= hi:
            return f"{LABELS.get(k, k)} 不在钻石手出手的范围"
    return ""


def base_x(ft: dict) -> dict:
    """结果档案 / 分析报告里的数据 → 学习用的特征。"""
    x: dict[str, Any] = {}
    if ft.get("age_hours") is not None:
        x["age_min"] = f(ft["age_hours"]) * 60
    if ft.get("change_1h") is not None:
        x["chg60"] = f(ft["change_1h"]) / 100
    for k in ("mcap", "holders", "top10_pct", "bundle_held_pct", "liquidity", "score", "volume_1h", "buys_1h",
              "sells_1h", "fresh_pct", "bot_pct", *INTEL_KEYS):
        if ft.get(k) is not None:
            x[k] = f(ft[k])
    sm = ft.get("smart")
    if sm is None and ft.get("smart_buyers") is not None:
        sm = len(ft.get("smart_buyers") or [])
    if sm is not None:
        x["smart"] = f(sm)
    if ft.get("has_x") is not None:
        x["has_x"] = int(bool(ft["has_x"]))
    return x


def learn_exit(rows: list[dict], stop: float) -> dict:
    holds = [r["hold_h"] for r in rows]
    if len(rows) < 10:
        return {}
    mx = round(_clamp(_q(holds, 0.75), 24, 168), 1)
    return {"stop_loss_pct": float(stop), "take_profit": [{"gain_pct": 100, "sell_pct": 50}],
            "recover_cost_only": True, "trailing_start_pct": 10000, "stall_hours": 0, "max_hold_hours": mx,
            "runner": {"enabled": True, "trigger_gain_pct": 100, "floor_gain_pct": 30, "max_hold_hours": mx,
                       "ladder": [{"drop_pct": 40, "sell_pct": 33}, {"drop_pct": 55, "sell_pct": 50},
                                  {"drop_pct": 70, "sell_pct": 100}]},
            "_hold_med": round(_med(holds), 1), "_n": len(rows)}


def train(base: list[dict], gate: dict, min_rows: int, min_pos: int) -> dict:
    """base：机器人见过的币 {"t", "x", "ret"}（ret = 拿 24 / 4 小时的收益）。范围内学"会不会翻倍"。"""
    data = sorted((b for b in base if not in_gate(b["x"], gate)), key=lambda b: b["t"])
    y_all = [int(b["ret"] >= 1.0) for b in data]
    if len(data) < min_rows or sum(y_all) < min_pos:
        return {"ok": False, "why": f"范围内的币 {len(data)} 个（要 ≥ {min_rows}），翻倍的 {sum(y_all)} 个（要 ≥ {min_pos}）"}
    cut = int(len(data) * 0.7)
    X, Xv = [vec(b["x"]) for b in data[:cut]], [vec(b["x"]) for b in data[cut:]]
    y, yv = y_all[:cut], y_all[cut:]
    rv = [b["ret"] for b in data[cut:]]
    m = fit(X, y, Xv, yv, rounds=250, nb=12)
    pv = [score(m, x) for x in Xv]
    a = auc(pv, yv)
    order = sorted(range(len(pv)), key=lambda i: -pv[i])
    best_cut, best_sum, acc, cnt = None, 0.0, 0.0, 0
    for i in order:
        acc += rv[i]
        cnt += 1
        if cnt >= max(10, len(pv) // 50) and acc / cnt >= 0.10 and acc > best_sum:
            best_sum, best_cut = acc, pv[i]
    above = [rv[i] for i in range(len(pv)) if best_cut is not None and pv[i] >= best_cut]
    imp = sorted(((v, NAMES[j]) for j, v in enumerate(m["imp"]) if v > 0), reverse=True)[:5]
    tot = sum(v for v, _ in imp) or 1
    ok = a >= 0.6 and best_cut is not None
    return {"ok": ok, "why": "" if ok else (f"检验不合格：AUC {a:.2f}（要 ≥ 0.6）" if a < 0.6 else
                                            "检验不合格：找不到平均赚 10% 以上的分数线"),
            "model": m, "cut": best_cut, "auc": round(a, 3), "n": len(data), "n_val": len(pv),
            "pos_rate": round(sum(y_all) / len(data), 3), "val_avg": round(sum(rv) / len(rv), 4) if rv else 0.0,
            "pick_avg": round(sum(above) / len(above), 4) if above else 0.0, "pick_n": len(above),
            "imp": [(LABELS.get(n, n), round(v / tot * 100)) for v, n in imp]}


class Diamond:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.m: dict[str, dict] = {}
        self._seen: dict[str, float] = {}
        self.task: asyncio.Task | None = None

    def c(self) -> dict:
        return {**DEFAULTS, **(self.cfg.get("diamond", {}) or {})}

    async def _table(self) -> None:
        await self.app.db.execute(
            "CREATE TABLE IF NOT EXISTS diamond_trades (chain TEXT, wallet TEXT, token TEXT, symbol TEXT, t_buy REAL, "
            "profit REAL, hold_h REAL, holding INTEGER, sold_x REAL, x TEXT, ts REAL, PRIMARY KEY(chain, wallet, token))")

    # ---------------- 找钻石手、还原他们的买入
    async def winners(self, chain: str, n: int) -> list[str]:
        db = self.app.db
        rows = await db.fetchall(
            "SELECT token, MAX(MAX(COALESCE(p240,0), COALESCE(p1440,0)) / price) x FROM token_outcomes "
            "WHERE chain=? AND ts>? AND price>0 GROUP BY token HAVING x>=5 ORDER BY x DESC LIMIT ?",
            (chain, time.time() - 7 * 86400, n))
        toks = [r["token"] for r in rows]
        for iv in ("24h", "6h"):
            try:
                for t in await self.app.gmgn.rank(chain, iv, 50):
                    if t.get("address") and f(t.get("market_cap") or t.get("usd_market_cap")) >= 300_000:
                        toks.append(norm_addr(chain, t["address"]))
            except Exception as e:  # noqa: BLE001
                log.debug("热门榜 %s: %s", iv, e)
        return list(dict.fromkeys(toks))[:n]

    async def collect(self, chain: str) -> int:
        c = self.c()
        await self._table()
        db, gm, now = self.app.db, self.app.gmgn, time.time()
        found = 0
        for tok in await self.winners(chain, int(c["tokens_per_run"])):
            try:
                ov = await self.app.data.overview(chain, tok)
                traders = await gm.top_traders(chain, tok, 50, order_by="profit")
            except Exception as e:  # noqa: BLE001
                log.debug("钻石手 %s: %s", tok, e)
                continue
            created = f(ov.get("created_ts"))
            supply = f(ov.get("mcap")) / f(ov.get("price")) if f(ov.get("price")) > 0 else f(ov.get("total_supply"))
            for t in traders:
                d = is_diamond(t, now, float(c["min_profit_usd"]), float(c["min_hold_hours"]))
                if not d:
                    continue
                w = norm_addr(chain, d["wallet"])
                if await db.fetchone("SELECT 1 FROM diamond_trades WHERE chain=? AND wallet=? AND token=?", (chain, w, tok)):
                    continue
                tb = d["t_buy"]
                res, span = ("1m", 3600) if now - tb < 6 * 86400 else ("5m", 4 * 3600)
                await asyncio.sleep(float(self.cfg.get("autopsy.call_gap_sec", 3) or 0))
                try:
                    cs = await gm.kline(chain, tok, res, int(tb - span), int(tb + 300)) or []
                except Exception:  # noqa: BLE001
                    cs = []
                x = kline_features(cs, tb, created or None, supply or None, d["px_buy"])
                snap = await db.fetchone("SELECT features FROM token_outcomes WHERE chain=? AND token=? AND ts BETWEEN ? AND ? "
                                         "ORDER BY ABS(ts - ?) LIMIT 1", (chain, tok, tb - 7200, tb + 1800, tb))
                if snap:
                    try:
                        x.update({k: v for k, v in base_x(json.loads(snap["features"] or "{}") or {}).items()
                                  if k not in ("age_min", "mcap")})
                    except (TypeError, ValueError):
                        pass
                x["kol"] = d["kol"]
                await db.execute(
                    "INSERT OR REPLACE INTO diamond_trades(chain,wallet,token,symbol,t_buy,profit,hold_h,holding,sold_x,x,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (chain, w, tok, str(ov.get("symbol") or "?"), tb, d["profit"], d["hold_h"],
                                                      d["holding"], d["sold_x"], json.dumps(x, default=str), now))
                found += 1
        return found

    async def diamonds(self, chain: str, days: float = 30) -> list[dict]:
        await self._table()
        out = []
        for r in await self.app.db.fetchall("SELECT * FROM diamond_trades WHERE chain=? AND t_buy>?",
                                            (chain, time.time() - days * 86400)):
            try:
                x = json.loads(r["x"] or "{}") or {}
            except (TypeError, ValueError):
                x = {}
            out.append({**dict(r), "x": x})
        return out

    async def base(self, chain: str, days: float = 10) -> tuple[list[dict], str]:
        """机器人见过的币：24 小时结果够 300 个就用 24 小时，否则先用 4 小时（拿不到价格 = 归零，算 -100%）。"""
        db = self.app.db
        n24 = await db.fetchone("SELECT COUNT(*) n FROM token_outcomes WHERE chain=? AND p1440 IS NOT NULL", (chain,))
        col = "p1440" if n24 and n24["n"] >= 300 else "p240"
        rows = await db.fetchall(f"SELECT ts, token, price, {col} p, features FROM token_outcomes WHERE chain=? AND ts>? "
                                 f"AND price>0 AND {col} IS NOT NULL AND {col}<>0 ORDER BY ts", (chain, time.time() - days * 86400))
        seen: set[str] = set()
        out = []
        for r in rows:
            if r["token"] in seen:
                continue
            seen.add(r["token"])
            try:
                ft = json.loads(r["features"] or "{}") or {}
            except (TypeError, ValueError):
                continue
            ret = -1.0 if f(r["p"]) < 0 else min(20.0, f(r["p"]) / f(r["price"]) - 1)
            out.append({"t": f(r["ts"]), "x": base_x(ft), "ret": ret - 0.04})
        return out, ("24 小时" if col == "p1440" else "4 小时（24 小时的结果还没攒够）")

    async def train(self) -> list[str]:
        c = self.c()
        out = []
        for chain in self.cfg.enabled_chains:
            ds = await self.diamonds(chain)
            if not ds:
                continue
            gate = learn_gate(ds)
            base, horizon = await self.base(chain)
            ent = train(base, gate, int(c["min_rows"]), int(c["min_pos"]))
            ext = learn_exit(ds, float(c["stop_loss_pct"]))
            self.m[chain] = {"gate": gate, "entry": ent, "exit": ext, "trained_at": time.time(), "n": len(ds),
                             "horizon": horizon}
            out.append(f"{chain}：钻石手 {len(ds)} 笔，买入{'可用' if ent.get('ok') else '不可用（' + ent.get('why', '') + '）'}")
        await self.app.db.kv_set("diamond_models", self.m)
        return out

    def ready(self, chain: str) -> bool:
        m = self.m.get(chain) or {}
        return bool(self.c().get("enabled")) and bool((m.get("entry") or {}).get("ok")) and bool(m.get("exit"))

    def exit_rules(self, chain: str, base: dict) -> dict | None:
        ext = (self.m.get(chain) or {}).get("exit") or {}
        if not ext:
            return None
        b = {k: v for k, v in (base or {}).items() if k not in ("tiers", "dynamic", "learned")}
        return {**b, **{k: v for k, v in ext.items() if not k.startswith("_")}}

    # ---------------- 实时：不花额度，直接用分析报告里的数据打分
    async def on_analysis(self, chain: str, token: str, rep: dict) -> None:
        if not self.ready(chain) or rep.get("risks"):
            return
        now = time.time()
        if now - self._seen.get(f"{chain}:{token}", 0) < 3600:
            return
        self._seen[f"{chain}:{token}"] = now
        if len(self._seen) > 20000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 3600}
        m = self.m[chain]
        x = base_x(rep)
        if in_gate(x, m["gate"]):
            await lane_score(self.app.db, rep.get("_oid"), PREFIX, -1)
            return
        p = score(m["entry"]["model"], vec(x))
        await lane_score(self.app.db, rep.get("_oid"), PREFIX, p)
        if p < float(m["entry"]["cut"]):
            return
        n = await self.app.db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?",
                                       (PREFIX + "%", self.app.day_start()))
        if n and n["n"] >= int(self.c()["max_per_day"]):
            return
        reason = f"{PREFIX}（实验仓，分数 {p:.2f}）"
        log.info("%s 买入候选 %s：%s", PREFIX, rep.get("symbol"), reason)
        t = asyncio.create_task(self.app.hub.confirm_and_buy(chain, token, rep, reason,
                                                             wallets=rep.get("smart_buyers") or [], tier="normal"))
        self.app.hub._tasks.add(t)
        t.add_done_callback(self.app.hub._tasks.discard)

    # ---------------- 报告
    async def text(self) -> str:
        L = [f"💎 <b>{PREFIX}</b>（学靠一个币拿得住、赚几万 U 的人；模拟盘 ¼ 仓位单独记账）"]
        for chain in self.cfg.enabled_chains:
            ds = await self.diamonds(chain)
            if not ds:
                continue
            m = self.m.get(chain) or {}
            L.append(f"<b>{chain.upper()}：找到 {len(ds)} 笔钻石手</b>（{len({d['wallet'] for d in ds})} 个人、"
                     f"{len({d['token'] for d in ds})} 个币，每笔赚 ≥ {fmt_usd(self.c()['min_profit_usd'])}、拿 ≥ "
                     f"{self.c()['min_hold_hours']:g} 小时）")
            prof = [d["profit"] for d in ds]
            holds = [d["hold_h"] for d in ds]
            sx = [f(d["sold_x"]) for d in ds if d.get("sold_x")]
            L.append(f"  他们：每笔中位赚 {fmt_usd(_med(prof))}、中位拿 {_med(holds):.0f} 小时，"
                     f"{sum(d['holding'] for d in ds) / len(ds) * 100:.0f}% 现在还拿着"
                     + (f"，卖出的中位卖在 {_med(sx):.1f} 倍" if sx else ""))
            L.append("  他们买入那一刻：")
            for k in ("age_min", "mcap", "chg60", "run_up", "holders", "top10_pct", "bundle_held_pct", "smart", "has_x", "score",
                      "dev_best_mcap", "whale_n", "same_funder_n", "tg_members"):
                xs = [f(d["x"][k]) for d in ds if d["x"].get(k) is not None]
                if len(xs) >= 5:
                    L.append(f"    {LABELS.get(k, k)}：中位 {_fmt(k, _med(xs))}（多数在 {_fmt(k, _q(xs, .25))} ~ {_fmt(k, _q(xs, .75))}，{len(xs)} 笔）")
            ent = m.get("entry") or {}
            if m.get("gate"):
                L.append("  出手范围：" + "、".join(f"{LABELS[k]} {_fmt(k, lo)}~{_fmt(k, hi)}" for k, (lo, hi) in m["gate"].items()))
            if ent.get("ok"):
                L.append(f"  买入判断（用机器人见过的同范围的币学，结果看 {m.get('horizon')}）：范围内 {ent['n']} 个币、翻倍的占 "
                         f"{ent['pos_rate'] * 100:.0f}%；检验集 AUC {ent['auc']:.2f}，分数 ≥ {ent['cut']:.2f} 的 {ent['pick_n']} 个"
                         f"平均 {ent['pick_avg'] * 100:+.0f}%（全部 {ent['val_avg'] * 100:+.0f}%）；最看重："
                         + "、".join(f"{n} {v}%" for n, v in ent.get("imp", [])))
            elif ent:
                L.append(f"  买入判断：还不能用（{ent.get('why')}），这条链暂不下单")
            ext = m.get("exit") or {}
            if ext:
                L.append(f"  卖出：翻倍先收回本金，剩下的不设固定止盈，从最高点回撤 40% / 55% / 70% 分批卖、回落到 +30% 卖完；"
                         f"止损 {ext['stop_loss_pct']:.0f}%；最长拿 {ext['max_hold_hours']:g} 小时（钻石手中位拿 {ext['_hold_med']:g} 小时）")
        if len(L) == 1:
            return L[0] + "\n还没找到钻石手：发 /diamond go 立即找一轮（大约 20~40 分钟）"
        L += await compare_sources(self.app, PREFIX)
        return "\n".join(L)

    def start(self) -> bool:
        if self.task and not self.task.done():
            return False

        async def go() -> None:
            from .sources.gmgn import LOW
            LOW.set(True)                  # 后台研究：只用空闲的 GMGN 额度
            n = 0
            for chain in [c for c in ("solana", "bsc") if c in self.cfg.enabled_chains]:
                try:
                    n += await self.collect(chain)
                except Exception as e:  # noqa: BLE001
                    log.warning("找钻石手失败 %s: %s", chain, e)
            msgs = await self.train()
            tg = getattr(self.app, "tg", None)
            if tg:
                await tg.send(f"💎 <b>{PREFIX}</b>：新找到 {n} 笔钻石手，重新学了一次\n" + html.escape("\n".join(msgs))
                              + "\n发 /diamond 看学到了什么", low=True)
        self.task = asyncio.create_task(go())
        return True

    async def run(self) -> None:
        m = await self.app.db.kv_get("diamond_models", {}) or {}
        self.m = m if isinstance(m, dict) else {}
        await asyncio.sleep(600)
        while True:
            last = max((v.get("trained_at", 0) for v in self.m.values()), default=0)
            if time.time() - last > float(self.c()["retrain_hours"]) * 3600 and self.start():
                await self.task
            await asyncio.sleep(3600)


def _fmt(k: str, v: float) -> str:
    if k in ("mcap", "liquidity", "dev_best_mcap"):
        return fmt_usd(v)
    if k in ("chg60", "run_up"):
        return f"{v * 100:+.0f}%"
    return f"{v:.0f}"
