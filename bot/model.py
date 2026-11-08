"""机器人自己的"选币模型"：从几千个真实结果里学"什么样的币之后会大涨"，目标是不靠跟单，自己当聪明钱。

两个模型：
- 分析模型（A）：每个被完整分析过的币，用分析当时的数据（评分、流动性、市值、持有人、成交、涨幅、买卖比、筹码、
  聪明钱 / 阴谋地址 / 早期发现者占比……）预测"之后会不会大涨"。
- 早期模型（B）：快毕业的币每 2.5 分钟一份快照，用快照数据加"走势"（持有人每分钟增长、成交增长、
  从第一次看到起涨了多少、上线多久）预测——目标是比评分更早发现好币。

学习方法：梯度提升决策树桩（纯 Python 实现，不用装额外的库），标签是"之后 4 小时按止损 -35% 近似算，
扣成本后赚 50% 以上"。按时间切开：前 70% 训练、后 30% 检验，用后 30% 决定"多高的分数才值得买"。
检验不合格（区分能力 AUC < 0.6，或者找不到"高分组平均赚 10% 以上、至少 15 个样本"的分数线）就不选币。

上线流程：影子模式（只记录"模型会买什么"，之后看结果）→ 模型选的币累计 30 个有结果、平均赚 10% 以上、
中位不亏，模拟盘自动让它真正下单（照样过全部风控和确认期）→ 之后最近 30 个选择平均转亏，自动退回影子模式。
实盘永远不自动启用。每天用最新数据重训一次。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import time
from bisect import bisect_left
from typing import TYPE_CHECKING, Any

from .brain import proxy_return
from .utils import f

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.model")

WIN = 0.5                 # 标签：近似收益 ≥ +50% 算"大涨"
CHAIN_IDX = {"solana": 0, "bsc": 1, "ethereum": 2, "base": 3, "robinhood": 4}

FEATS_A = ["评分", "流动性", "市值", "持有人", "1小时成交", "1小时涨幅", "上线小时", "1小时买单", "1小时卖单", "买卖比",
           "成交/市值", "流动性/市值", "每小时新增持有人", "前10持仓", "捆绑持仓", "埋伏持仓", "dev持仓", "聪明钱个数",
           "阴谋地址持仓", "早期发现者持仓", "精准钱包持仓", "风险项", "链", "来源", "项目方发币数", "项目方已卖光",
           "LP已销毁", "团伙成员数"]
FEATS_B = ["持有人", "成交额", "跑路风险", "前10", "捆绑", "老鼠仓", "dev", "刷量", "聪明钱", "KOL", "上线分钟",
           "持有人每分钟增长%", "成交每分钟增长%", "比第一次看到涨了%", "已观察分钟", "每分钟新增持有人", "链"]


def src_cat(source: str) -> int:
    s = str(source or "")
    if s.startswith("smart"):
        return 1
    if s.startswith("早期"):
        return 2
    if s.startswith("强叙事"):
        return 3
    if "毕业" in s or "新币" in s or "pump" in s.lower():
        return 0
    return 4


def feats_a(ft: dict, chain: str, source: str) -> list[float | None]:
    def g(k: str) -> float | None:
        v = ft.get(k)
        return None if v is None else f(v)
    mcap, liq, vol = g("mcap") or 0, g("liquidity") or 0, g("volume_1h") or 0
    buys, sells, age, hold = g("buys_1h") or 0, g("sells_1h") or 0, g("age_hours") or 0, g("holders") or 0
    return [g("score"), liq, mcap, hold, vol, g("change_1h"), age, buys, sells,
            buys / (buys + sells) if buys + sells else None, vol / mcap if mcap else None,
            liq / mcap if mcap else None, hold / max(age, 0.05) if hold else None,
            g("top10_pct"), g("bundle_held_pct"), g("insider_held_pct"), g("dev_held_pct"), g("smart"),
            g("cabal_pct"), g("early_finder_pct"), g("winner_pct"), g("risks"),
            float(CHAIN_IDX.get(chain, 9)), float(src_cat(source)), g("dev_launches"), g("dev_sold"), g("lp_burned"), g("gang_n")]


def feats_b(s: dict, prev: dict | None, first: dict | None) -> list[float | None]:
    now, created = f(s.get("ts")), f(s.get("created"))
    age = (now - created) / 60 if created else None
    hg = vg = None
    if prev and now - f(prev.get("ts")) > 30:
        mins = (now - f(prev["ts"])) / 60
        if f(prev.get("holders")) > 0:
            hg = (f(s.get("holders")) / f(prev["holders"]) - 1) * 100 / mins
        if f(prev.get("volume")) > 0:
            vg = (f(s.get("volume")) / f(prev["volume"]) - 1) * 100 / mins
    pc = (f(s.get("price")) / f(first["price"]) - 1) * 100 if first and f(first.get("price")) > 0 else None
    obs = (now - f(first["ts"])) / 60 if first else 0.0
    return [f(s.get("holders")), f(s.get("volume")), f(s.get("rug")), f(s.get("top10")), f(s.get("bundler")),
            f(s.get("insider")), f(s.get("creator")), f(s.get("wash")), f(s.get("smart")), f(s.get("kol")), age, hg, vg,
            pc, obs, f(s.get("holders")) / max(age, 1) if age else None, float(CHAIN_IDX.get(s.get("chain"), 9))]


# ============================================================ 梯度提升（决策树桩，直方图加速）
def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def edges_of(col: list[float | None], nb: int) -> list[float]:
    vals = sorted(v for v in col if v is not None)
    if not vals:
        return []
    qs = sorted({vals[min(len(vals) - 1, int(len(vals) * k / nb))] for k in range(1, nb)})
    return qs


def binize(v: float | None, edges: list[float]) -> int:
    return 0 if v is None else 1 + bisect_left(edges, v)     # 0 = 缺失；分箱 ≤ b 等价于 v ≤ edges[b-1]


def fit(X: list[list[float | None]], y: list[int], Xv: list[list[float | None]], yv: list[int],
        rounds: int = 300, lr: float = 0.08, nb: int = 16, lam: float = 2.0, min_h: float = 3.0) -> dict:
    n, nf = len(X), len(X[0])
    edges = [edges_of([r[j] for r in X], nb) for j in range(nf)]
    cols = [[binize(r[j], edges[j]) for r in X] for j in range(nf)]
    nbin = [len(e) + 2 for e in edges]
    p0 = min(max(sum(y) / n, 1e-3), 1 - 1e-3)
    base = math.log(p0 / (1 - p0))
    F = [base] * n
    Fv = [base] * len(Xv)
    stumps: list[tuple[int, float, float, float, int]] = []    # 特征、阈值（分箱号）、左、右、缺失走左/右
    best_loss, best_k = float("inf"), 0
    imp = [0.0] * nf

    def logloss(Fs: list[float], ys: list[int]) -> float:
        return -sum(math.log(max(_sig(s) if t else 1 - _sig(s), 1e-9)) for s, t in zip(Fs, ys)) / max(len(ys), 1)

    for k in range(rounds):
        p = [_sig(v) for v in F]
        g = [pi - yi for pi, yi in zip(p, y)]
        h = [max(pi * (1 - pi), 1e-6) for pi in p]
        gt, ht = sum(g), sum(h)
        best = None
        for j in range(nf):
            G = [0.0] * nbin[j]
            H = [0.0] * nbin[j]
            for b, gi, hi in zip(cols[j], g, h):
                G[b] += gi
                H[b] += hi
            gm, hm = G[0], H[0]                     # 缺失值单独算，跟着更合适的一边
            GL = HL = 0.0
            for b in range(1, nbin[j] - 1):
                GL += G[b]
                HL += H[b]
                GR, HR = gt - gm - GL, ht - hm - HL
                for miss_left in (True, False):
                    gl, hl = (GL + gm, HL + hm) if miss_left else (GL, HL)
                    gr, hr = (GR, HR) if miss_left else (GR + gm, HR + hm)
                    if hl < min_h or hr < min_h:
                        continue
                    gain = gl * gl / (hl + lam) + gr * gr / (hr + lam) - gt * gt / (ht + lam)
                    if not best or gain > best[0]:
                        best = (gain, j, b, -gl / (hl + lam) * lr, -gr / (hr + lam) * lr, miss_left)
        if not best or best[0] <= 1e-6:
            break
        gain, j, b, wl, wr, ml = best
        imp[j] += gain
        thr = edges[j][b - 1] if b - 1 < len(edges[j]) else float("inf")
        stumps.append((j, thr, wl, wr, int(ml)))
        for i, bb in enumerate(cols[j]):
            F[i] += (wl if ml else wr) if bb == 0 else (wl if bb <= b else wr)
        for i, r in enumerate(Xv):
            v = r[j]
            Fv[i] += (wl if ml else wr) if v is None else (wl if v <= thr else wr)
        if Xv and (k % 5 == 4 or k == rounds - 1):
            loss = logloss(Fv, yv)
            if loss < best_loss - 1e-5:
                best_loss, best_k = loss, len(stumps)
            elif len(stumps) - best_k >= 40:        # 检验集 40 轮没变好就停（防止背答案）
                break
    if Xv and best_k:
        stumps = stumps[:best_k]
    return {"base": base, "stumps": [(j, thr, wl, wr) for j, thr, wl, wr, _ in stumps],
            "miss_dir": [ml for *_, ml in stumps], "imp": imp, "rounds": len(stumps)}


def score(model: dict, x: list[float | None]) -> float:
    s = model["base"]
    for (j, thr, wl, wr), ml in zip(model["stumps"], model["miss_dir"]):
        v = x[j]
        s += (wl if ml else wr) if v is None else (wl if v <= thr else wr)
    return _sig(s)


def auc(ps: list[float], ys: list[int]) -> float:
    pos = sum(ys)
    neg = len(ys) - pos
    if not pos or not neg:
        return 0.5
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    rank_sum, i = 0.0, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and ps[order[j + 1]] == ps[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        rank_sum += sum(r for k in range(i, j + 1) if ys[order[k]])
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def build(rows: list[tuple[float, list[float | None], float]], names: list[str], min_rows: int = 800,
          min_pos: int = 40) -> dict:
    """rows: (时间, 特征, 近似收益)。返回模型（含检验结果）；数据不够返回 {"ok": False, "why": ...}。"""
    rows = sorted(rows, key=lambda r: r[0])
    y_all = [int(r[2] >= WIN) for r in rows]
    if len(rows) < min_rows or sum(y_all) < min_pos:
        return {"ok": False, "why": f"样本 {len(rows)} 个（要 ≥ {min_rows}），大涨的 {sum(y_all)} 个（要 ≥ {min_pos}）"}
    cut = int(len(rows) * 0.7)
    X, Xv = [r[1] for r in rows[:cut]], [r[1] for r in rows[cut:]]
    y, yv = y_all[:cut], y_all[cut:]
    rets_v = [r[2] for r in rows[cut:]]
    m = fit(X, y, Xv, yv)
    pv = [score(m, x) for x in Xv]
    a = auc(pv, yv)
    # 用检验集找分数线：高于这条线的币平均要赚 10% 以上、至少 15 个；在满足条件里选合计赚得最多的
    order = sorted(range(len(pv)), key=lambda i: -pv[i])
    best_cut, best_sum, acc, cnt = None, 0.0, 0.0, 0
    min_n = max(15, int(len(pv) * 0.02))
    for i in order:
        acc += rets_v[i]
        cnt += 1
        if cnt >= min_n and acc / cnt >= 0.10 and acc > best_sum:
            best_sum, best_cut = acc, pv[i]
    above = [rets_v[i] for i in range(len(pv)) if best_cut is not None and pv[i] >= best_cut]
    base_avg = sum(rets_v) / len(rets_v)
    deciles = []
    for q in range(5):          # 五档：每档平均收益（检验集）
        idx = order[q * len(order) // 5:(q + 1) * len(order) // 5]
        if idx:
            deciles.append(round(sum(rets_v[i] for i in idx) / len(idx), 4))
    imp = sorted(((v, names[j]) for j, v in enumerate(m["imp"]) if v > 0), reverse=True)[:6]
    tot = sum(v for v, _ in imp) or 1
    ok = a >= 0.6 and best_cut is not None
    return {"ok": ok, "why": "" if ok else (f"检验不合格：AUC {a:.2f}（要 ≥ 0.6）" if a < 0.6 else
                                            "检验不合格：找不到平均赚 10% 以上的分数线"),
            "model": m, "cut": best_cut, "auc": round(a, 3), "n": len(rows), "n_val": len(rows) - cut,
            "pos_rate": round(sum(y_all) / len(rows), 4), "val_avg": round(base_avg, 4),
            "pick_avg": round(sum(above) / len(above), 4) if above else 0.0, "pick_n": len(above),
            "pick_rate": round(len(above) / len(pv), 4) if pv else 0.0, "quintiles": deciles,
            "imp": [(n, round(v / tot * 100)) for v, n in imp], "trained_at": time.time()}


# ============================================================ 运行时
class SelfModel:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.m: dict[str, dict] = {}
        self.picked: dict[str, float] = {}
        self.last_train = 0.0

    async def load(self) -> None:
        for k in ("a", "b"):
            v = await self.app.db.kv_get(f"model_{k}", None)
            if v:
                self.m[k] = v
        self.last_train = max([v.get("trained_at", 0) for v in self.m.values()] + [0])

    async def mode(self) -> str:
        m = await self.app.db.kv_get("model_mode", None) or str(self.cfg.get("learning.model", "shadow"))
        return m if m in ("shadow", "on", "off") else "shadow"

    def ready(self, k: str) -> bool:
        v = self.m.get(k)
        return bool(v and v.get("ok") and v.get("cut") is not None)

    # ---------------- 训练
    async def dataset_a(self, days: float = 10) -> list[tuple[float, list, float]]:
        rows = await self.app.db.fetchall(
            "SELECT ts, chain, source, price, p15, p60, p240, features FROM token_outcomes WHERE ts>? AND price>0 "
            "AND p60>0", (time.time() - days * 86400,))
        out = []
        for r in rows:
            if str(r["source"] or "") == "whynot":
                continue
            ret = proxy_return(f(r["price"]), f(r["p60"]), f(r["p240"]) if f(r["p240"]) > 0 else None,
                               f(r["p15"]) if f(r["p15"]) > 0 else None)
            if ret is None:
                continue
            out.append((f(r["ts"]), feats_a(json.loads(r["features"] or "{}"), r["chain"], r["source"]), ret))
        return out

    async def dataset_b(self, days: float = 7) -> list[tuple[float, list, float]]:
        db = self.app.db
        toks = {(r["chain"], r["token"]): dict(r) for r in await db.fetchall(
            "SELECT chain, token, first_ts, p60, p240 FROM early_tok WHERE first_ts>? AND p240>0",
            (time.time() - days * 86400,))}
        if not toks:
            return []
        seqs: dict[tuple[str, str], list[dict]] = {}
        for r in await db.fetchall("SELECT * FROM early_obs WHERE ts>? ORDER BY ts", (time.time() - days * 86400 - 6 * 3600,)):
            k = (r["chain"], r["token"])
            if k in toks:
                seqs.setdefault(k, []).append(dict(r))
        out = []
        for k, ss in seqs.items():
            t = toks[k]
            first = ss[0]
            for i, s in enumerate(ss):
                if f(s["ts"]) - f(t["first_ts"]) > 30 * 60:     # 只用前 30 分钟的快照，结果价至少还在 30 分钟以后
                    break
                ret = proxy_return(f(s["price"]) * 1.02, f(t["p60"]) if f(t["p60"]) > 0 else None, f(t["p240"]))
                if ret is None:
                    continue
                out.append((f(s["ts"]), feats_b(s, ss[i - 1] if i else None, first), ret))
        return out

    async def train(self) -> list[str]:
        msgs = []
        for k, name, ds, names, mr, mp in (("a", "分析模型", self.dataset_a, FEATS_A, 800, 40),
                                           ("b", "早期模型", self.dataset_b, FEATS_B, 1500, 40)):
            rows = await ds()
            res = await asyncio.to_thread(build, rows, names, mr, mp)
            if "model" not in res:
                msgs.append(f"{name}：数据还不够（{res['why']}）")
                continue
            self.m[k] = res
            await self.app.db.kv_set(f"model_{k}", res)
            msgs.append(self.model_line(k))
        self.last_train = time.time()
        return msgs

    def model_line(self, k: str) -> str:
        v = self.m.get(k)
        name = "分析模型" if k == "a" else "早期模型"
        if not v:
            return f"{name}：还没训练"
        head = f"{name}（{time.strftime('%m-%d %H:%M', time.localtime(v['trained_at']))} 训练，{v['n']} 个样本）"
        if not v.get("ok"):
            return f"{head}：{v['why']}，暂不选币"
        q = "、".join(f"{x * 100:+.0f}%" for x in v["quintiles"])
        return (f"{head}：检验 AUC {v['auc']:.2f}；检验集里平均每个币 {v['val_avg'] * 100:+.1f}%，"
                f"模型高分组（约前 {v['pick_rate'] * 100:.0f}%）平均 {v['pick_avg'] * 100:+.1f}%（{v['pick_n']} 个）；"
                f"按分数五档从高到低：{q}；最看重：" + "、".join(f"{n} {p}%" for n, p in v["imp"][:4]))

    # ---------------- 实时打分
    async def on_analysis(self, chain: str, token: str, rep: dict, feats: dict, source: str) -> float | None:
        if not self.ready("a") or str(source) in ("whynot",) or str(source).startswith("模型"):
            return None
        v = self.m["a"]
        p = score(v["model"], feats_a(feats, chain, source))
        if p >= v["cut"]:
            await self._pick("a", chain, token, rep.get("symbol") or "?", p, float(rep.get("price") or 0), rep)
        return p

    async def on_early(self, s: dict, prev: dict | None, first: dict | None) -> float | None:
        if not self.ready("b"):
            return None
        v = self.m["b"]
        p = score(v["model"], feats_b(s, prev, first))
        if p >= v["cut"] and f(s.get("ts")) - f((first or s).get("ts")) <= 30 * 60:
            await self._pick("b", s["chain"], s["token"], "?", p, f(s.get("price")), None)
        return p

    async def _pick(self, k: str, chain: str, token: str, symbol: str, p: float, price: float, rep: dict | None) -> None:
        key = f"{chain}:{token}"
        now = time.time()
        if now - self.picked.get(key, 0) < 6 * 3600 or price <= 0:
            return
        self.picked[key] = now
        if len(self.picked) > 5000:
            self.picked = {kk: t for kk, t in self.picked.items() if now - t < 6 * 3600}
        mode = await self.mode()
        if mode == "off":
            return
        live = mode == "on" and self.cfg.dry_run          # 实盘永远不自动让模型下单
        if live:
            n = await self.app.db.fetchone("SELECT COUNT(*) n FROM model_picks WHERE acted=1 AND ts>?", (now - 3600,))
            if n and n["n"] >= int(self.cfg.get("learning.model_max_per_hour", 3)):
                live = False                                # 每小时最多下 3 笔，其余只记录
        await self.app.db.execute(
            "INSERT INTO model_picks(ts,chain,token,symbol,model,score,price,acted) VALUES(?,?,?,?,?,?,?,?)",
            (now, chain, token, symbol, k, p, price, int(live)))
        if not live:
            return
        name = "分析模型" if k == "a" else "早期模型"
        t = asyncio.create_task(self._buy(chain, token, rep, f"模型买入（{name}，分数 {p:.2f}）"))
        self.app.hub._tasks.add(t)
        t.add_done_callback(self.app.hub._tasks.discard)

    async def _buy(self, chain: str, token: str, rep: dict | None, reason: str) -> None:
        try:
            if rep is None:
                rep = await self.app.analyzer.analyze(chain, token, source="模型·早期")
                if not rep:
                    return
            await self.app.hub.confirm_and_buy(chain, token, rep, reason, wallets=rep.get("smart_buyers") or [])
        except Exception as e:  # noqa: BLE001
            log.warning("模型买入失败 %s: %s", token, e)

    # ---------------- 影子成绩
    async def fill_picks(self) -> None:
        db = self.app.db
        now = time.time()
        for col, mins in (("p60", 60), ("p240", 240)):
            rows = await db.fetchall(f"SELECT rowid, chain, token FROM model_picks WHERE {col} IS NULL AND ts<? AND ts>? "
                                     "LIMIT 60", (now - mins * 60, now - mins * 60 - 6 * 3600))
            by: dict[str, list[dict]] = {}
            for r in rows:
                by.setdefault(r["chain"], []).append(dict(r))
            for chain, lst in by.items():
                px = await self.app.data.prices(chain, list({r["token"] for r in lst}))
                await db.executemany(f"UPDATE model_picks SET {col}=? WHERE rowid=?",
                                     [(px.get(r["token"]) or -1, r["rowid"]) for r in lst])

    async def record(self, since: float = 0, k: str | None = None, last: int = 0) -> dict:
        q = "SELECT * FROM model_picks WHERE p240 IS NOT NULL AND p240>0 AND ts>=?"
        args: list[Any] = [since]
        if k:
            q += " AND model=?"
            args.append(k)
        q += " ORDER BY ts DESC"
        if last:
            q += f" LIMIT {int(last)}"
        rows = await self.app.db.fetchall(q, tuple(args))
        rs = sorted(r for r in (proxy_return(f(x["price"]), f(x["p60"]) if f(x["p60"]) > 0 else None, f(x["p240"]))
                                for x in rows) if r is not None)
        if not rs:
            return {"n": 0}
        return {"n": len(rs), "avg": sum(rs) / len(rs), "med": rs[len(rs) // 2],
                "win": sum(1 for r in rs if r > 0) / len(rs), "big": sum(1 for r in rs if r >= WIN)}

    async def promote_check(self) -> str:
        if not self.cfg.dry_run:
            return ""
        mode = await self.mode()
        since = min([v.get("trained_at", 0) for v in self.m.values() if v.get("ok")] or [time.time()])
        if mode == "shadow" and bool(self.cfg.get("learning.model_auto_promote", True)):
            r = await self.record(since=since - 3 * 86400)
            if r["n"] >= 30 and r["avg"] >= 0.10 and r["med"] >= 0:
                await self.app.db.kv_set("model_mode", "on")
                return (f"🤖 <b>模型通过考核</b>：影子期选了 {r['n']} 个有结果的币，近似平均 {r['avg'] * 100:+.1f}%，"
                        f"中位 {r['med'] * 100:+.0f}%，胜率 {r['win'] * 100:.0f}%。模拟盘开始让它自己下单"
                        f"（照样过全部风控和确认期）。不想要就发 /model shadow")
        if mode == "on":
            r = await self.record(last=30)
            if r["n"] >= 30 and r["avg"] < 0:
                await self.app.db.kv_set("model_mode", "shadow")
                return (f"🤖 模型最近 30 个选择近似平均 {r['avg'] * 100:+.1f}%，转亏了，自动退回影子模式（只记录不下单），"
                        f"等重新训练、成绩回正再上")
        return ""

    async def status_text(self) -> str:
        mode = await self.mode()
        names = {"shadow": "影子模式（只记录不下单）", "on": "模拟盘自己下单", "off": "关闭"}
        lines = [f"<b>🤖 自己的选币模型</b>：{names[mode]}" + ("（实盘不会自动下单）" if not self.cfg.dry_run else "")]
        for k in ("a", "b"):
            lines.append(html.escape(self.model_line(k)))
        for k, name in (("a", "分析模型"), ("b", "早期模型")):
            r = await self.record(since=time.time() - 7 * 86400, k=k)
            if r["n"]:
                lines.append(f"{name}最近 7 天选的币（{r['n']} 个有 4 小时结果）：近似平均 {r['avg'] * 100:+.1f}%，"
                             f"中位 {r['med'] * 100:+.0f}%，胜率 {r['win'] * 100:.0f}%，大涨 {r['big']} 个")
        recent = await self.app.db.fetchall("SELECT ts, symbol, model, score, price, p240 FROM model_picks "
                                            "ORDER BY ts DESC LIMIT 5")
        if recent:
            lines.append("最近选的：" + html.escape("；".join(
                f"{time.strftime('%H:%M', time.localtime(r['ts']))} {r['symbol']}（{'分析' if r['model'] == 'a' else '早期'} "
                f"{r['score']:.2f}" + (f"，4h {f(r['p240']) / f(r['price']):.1f}倍" if f(r["p240"]) > 0 else "") + "）"
                for r in recent)))
        lines.append("考核：影子期选的币有 30 个出结果、平均赚 10% 以上、中位不亏 → 模拟盘自动让它下单；"
                     "之后最近 30 个平均转亏 → 自动退回影子")
        lines.append("命令：/model train 立即训练｜/model shadow / on / off")
        return "\n".join(lines)

    async def review_text(self, start: float) -> str:
        if not self.m:
            return ""
        r = await self.record(since=start - 4 * 3600)
        mode = await self.mode()
        tag = {"shadow": "影子", "on": "已下单", "off": "关闭"}[mode]
        if not r["n"]:
            return f"🤖 自己的选币模型（{tag}）：今天还没有出结果的选择，发 /model 查看"
        return (f"🤖 <b>自己的选币模型</b>（{tag}）：今天 {r['n']} 个选择出了 4 小时结果，近似平均 {r['avg'] * 100:+.1f}%，"
                f"中位 {r['med'] * 100:+.0f}%，胜率 {r['win'] * 100:.0f}%，大涨 {r['big']} 个")

    async def run(self) -> None:
        await asyncio.sleep(120)
        every = float(self.cfg.get("learning.model_train_hours", 24)) * 3600
        while True:
            try:
                await self.fill_picks()
                need = (not any(self.ready(k) for k in ("a", "b")) and time.time() - self.last_train > 6 * 3600) \
                    or time.time() - self.last_train > every
                if need:
                    msgs = await self.train()
                    if any("AUC" in m for m in msgs):
                        await self.app.tg.send("🤖 <b>选币模型重新训练</b>\n" + html.escape("\n".join(msgs)), low=True)
                msg = await self.promote_check()
                if msg:
                    await self.app.tg.send(msg)
            except Exception as e:  # noqa: BLE001
                log.exception("选币模型出错: %s", e)
            await asyncio.sleep(300)
