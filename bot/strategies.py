"""主动建仓策略（不等聪明钱）：

1. 早期建仓：快毕业（bonding curve 快满）、结构干净、持有人在快速增长的币，小仓位先上。
2. 强叙事：市场注意力明显集中的币（KOL 持有、X 粉丝、付费推广、同名币扎堆且它是领头的、成交火热），先建仓再观察。

数据全部来自 GMGN trenches 列表本身（一次请求 80 个币，带持有人、KOL、聪明钱、捆绑/老鼠仓比例、
dev 持仓、社交链接等字段），不额外花接口额度。触发后仍然要过完整分析、风控过滤和 30 秒确认期，
买入后交给动态持仓管理（健康分、涨不上去就卖、止损）。

注意："叙事"在这里是用市场注意力的数据来近似，不是理解新闻内容；GMGN 自己的回测也显示叙事评分
和后续涨跌相关性不强，所以这两类只用一般档的小仓位，并且每天限量。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from typing import TYPE_CHECKING

from .chains import norm_addr
from .utils import f

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.strategy")

EARLY = "早期建仓"
NARRATIVE = "强叙事"

EARLY_DEFAULTS = {
    "enabled": True, "max_age_min": 180, "min_holders": 100, "growth_window_min": 3,
    "min_holder_growth_pct": 15, "min_volume": 15000, "max_rug_ratio": 0.2, "max_top10_rate": 0.3,
    "max_bundler_rate": 0.15, "max_insider_rate": 0.15, "max_creator_rate": 0.05, "max_per_day": 6,
    "persist_sec": 60, "min_token_score": 50,
}
NARRATIVE_DEFAULTS = {
    "enabled": True, "min_score": 60, "max_age_hours": 24, "min_holders": 150, "min_volume": 30000,
    "max_rug_ratio": 0.3, "max_top10_rate": 0.4, "max_bundler_rate": 0.3, "max_insider_rate": 0.2,
    "max_creator_rate": 0.1, "max_per_day": 6,
    "persist_sec": 180, "min_token_score": 55,
}


def _rate(it: dict, *keys: str) -> float:
    """比例字段统一成 0~1（有的接口给百分数）。"""
    for k in keys:
        if it.get(k) not in (None, ""):
            v = f(it.get(k))
            return v / 100 if v > 1 else v
    return 0.0


def volume(it: dict) -> float:
    """成交额。trenches 列表实测只有 24h 成交额（没有 1h），列表里的币基本都是 24 小时内发的，24h ≈ 发币以来累计。"""
    return f(it.get("volume_1h")) or f(it.get("volume_24h"))


TWITTER_EPOCH_MS = 1288834974657


def tweet_info(it: dict, now: float) -> tuple[str, float]:
    """代币绑定的是一条推文（x.com/某人/status/ID）时，返回 (发推人, 推文距今小时数)；绑定的是账号主页返回 ("", -1)。
    推文时间从推文 ID 里算出来（X 的 ID 自带毫秒时间戳）。"""
    m = re.search(r"([A-Za-z0-9_]{1,30})/status(?:es)?/(\d{10,25})", str(it.get("twitter") or ""))
    if not m:
        return "", -1.0
    ts = ((int(m.group(2)) >> 22) + TWITTER_EPOCH_MS) / 1000
    return m.group(1), max(now - ts, 0) / 3600


def structure_problem(it: dict, cfg: dict) -> str:
    """结构是否干净。返回问题描述，干净返回空。"""
    if str(it.get("is_wash_trading", "")).lower() in ("true", "1", "yes"):
        return "刷量"
    checks = [
        (_rate(it, "rug_ratio"), "max_rug_ratio", "跑路风险"),
        (_rate(it, "top_10_holder_rate"), "max_top10_rate", "前10持仓"),
        (_rate(it, "bundler_trader_amount_rate", "bundler_rate"), "max_bundler_rate", "捆绑交易"),
        (max(_rate(it, "rat_trader_amount_rate", "insider_rate"), _rate(it, "suspected_insider_hold_rate")),
         "max_insider_rate", "老鼠仓"),
        (_rate(it, "creator_balance_rate", "dev_team_hold_rate"), "max_creator_rate", "dev 持仓"),
    ]
    for v, key, name in checks:
        if v > float(cfg[key]):
            return f"{name} {v * 100:.1f}%，上限 {float(cfg[key]) * 100:.0f}%"
    return ""


def early_status(it: dict, hist: deque, now: float, cfg: dict) -> tuple[str, str]:
    """快毕业 + 结构干净 + 持有人快速增长。返回 (买入理由, 不达标的原因)，二者只有一个非空。"""
    created = f(it.get("created_timestamp"))
    if created and now - created > float(cfg["max_age_min"]) * 60:
        return "", f"发币超过 {float(cfg['max_age_min']) / 60:g} 小时"
    holders = int(f(it.get("holder_count")))
    if holders < int(cfg["min_holders"]):
        return "", f"持有人 {holders}（要 ≥ {cfg['min_holders']}）"
    if volume(it) < float(cfg["min_volume"]):
        return "", f"成交 ${volume(it):,.0f}（要 ≥ ${float(cfg['min_volume']):,.0f}）"
    prob = structure_problem(it, cfg)
    if prob:
        return "", "结构不过关：" + prob
    win = float(cfg["growth_window_min"]) * 60
    old = [(t, h) for t, h in hist if now - t <= win * 2]
    if not old or now - old[0][0] < win * 0.6 or old[0][1] <= 0:
        return "", "刚进列表，观察持有人增长中"
    t0, h0 = old[0]
    growth = (holders / h0 - 1) * 100
    mins = (now - t0) / 60
    need = float(cfg["min_holder_growth_pct"]) * (mins / float(cfg["growth_window_min"]))
    if growth < max(need, float(cfg["min_holder_growth_pct"]) * 0.6):
        return "", f"持有人增长不够（{mins:.0f} 分钟 +{growth:.0f}%，要 ≥ {cfg['min_holder_growth_pct']}%/{cfg['growth_window_min']} 分钟）"
    extra = []
    if int(f(it.get("smart_degen_count"))):
        extra.append(f"聪明钱 {int(f(it.get('smart_degen_count')))}")
    if int(f(it.get("renowned_count"))):
        extra.append(f"KOL {int(f(it.get('renowned_count')))}")
    return (f"{EARLY}（快毕业，持有人 {mins:.0f} 分钟 {h0}→{holders} +{growth:.0f}%"
            + ("，" + "，".join(extra) if extra else "") + "）"), ""


def early_signal(it: dict, hist: deque, now: float, cfg: dict) -> str:
    return early_status(it, hist, now, cfg)[0]


OBS_COLS = ("ts", "chain", "token", "price", "holders", "volume", "rug", "top10", "bundler", "insider", "creator",
            "wash", "smart", "kol", "created")


def obs_row(chain: str, addr: str, it: dict, now: float) -> tuple:
    """早期观察样本（和 structure_problem / early_status 用同样的字段口径）。"""
    return (now, chain, addr, f(it.get("price")), int(f(it.get("holder_count"))), volume(it),
            _rate(it, "rug_ratio"), _rate(it, "top_10_holder_rate"),
            _rate(it, "bundler_trader_amount_rate", "bundler_rate"),
            max(_rate(it, "rat_trader_amount_rate", "insider_rate"), _rate(it, "suspected_insider_hold_rate")),
            _rate(it, "creator_balance_rate", "dev_team_hold_rate"),
            int(str(it.get("is_wash_trading", "")).lower() in ("true", "1", "yes")),
            int(f(it.get("smart_degen_count"))), int(f(it.get("renowned_count"))), f(it.get("created_timestamp")))


def status_key(why: str) -> str:
    """不达标原因的类别（去掉数字），类别变了才记一条，避免刷屏。"""
    return why.split("（")[0].split("：")[0].split(" ")[0]


def _k(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"


def norm_name(it: dict) -> str:
    s = str(it.get("symbol") or it.get("name") or "").lower()
    return re.sub(r"[^0-9a-z一-鿿]", "", s)


def narrative_score(it: dict, same_name: int, is_leader: bool, now: float | None = None) -> tuple[int, list[str]]:
    """市场注意力评分 0~100。"""
    now = now or time.time()
    s = 0
    notes: list[str] = []

    def add(v: int, why: str) -> None:
        nonlocal s
        s += v
        notes.append(why)

    kol = int(f(it.get("renowned_count")))
    if kol:
        add(35 if kol >= 3 else 25 if kol == 2 else 15, f"KOL {kol} 个")
    sm = int(f(it.get("smart_degen_count")))
    if sm:
        add(20 if sm >= 3 else 10 if sm == 2 else 5, f"聪明钱 {sm} 个")
    fol = int(f(it.get("x_user_follower")))
    author, age_h = tweet_info(it, now)
    if author:
        # 绑定的是别人的一条推文：粉丝数是发推人的。新推文 + 大号 = 正在发酵的热点；旧推文不算
        if age_h <= 12 and fol >= 100_000:
            add(30, f"引用 @{author} {age_h:.0f} 小时前的推文（粉丝 {_k(fol)}）")
        elif age_h <= 12 and fol >= 20_000:
            add(15, f"引用 @{author} 新推文（粉丝 {_k(fol)}）")
    elif it.get("twitter") and fol >= 1000:
        add(15 if fol >= 100_000 else 10 if fol >= 10_000 else 5, f"项目 X 粉丝 {_k(fol)}")
    if it.get("twitter") and it.get("telegram") and it.get("website"):
        add(5, "社交齐全")
    if str(it.get("dexscr_ad", "")).lower() in ("true", "1") or f(it.get("dexscr_boost_fee")) > 0 \
            or str(it.get("dexscr_trending_bar", "")).lower() in ("true", "1"):
        add(10, "项目方在付费推广")
    if same_name >= 3 and is_leader:
        add(10, f"同名币 {same_name} 个、它领跑")
    if volume(it) >= 200_000:
        add(10, f"成交 ${volume(it) / 1000:.0f}K")
    if int(f(it.get("swaps_1h"))) >= 1000 or int(f(it.get("swaps_24h"))) >= 5000:
        add(5, "交易活跃")
    if int(f(it.get("holder_count"))) >= 500:
        add(5, f"持有人 {int(f(it.get('holder_count')))}")
    return min(s, 100), notes


class Strategies:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.hist: dict[tuple[str, str], deque] = {}
        self.fired: dict[str, float] = {}   # 已触发的币（12 小时内不重复）
        self.stats: dict[str, dict] = {}    # 每条链最近一轮扫描的概况（/status 里显示）
        self.pending: dict[str, dict] = {}  # 达标但还在观察是否持续的币
        self.attempts: deque = deque(maxlen=50)   # 最近交给确认期的时间（每小时限量）
        self.last_items: dict[str, dict[str, dict]] = {}   # 最近一轮列表（/whynot 用）
        self.missed: dict[str, float] = {}   # 热度够但结构不过关、已记录过的币
        self.obs_at: dict[tuple[str, str], tuple[float, int]] = {}
        self.obs_seq: dict[tuple[str, str], tuple[dict, dict | None]] = {}   # 每个币第一份 / 上一份快照（早期模型算走势）   # 早期观察样本：上次记录时间、已记几份
        self.notes: dict[str, str] = {}      # 每个币最近一次记录的判断类别（变了才写 strat_log）

    def conf(self, name: str) -> dict:
        base = EARLY_DEFAULTS if name == "early" else NARRATIVE_DEFAULTS
        return {**base, **(self.cfg.get(f"strategies.{name}", {}) or {})}

    async def run(self, chain: str) -> None:
        poll = float(self.cfg.get("strategies.poll_sec", 30))
        while True:
            try:
                await self.poll(chain)
            except Exception as e:  # noqa: BLE001
                log.warning("%s 主动建仓扫描失败: %s", chain, e)
                self.stats.setdefault(chain, {})["error"] = str(e)[:80]
            await asyncio.sleep(poll)

    async def poll(self, chain: str, items: list[dict] | None = None,
                   now: float | None = None) -> list[tuple[str, str, str]]:
        """扫一轮。返回本轮触发的 [(地址, 理由, 档位)]（测试用）。

        论证第一步：达标的币先进"观察名单"，隔几分钟后的扫描里仍然达标、持有人没减少、叙事分没明显下降，
        才交给下一步（完整分析 + 代币评分 + 风控 + 30 秒确认期）。"""
        ec, nc = self.conf("early"), self.conf("narrative")
        if not (ec["enabled"] or nc["enabled"]):
            return []
        if items is None:
            items = await self.app.gmgn.trenches(chain, ["near_completion", "completed"], 80)
        now = now or time.time()
        names = Counter(norm_name(it) for it in items if norm_name(it))
        leader: dict[str, tuple[float, str]] = {}
        for it in items:
            n, v = norm_name(it), volume(it)
            if n and v > leader.get(n, (-1.0, ""))[0]:
                leader[n] = (v, str(it.get("address")))
        fired: list[tuple[str, str, str]] = []
        obs: list[tuple] = []
        st = {"ts": now, "items": len(items), "near": 0, "clean": 0, "best_growth": None, "best_narr": None,
              "error": ""}
        for it in items:
            if not it.get("address"):
                continue
            addr = norm_addr(chain, it["address"])
            key = (chain, addr)
            dq = self.hist.setdefault(key, deque(maxlen=40))
            dq_before = deque(dq)
            dq.append((now, int(f(it.get("holder_count")))))
            if now - self.fired.get(f"{chain}:{addr}", 0) < 12 * 3600:
                continue
            reason, tier, sc = "", "normal", None
            if it.get("_stage") == "即将毕业":
                st["near"] += 1
                if not structure_problem(it, ec):
                    st["clean"] += 1
                    old = [(t, h) for t, h in dq_before if now - t <= float(ec["growth_window_min"]) * 120]
                    if old and old[0][1] > 0 and now - old[0][0] >= 60:
                        g = (int(f(it.get("holder_count"))) / old[0][1] - 1) * 100
                        if st["best_growth"] is None or g > st["best_growth"][0]:
                            st["best_growth"] = (g, str(it.get("symbol") or "?"))
            if it.get("_stage") == "即将毕业" and f(it.get("price")) > 0:
                # 自我学习的样本：每个快毕业的币每 2.5 分钟记一份快照，最多 30 份
                last, n = self.obs_at.get(key, (0.0, 0))
                if now - last >= 150 and n < 30:
                    self.obs_at[key] = (now, n + 1)
                    row = obs_row(chain, addr, it, now)
                    obs.append(row)
                    model = getattr(self.app, "model", None)
                    if model:      # 早期模型打分（影子模式只记录）
                        s = dict(zip(OBS_COLS, row))
                        first, prev = self.obs_seq.get(key, (s, None))
                        self.obs_seq[key] = (first, s)
                        t = asyncio.create_task(model.on_early(s, prev, first))
                        self.app.hub._tasks.add(t)
                        t.add_done_callback(self.app.hub._tasks.discard)
            if ec["enabled"] and it.get("_stage") == "即将毕业":
                reason, why = early_status(it, dq_before, now, ec)
                self.note(chain, addr, it, "早期建仓：" + (why or "达标，进入观察"))
                if reason and (int(f(it.get("smart_degen_count"))) >= 2 or int(f(it.get("renowned_count"))) >= 1):
                    tier = "good"
            if not reason and nc["enabled"]:
                created = f(it.get("created_timestamp"))
                n = norm_name(it)
                ok_size = (not created or now - created <= float(nc["max_age_hours"]) * 3600) \
                    and int(f(it.get("holder_count"))) >= int(nc["min_holders"]) \
                    and volume(it) >= float(nc["min_volume"])
                prob = structure_problem(it, nc) if ok_size else ""
                probe = False
                if prob:
                    # 叙事分很高、结构只是稍微超限（刚发币时前10 / 捆绑天然偏高）：按试探仓的宽松上限再看一次
                    from .trade.trader import probe_cfg
                    pb = probe_cfg(self.cfg)
                    msc0, _ = narrative_score(it, names.get(n, 0), True, now)
                    loose = {**nc, "max_top10_rate": pb["max_top10_pct"] / 100, "max_bundler_rate": pb["max_bundle_pct"] / 100,
                             "max_insider_rate": pb["max_insider_pct"] / 100}
                    if pb["enabled"] and msc0 >= int(pb["narrative_min_score"]) and not structure_problem(it, loose):
                        probe, prob0, prob = True, prob, ""
                if prob and f"{chain}:{addr}" not in self.missed:
                    msc, _ = narrative_score(it, names.get(n, 0), True, now)
                    if msc >= int(nc["min_score"]):
                        # 热度够、结构不过关：记入"被过滤的信号"，1 小时后回看，检验这条结构过滤是否太严
                        self.missed[f"{chain}:{addr}"] = now
                        rep = {"symbol": it.get("symbol", ""), "price": f(it.get("price"))}
                        t = asyncio.create_task(self.app.trader.skip(
                            chain, addr, rep, NARRATIVE, f"论证：叙事分 {msc} 够，但结构不过关（{prob}）"))
                        self.app.hub._tasks.add(t)
                        t.add_done_callback(self.app.hub._tasks.discard)
                if ok_size and not prob:
                    is_leader = bool(n) and leader.get(n, (0, ""))[1] == str(it.get("address"))
                    if names.get(n, 0) >= 3 and not is_leader:
                        continue   # 同名跟风盘，不碰
                    sc, notes = narrative_score(it, names.get(n, 0), is_leader, now)
                    if st["best_narr"] is None or sc > st["best_narr"][0]:
                        st["best_narr"] = (sc, str(it.get("symbol") or "?"))
                    if sc >= int(nc["min_score"]):
                        reason = f"{NARRATIVE}（{sc} 分：{'、'.join(notes[:4])}）"
                        if probe:
                            reason += f"（试探仓：{prob0}，先小仓位建仓再观察）"
            if reason:
                pk = f"{chain}:{addr}"
                holders = int(f(it.get("holder_count")))
                name = "early" if reason.startswith(EARLY) else "narrative"
                persist = float((ec if name == "early" else nc)["persist_sec"])
                pend = self.pending.get(pk)
                if not pend or now - pend["last"] > max(persist, 120) + 120:
                    self.pending[pk] = {"first": now, "last": now, "holders": holders, "score": sc}
                    continue
                pend["last"] = now
                if holders < pend["holders"] or (sc is not None and pend["score"] is not None
                                                 and sc < pend["score"] - 15):
                    self.pending.pop(pk, None)   # 热度在退，不买
                    self.note(chain, addr, it, f"{'早期建仓' if name == 'early' else '强叙事'}：观察期内热度回落，放弃")
                    continue
                if now - pend["first"] < persist:
                    continue
                self.pending.pop(pk, None)
                reason += f"｜论证：持续 {(now - pend['first']) / 60:.0f} 分钟仍达标"
                self.fired[pk] = now
                fired.append((addr, reason, tier))
                t = asyncio.create_task(self.fire(chain, addr, reason, tier))
                self.app.hub._tasks.add(t)
                t.add_done_callback(self.app.hub._tasks.discard)
        if obs and getattr(self.app, "db", None) is not None:
            t = asyncio.create_task(self._save_obs(obs))
            self.app.hub._tasks.add(t)
            t.add_done_callback(self.app.hub._tasks.discard)
        self.stats[chain] = st
        self.last_items[chain] = {norm_addr(chain, it["address"]): it for it in items if it.get("address")}
        cutoff = now - 3600
        for k in [k for k, d in self.hist.items() if d and d[-1][0] < cutoff]:
            self.hist.pop(k, None)
        for k in [k for k, t in self.fired.items() if now - t > 12 * 3600]:
            self.fired.pop(k, None)
        for k in [k for k, v in self.pending.items() if now - v["last"] > 900]:
            self.pending.pop(k, None)
        for k in [k for k, t in self.missed.items() if now - t > 12 * 3600]:
            self.missed.pop(k, None)
        for k in [k for k, v in self.obs_at.items() if now - v[0] > 6 * 3600]:
            self.obs_at.pop(k, None)
            self.obs_seq.pop(k, None)
        return fired

    async def _save_obs(self, rows: list[tuple]) -> None:
        db = self.app.db
        await db.executemany("INSERT INTO early_obs(ts,chain,token,price,holders,volume,rug,top10,bundler,insider,"
                             "creator,wash,smart,kol,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        await db.executemany("INSERT OR IGNORE INTO early_tok(chain,token,first_ts) VALUES(?,?,?)",
                             [(r[1], r[2], r[0]) for r in rows])

    def note(self, chain: str, addr: str, it: dict, msg: str) -> None:
        """记录主动建仓对这个币的判断变化（类别变了才记），/history 里能看到它为什么没早点买。"""
        if getattr(self.app, "db", None) is None:
            return
        k = f"{chain}:{addr}"
        key = status_key(msg.split("：", 1)[1] if "：" in msg else msg) + msg.split("：")[0]
        if self.notes.get(k) == key:
            return
        self.notes[k] = key
        if len(self.notes) > 5000:
            self.notes.clear()
        t = asyncio.create_task(self.app.db.execute(
            "INSERT INTO strat_log(ts,chain,token,msg,price) VALUES(?,?,?,?,?)",
            (time.time(), chain, addr, msg, f(it.get("price")))))
        self.app.hub._tasks.add(t)
        t.add_done_callback(self.app.hub._tasks.discard)

    def explain(self, chain: str, addr: str) -> list[str]:
        """这个币在主动建仓两条策略里的情况（给 /whynot 用）。"""
        it = (self.last_items.get(chain) or {}).get(addr)
        if not it:
            return ["不在 GMGN 当前的「快毕业/已毕业」列表里（列表只有各 60 个最新的币），主动建仓扫不到它"]
        ec, nc = self.conf("early"), self.conf("narrative")
        out = [f"在 GMGN「{it.get('_stage')}」列表里"]
        now = time.time()
        created = f(it.get("created_timestamp"))
        age_h = (now - created) / 3600 if created else 0
        if it.get("_stage") == "即将毕业":
            prob = structure_problem(it, ec)
            out.append(("❌ 早期建仓：结构不过关（" + prob + "）") if prob else "✅ 早期建仓：结构干净（还要看持有人 3 分钟增长 ≥ 15%）")
        prob = structure_problem(it, nc)
        n_names = sum(1 for x in (self.last_items.get(chain) or {}).values() if norm_name(x) == norm_name(it))
        sc, notes = narrative_score(it, n_names, True, now)
        basic = []
        if age_h > float(nc["max_age_hours"]):
            basic.append(f"发币 {age_h:.0f} 小时")
        if int(f(it.get("holder_count"))) < int(nc["min_holders"]):
            basic.append(f"持有人 {int(f(it.get('holder_count')))}")
        if volume(it) < float(nc["min_volume"]):
            basic.append(f"成交 ${volume(it):,.0f}")
        out.append(f"{'✅' if sc >= int(nc['min_score']) else '❌'} 强叙事评分 {sc}（要 ≥ {nc['min_score']}）："
                   + ("、".join(notes) or "没有加分项"))
        if prob:
            out.append(f"❌ 强叙事结构：{prob}（GMGN 数据，上限见 config 的 strategies.narrative）")
        if basic:
            out.append("❌ 强叙事基础条件不够：" + "、".join(basic))
        key = f"{chain}:{addr}"
        if key in self.pending:
            out.append(f"⏳ 正在观察中（已 {(now - self.pending[key]['first']) / 60:.0f} 分钟）")
        if now - self.fired.get(key, 0) < 12 * 3600:
            out.append("☑️ 已经触发过（12 小时内不重复）")
        return out

    def status_text(self, chain: str) -> str:
        st = self.stats.get(chain)
        if not st:
            return "主动建仓：还没扫完第一轮"
        if st.get("error") and not st.get("items"):
            return f"主动建仓：扫描出错 {st['error']}"
        ago = time.time() - st["ts"]
        ec, nc = self.conf("early"), self.conf("narrative")
        g = st.get("best_growth")
        nb = st.get("best_narr")
        parts = [f"主动建仓（{ago:.0f} 秒前扫描 {st['items']} 个币）",
                 f"  快毕业 {st['near']} 个，结构干净 {st['clean']} 个"
                 + (f"，持有人增长最快 {g[1]} +{g[0]:.0f}%（要 ≥{ec['min_holder_growth_pct']}%）" if g else "")]
        if nb:
            parts.append(f"  叙事分最高 {nb[1]} {nb[0]} 分（要 ≥{nc['min_score']}）")
        return "\n".join(parts)

    async def fire(self, chain: str, token: str, reason: str, tier: str) -> None:
        """论证第二步：限量 → 完整分析 → 代币评分达标 → 交给风控和 30 秒确认期。没通过的记入"被过滤的信号"。"""
        name = "early" if reason.startswith(EARLY) else "narrative"
        prefix = EARLY if name == "early" else NARRATIVE
        c = self.conf(name)
        now = time.time()
        per_hour = int(self.cfg.get("strategies.max_per_hour", 2))
        if sum(1 for t in self.attempts if now - t < 3600) >= per_hour:
            log.info("主动建仓这 1 小时已经 %d 笔，跳过 %s", per_hour, token)
            await self.app.db.execute("INSERT INTO strat_log(ts,chain,token,msg,price) VALUES(?,?,?,?,?)",
                                      (now, chain, token, f"{prefix}：达标，但这 1 小时已经买了 {per_hour} 笔，跳过", 0))
            return
        n = await self.app.db.fetchone(
            "SELECT COUNT(*) n FROM positions WHERE reason LIKE ? AND opened_at>=?",
            (prefix + "%", self.app.day_start()))
        if n and n["n"] >= int(c["max_per_day"]):
            log.info("%s 今天已买 %d 笔，达到上限，跳过 %s", prefix, n["n"], token)
            await self.app.db.execute("INSERT INTO strat_log(ts,chain,token,msg,price) VALUES(?,?,?,?,?)",
                                      (now, chain, token, f"{prefix}：达标，但今天已经买满 {n['n']} 笔，跳过", 0))
            return
        rep = await self.app.analyzer.analyze(chain, token, source=prefix)
        if not rep:
            return
        if float(rep.get("score") or 0) < float(c["min_token_score"]):
            await self.app.trader.skip(chain, token, rep, prefix,
                                       f"论证：代币评分 {rep.get('score')} 低于 {c['min_token_score']}")
            return
        reason += f"、代币评分 {rep.get('score')}"
        self.attempts.append(now)
        log.info("触发%s %s：%s", prefix, rep.get("symbol"), reason)
        await self.app.hub.confirm_and_buy(chain, token, rep, reason, wallets=rep.get("smart_buyers") or [],
                                           tier=tier)
