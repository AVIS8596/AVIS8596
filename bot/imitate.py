"""学来的打法（/imitate）：机器人从 GMGN 盈利地址的真实交易里，自己学"它们怎么买、怎么卖"，在模拟盘里按学到的打法下单，
和现有的买入来源（跟单 / 新币 / 早期建仓 / 强叙事）分开记账对比。不是人写的规则，每天用新数据重新学。

数据：/autopsy（每天自动跑一次）存下的每笔仓位 autopsy_trades——它们买入那一刻币的样子（K 线：发币多久、市值、
买前 5/15/60 分钟涨跌、1 小时内已拉多少、离高点多远、成交额；机器人当时的记录：持有人、前 10、捆绑、聪明钱、流动性、
推特、评分），以及从买入到卖出的价格走势（最高涨到多少、卖的时候赚多少、从最高点回撤多少、拿了多久、第一次卖多少）。

学买入：
  1. 它们出手的范围：发币多久、市值、1 小时内已经拉了多少、买前 1 小时涨跌——取它们全部买入的 5%~95% 区间，
     范围外的币不看（它们不碰的币，学不到怎么判断）；
  2. 范围内哪些买入"晚 30 秒照样能赚 15% 以上"（按我们能做到的速度算，不是按它们自己的成交价）：
     用和选币模型同一套的提升树学出打分，按时间切 70% 训练 / 30% 检验，检验合格（AUC ≥ 0.6、
     分数线以上平均赚 5% 以上）才下单，分数线也在检验集上定。
学卖出（只看它们赚钱的单 / 亏钱的单各自怎么走）：
  止盈 = 赚钱的单第一次卖出时的中位涨幅、第一次卖出的中位比例；止损 = 亏钱的单卖出时的中位亏幅；
  移动止损 = 赚钱的单最高涨幅的一半开始、按卖出时离最高点的中位回撤；最长持有 = 赚钱的单持有时间的 75% 分位。
执行：机器人每分析一个币（新币 / 早期 / 聪明钱信号都算），先用不花额度的数据看在不在范围里，在的话查 1 次 K 线算出
"现在这一刻"的样子并打分，过分数线就按"学来的打法（实验仓）"买入（照样过全部防阴谋盘风控、¼ 仓位、每天有上限），
卖出用学到的卖出规则（不用现在的全局卖出规则）。
对比：/imitate 看学到了什么、它自己的单子和其他来源同期比赚不赚，再用 K 线回放：把机器人最近的真实入场分别按
"现在的卖出规则"和"学来的卖出规则"重放，看哪套卖得好。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from .autopsy_learn import kline_features
from .model import auc, fit, score
from .analysis.intel import INTEL_KEYS
from .utils import f

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.imitate")

PREFIX = "学来的打法"
NAMES = ["age_min", "mcap", "chg5", "chg15", "chg60", "run_up", "dd", "vol5", "holders", "top10_pct",
         "bundle_held_pct", "smart", "liquidity", "has_x", "score",
         # 11-12 加的背景信息：项目方历史战绩、大户质量、Telegram 社区（新数据才有，旧数据留空）
         *INTEL_KEYS]
LABELS = {"age_min": "发币多久", "mcap": "市值", "chg5": "前 5 分钟涨跌", "chg15": "前 15 分钟涨跌",
          "chg60": "前 1 小时涨跌", "run_up": "1 小时内已拉", "dd": "离高点回撤", "vol5": "前 5 分钟成交额",
          "holders": "持有人", "top10_pct": "前 10 持仓", "bundle_held_pct": "捆绑", "smart": "聪明钱数",
          "liquidity": "流动性", "has_x": "推特", "score": "代币评分", "dev_tokens_n": "项目方发过几个币",
          "dev_best_mcap": "项目方最好的币市值", "dev_good_n": "项目方做到 50 万的币", "whale_n": "大户数",
          "smart_hold_n": "前 20 里聪明钱", "fresh_top_n": "前 20 里新钱包", "same_funder_n": "同一出资人最大一伙",
          "dev_dead_n": "项目方死掉的币", "old_top_n": "前 20 里老钱包", "sus_top_n": "前 20 里可疑钱包",
          "tg_members": "TG 人数", "tg_growth_h": "TG 每小时增长"}
GATE_KEYS = ("age_min", "mcap", "run_up", "chg60")
GOOD = 0.15                # 晚 30 秒跟进、扣成本后赚 15% 以上算"好的买入"
DEFAULTS = {"enabled": True, "min_rows": 120, "min_pos": 15, "max_per_day": 20, "max_evals_per_hour": 60,
            "retrain_hours": 24, "autopsy_top": 20}


def _q(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))] if s else 0.0


def _med(xs: list[float]) -> float:
    return _q(xs, 0.5)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def vec(x: dict) -> list[float | None]:
    return [None if x.get(k) is None else f(x.get(k)) for k in NAMES]


def learn_gate(rows: list[dict]) -> dict[str, tuple[float, float]]:
    """它们出手的范围：每个关键特征取全部买入的 5%~95% 区间（样本少于 30 的特征不设范围）。"""
    out = {}
    for k in GATE_KEYS:
        xs = [f(r["x"][k]) for r in rows if r["x"].get(k) is not None]
        if len(xs) >= 30:
            out[k] = (_q(xs, 0.05), _q(xs, 0.95))
    return out


def in_gate(x: dict, gate: dict[str, tuple[float, float]]) -> str:
    """不在范围里返回原因，在范围里返回空。缺数据的特征不拦。"""
    for k, (lo, hi) in gate.items():
        v = x.get(k)
        if v is not None and not lo <= f(v) <= hi:
            return f"{LABELS.get(k, k)} 不在它们出手的范围"
    return ""


def learn_exit(rows: list[dict]) -> dict:
    """从它们赚钱 / 亏钱的单子怎么卖，学出一套卖出规则（exit 配置的格式）。数据不够返回 {}。"""
    win = [r["x"] for r in rows if r["own"] > 0 and r["x"].get("ex_sell_gain") is not None]
    lose = [r["x"] for r in rows if r["own"] < 0 and r["x"].get("ex_sell_gain") is not None]
    if len(win) < 15 or len(lose) < 10:
        return {}
    tp = _clamp(_med([f(x["ex_sell_gain"]) for x in win]), 0.15, 3.0)
    frac = _clamp(_med([f(x.get("ex_first_frac") or 1) for x in win]), 0.3, 1.0)
    stop = _clamp(_med([f(x["ex_sell_gain"]) for x in lose]), -0.5, -0.1)
    peaks = [f(x["ex_peak_gain"]) for x in win if x.get("ex_peak_gain") is not None]
    dds = [f(x["ex_dd_at_sell"]) for x in win if x.get("ex_dd_at_sell") is not None]
    holds = [f(x["ex_hold_min"]) for x in win if x.get("ex_hold_min") is not None]
    rules: dict[str, Any] = {
        "stop_loss_pct": round(stop * 100, 1),
        "take_profit": [{"gain_pct": round(tp * 100, 1), "sell_pct": round(frac * 100)},
                        {"gain_pct": round(tp * 200, 1), "sell_pct": 100}],
        "trailing_start_pct": round(_clamp(_med(peaks) / 2, 0.15, 2.0) * 100, 1) if len(peaks) >= 10 else 10000,
        "trailing_drop_pct": round(_clamp(_med(dds), 0.08, 0.5) * 100, 1) if len(dds) >= 10 else 30,
        "max_hold_hours": round(_clamp(_q(holds, 0.75) / 60, 0.25, 24), 2) if holds else 6,
        "stall_hours": 0, "runner": {"enabled": False}, "recover_cost_only": False,
        "_n": [len(win), len(lose)],
    }
    return rules


def train_entry(rows: list[dict], gate: dict, min_rows: int, min_pos: int) -> dict:
    """范围内的买入：晚 30 秒照样赚 15% 以上 = 好。按时间 70% 训练 / 30% 检验；检验合格才用。"""
    data = sorted((r for r in rows if not in_gate(r["x"], gate) and 30 in r["fol"]), key=lambda r: r["t"])
    y_all = [int(r["fol"][30] >= GOOD) for r in data]
    if len(data) < min_rows or sum(y_all) < min_pos:
        return {"ok": False, "why": f"样本 {len(data)} 笔（要 ≥ {min_rows}），其中好的买入 {sum(y_all)} 笔（要 ≥ {min_pos}）"}
    cut = int(len(data) * 0.7)
    X, Xv = [vec(r["x"]) for r in data[:cut]], [vec(r["x"]) for r in data[cut:]]
    y, yv = y_all[:cut], y_all[cut:]
    rv = [r["fol"][30] for r in data[cut:]]
    m = fit(X, y, Xv, yv, rounds=200, nb=10, min_h=2.0)
    pv = [score(m, x) for x in Xv]
    a = auc(pv, yv)
    order = sorted(range(len(pv)), key=lambda i: -pv[i])
    best_cut, best_sum, acc, cnt = None, 0.0, 0.0, 0
    for i in order:
        acc += rv[i]
        cnt += 1
        if cnt >= max(8, len(pv) // 10) and acc / cnt >= 0.05 and acc > best_sum:
            best_sum, best_cut = acc, pv[i]
    above = [rv[i] for i in range(len(pv)) if best_cut is not None and pv[i] >= best_cut]
    imp = sorted(((v, NAMES[j]) for j, v in enumerate(m["imp"]) if v > 0), reverse=True)[:5]
    tot = sum(v for v, _ in imp) or 1
    ok = a >= 0.6 and best_cut is not None
    return {"ok": ok, "why": "" if ok else (f"检验不合格：AUC {a:.2f}（要 ≥ 0.6，分不出好坏）" if a < 0.6
                                            else "检验不合格：找不到平均赚 5% 以上的分数线"),
            "model": m, "cut": best_cut, "auc": round(a, 3), "n": len(data), "n_val": len(pv),
            "pos_rate": round(sum(y_all) / len(data), 3), "val_avg": round(sum(rv) / len(rv), 4) if rv else 0.0,
            "pick_avg": round(sum(above) / len(above), 4) if above else 0.0, "pick_n": len(above),
            "imp": [(LABELS.get(n, n), round(v / tot * 100)) for v, n in imp]}


class Imitator:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.m: dict[str, dict] = {}            # 链 → {"gate", "entry", "exit", "trained_at", "rows"}
        self._seen: dict[str, float] = {}
        self._evals: list[float] = []
        self._tasks: set[asyncio.Task] = set()

    def c(self) -> dict:
        return {**DEFAULTS, **(self.cfg.get("imitate", {}) or {})}

    async def load(self) -> None:
        m = await self.app.db.kv_get("imitate_models", {}) or {}
        self.m = m if isinstance(m, dict) else {}

    async def rows(self, chain: str, days: float = 14) -> list[dict]:
        db = self.app.db
        await db.execute("CREATE TABLE IF NOT EXISTS autopsy_trades (chain TEXT, wallet TEXT, token TEXT, symbol TEXT, "
                         "t_buy REAL, own REAL, hold_min REAL, x TEXT, fol TEXT, PRIMARY KEY(chain, wallet, token, t_buy))")
        out = []
        for r in await db.fetchall("SELECT t_buy, own, x, fol FROM autopsy_trades WHERE chain=? AND t_buy>?",
                                   (chain, time.time() - days * 86400)):
            try:
                x = json.loads(r["x"] or "{}") or {}
                fol = {int(k): float(v) for k, v in (json.loads(r["fol"] or "{}") or {}).items()}
            except (TypeError, ValueError):
                continue
            out.append({"t": f(r["t_buy"]), "own": f(r["own"]), "x": x, "fol": fol})
        return out

    async def train(self) -> list[str]:
        c = self.c()
        out = []
        for chain in self.cfg.enabled_chains:
            rows = await self.rows(chain)
            if not rows:
                continue
            gate = learn_gate(rows)
            ent = train_entry(rows, gate, int(c["min_rows"]), int(c["min_pos"]))
            ext = learn_exit(rows)
            self.m[chain] = {"gate": gate, "entry": ent, "exit": ext, "trained_at": time.time(), "rows": len(rows)}
            out.append(f"{chain}：{len(rows)} 笔样本，买入{'可用' if ent.get('ok') else '不可用（' + ent.get('why', '') + '）'}，"
                       f"卖出{'学到了' if ext else '数据不够'}")
        await self.app.db.kv_set("imitate_models", self.m)
        return out

    def ready(self, chain: str) -> bool:
        m = self.m.get(chain) or {}
        return bool(self.c().get("enabled")) and bool((m.get("entry") or {}).get("ok")) and bool(m.get("exit"))

    def exit_rules(self, chain: str, base: dict) -> dict | None:
        """这条链学到的卖出规则（exit 配置格式），叠在全局配置上（只换止盈止损、移动止损、持有时间这几项）。"""
        ext = (self.m.get(chain) or {}).get("exit") or {}
        if not ext:
            return None
        b = {k: v for k, v in (base or {}).items() if k not in ("tiers", "dynamic", "learned")}
        return {**b, **{k: v for k, v in ext.items() if not k.startswith("_")}}

    # ---------------- 实时：机器人每分析一个币就看一眼
    async def on_analysis(self, chain: str, token: str, rep: dict) -> None:
        if not self.ready(chain) or rep.get("risks"):
            return
        now = time.time()
        if now - self._seen.get(f"{chain}:{token}", 0) < 1800:
            return
        x0 = {"age_min": f(rep.get("age_hours")) * 60 if rep.get("age_hours") is not None else None,
              "mcap": rep.get("mcap")}
        if in_gate(x0, self.m[chain]["gate"]):
            return                                       # 不花额度就知道不在范围里
        self._evals = [t for t in self._evals if now - t < 3600]
        if len(self._evals) >= int(self.c()["max_evals_per_hour"]):
            return
        self._seen[f"{chain}:{token}"] = now
        if len(self._seen) > 20000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 1800}
        self._evals.append(now)
        t = asyncio.create_task(self._eval(chain, token, rep, x0))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def features_now(self, candles: list[dict], rep: dict, x0: dict, now: float) -> dict:
        x = kline_features(candles, now + 31, None, None, f(rep.get("price")))   # +31：把最新一根 K 线也算进"之前"
        x.update(x0)
        for k in ("holders", "top10_pct", "bundle_held_pct", "liquidity", "score", *INTEL_KEYS):
            if rep.get(k) is not None:
                x[k] = f(rep[k])
        x["smart"] = len(rep.get("smart_buyers") or [])
        if rep.get("has_x") is not None:
            x["has_x"] = int(bool(rep.get("has_x")))
        return x

    async def _eval(self, chain: str, token: str, rep: dict, x0: dict) -> None:
        try:
            now = time.time()
            cs = await self.app.gmgn.kline(chain, token, "1m", int(now - 3900), int(now)) or []
            x = self.features_now(cs, rep, x0, now)
            m = self.m[chain]
            if in_gate(x, m["gate"]):
                return
            p = score(m["entry"]["model"], vec(x))
            if p < float(m["entry"]["cut"]):
                return
            db = self.app.db
            n = await db.fetchone("SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?",
                                  (PREFIX + "%", self.app.day_start()))
            if n and n["n"] >= int(self.c()["max_per_day"]):
                return
            reason = f"{PREFIX}（实验仓，分数 {p:.2f}）"
            log.info("%s 买入候选 %s：%s", PREFIX, rep.get("symbol"), reason)
            await self.app.hub.confirm_and_buy(chain, token, rep, reason, wallets=rep.get("smart_buyers") or [],
                                               tier="normal")
        except Exception as e:  # noqa: BLE001
            log.debug("学来的打法评估失败 %s: %s", token, e)

    # ---------------- 报告：学到了什么 + 自己的成绩 + 和其他来源 / 现在的卖出规则对比
    async def text(self) -> str:
        L = [f"🎓 <b>{PREFIX}</b>（从 GMGN 盈利地址的交易里自己学的买卖方式，模拟盘 ¼ 仓位单独记账）"]
        if not self.m:
            return L[0] + "\n还没学过：先跑一次 /autopsy go 收集数据，跑完会自动学"
        for chain, m in self.m.items():
            ent, ext = m.get("entry") or {}, m.get("exit") or {}
            L.append(f"<b>{chain.upper()}</b>：{m.get('rows', 0)} 笔样本，"
                     f"{time.strftime('%m-%d %H:%M', time.localtime(m.get('trained_at', 0)))} 学的")
            if m.get("gate"):
                L.append("  出手范围：" + "、".join(f"{LABELS[k]} {_fmt(k, lo)}~{_fmt(k, hi)}" for k, (lo, hi) in m["gate"].items()))
            if ent.get("ok"):
                L.append(f"  买入判断：检验集 AUC {ent['auc']:.2f}，分数 ≥ {ent['cut']:.2f} 的 {ent['pick_n']} 笔"
                         f"晚 30 秒跟进平均 {ent['pick_avg'] * 100:+.0f}%（检验集全部 {ent['val_avg'] * 100:+.0f}%）；"
                         "最看重：" + "、".join(f"{n} {v}%" for n, v in ent.get("imp", [])))
            else:
                L.append(f"  买入判断：还不能用（{ent.get('why', '没训练')}），这条链暂不下单")
            if ext:
                tp = ext["take_profit"][0]
                L.append(f"  卖出规则：涨 {tp['gain_pct']:+.0f}% 卖 {tp['sell_pct']:.0f}%、涨 {ext['take_profit'][1]['gain_pct']:+.0f}% 卖完；"
                         f"止损 {ext['stop_loss_pct']:.0f}%；"
                         + (f"最高涨过 {ext['trailing_start_pct']:+.0f}% 后回撤 {ext['trailing_drop_pct']:.0f}% 卖；"
                            if ext["trailing_start_pct"] < 10000 else "")
                         + f"最长拿 {ext['max_hold_hours']:g} 小时（学自赚钱的单 {ext['_n'][0]} 笔、亏钱的单 {ext['_n'][1]} 笔）")
        L += await compare_sources(self.app, PREFIX)
        # ---- 卖出规则对比：机器人最近的真实入场，按两套卖出规则回放
        try:
            L += await self.exit_compare()
        except Exception as e:  # noqa: BLE001
            log.debug("卖出回放失败: %s", e)
        return "\n".join(L)

    async def exit_compare(self, days: float = 3) -> list[str]:
        from .trade.replay import collect, replay_one
        rules = {c: self.exit_rules(c, self.cfg.get("exit", {}) or {}) for c in self.m}
        if not any(rules.values()):
            return []
        trades, _ = await collect(self.app, days)
        trades = [t for t in trades if rules.get(t["p"]["chain"])]
        if len(trades) < 5:
            return []
        cost = 0.02
        cur = [replay_one(t, self.cfg.get("exit", {}) or {}, None, False, cost) - 1 for t in trades]
        new = [replay_one(t, self.cfg.get("exit", {}) or {}, rules[t["p"]["chain"]], False, cost) - 1 for t in trades]
        better = sum(1 for a, b in zip(cur, new) if b > a + 1e-9)
        return [f"<b>卖出规则回放</b>（机器人最近 {days:g} 天 {len(trades)} 笔真实入场，K 线重放）",
                f"  现在的卖出规则：每笔平均 {sum(cur) / len(cur) * 100:+.1f}%、中位 {_med(cur) * 100:+.0f}%",
                f"  学来的卖出规则：每笔平均 {sum(new) / len(new) * 100:+.1f}%、中位 {_med(new) * 100:+.0f}%"
                f"（更好 {better} 笔 / 更差 {sum(1 for a, b in zip(cur, new) if b < a - 1e-9)} 笔）"]

    async def run(self) -> None:
        """启动时先用已有数据学一次；之后每天自动解剖一批盈利地址（收集新数据）再重新学。"""
        await self.load()
        await asyncio.sleep(180)
        while True:
            try:
                if not self.m or time.time() - max((m.get("trained_at", 0) for m in self.m.values()), default=0) \
                        > float(self.c()["retrain_hours"]) * 3600:
                    ap = getattr(self.app, "autopsy", None)
                    last = await self.app.db.kv_get("autopsy_report", None)
                    busy = ap is not None and ap.task is not None and not ap.task.done()     # 你手动发的 /autopsy 正在跑
                    stale = not last or time.time() - f(last.get("ts")) > 20 * 3600
                    if not stale:      # 11-12 第一次解剖时还没记"怎么卖"的数据：学不了卖出，马上补跑一次
                        r = await self.app.db.fetchone("SELECT COUNT(*) n FROM autopsy_trades WHERE x LIKE '%ex_sell_gain%'")
                        stale = not r or not r["n"]
                    if ap and not busy and stale:
                        chains = [c for c in ("solana", "bsc") if c in self.cfg.enabled_chains]
                        await ap.run(chains, int(self.c()["autopsy_top"]))      # 收集新数据（不发报告，/autopsy 可看）
                    msgs = await self.train()
                    tg = getattr(self.app, "tg", None)
                    if tg and msgs:
                        await tg.send(f"🎓 <b>{PREFIX}</b> 重新学了一次：\n" + html.escape("\n".join(msgs))
                                      + "\n发 /imitate 看学到了什么、成绩怎么样", low=True)
            except Exception as e:  # noqa: BLE001
                log.warning("学来的打法训练失败: %s", e)
            await asyncio.sleep(3600)


def _fmt(k: str, v: float) -> str:
    if k == "mcap":
        from .utils import fmt_usd
        return fmt_usd(v)
    if k in ("run_up", "chg60", "chg5", "chg15", "dd"):
        return f"{v * 100:+.0f}%"
    return f"{v:.0f}"


async def compare_sources(app: "App", prefix: str) -> list[str]:
    """这个打法自己的单子 vs 同期其他买入来源（已平仓、扣过成本）。"""
    from .trade.sizing import source_of
    db = app.db
    dry = int(bool(app.cfg.dry_run))
    mine = await db.fetchall("SELECT opened_at, status FROM positions WHERE reason LIKE ? AND dry_run=? ORDER BY opened_at",
                             (prefix + "%", dry))
    if not mine:
        return [f"还没有按{prefix}买过（买入判断检验合格、碰到合适的币才会买）"]
    since = f(mine[0]["opened_at"])
    allp = await db.fetchall("SELECT reason, status, cost_usd, realized_usd FROM positions WHERE opened_at>=? "
                             "AND dry_run=? AND status='closed'", (since, dry))
    by: dict[str, list[float]] = {}
    for p in allp:
        c = f(p["cost_usd"])
        if c > 0:
            by.setdefault(source_of(p["reason"]), []).append(f(p["realized_usd"]) / c - 1)
    L = [f"<b>成绩对比</b>（{time.strftime('%m-%d %H:%M', time.localtime(since))} 起，已平仓，扣过成本）"]
    for src, rs in sorted(by.items(), key=lambda kv: (kv[0] != prefix, -len(kv[1]))):
        L.append(f"  {'👉 ' if src == prefix else ''}{src}：{len(rs)} 笔，每笔平均 {sum(rs) / len(rs) * 100:+.1f}%、"
                 f"中位 {_med(rs) * 100:+.0f}%、赚钱 {sum(1 for r in rs if r > 0) / len(rs) * 100:.0f}%")
    n_open = sum(1 for p in mine if p["status"] == "open")
    if n_open:
        L.append(f"  {prefix}还有 {n_open} 笔持仓中")
    return L
