"""买入时机与过滤：快手钱包、确认期、追高/虚高市值/卖压、连亏熔断、过滤复盘（不连网）。"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.db import DB  # noqa: E402
from bot.signals import Signal, SignalHub  # noqa: E402
from bot.trade.trader import Trader, looks_fake, skip_cat  # noqa: E402

RAW = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
RAW["general"]["dry_run"] = False


class Cfg:
    def __init__(self, raw):
        self.raw = raw

    def get(self, dotted, default=None):
        cur = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def per_chain(self, dotted, chain, default=None):
        v = self.get(dotted, {}) or {}
        return v.get(chain, default) if isinstance(v, dict) else default

    dry_run = False


GOOD = {"symbol": "OK", "price": 1.0, "liquidity": 50_000, "mcap": 500_000, "holders": 800, "volume_1h": 80_000,
        "buys_1h": 300, "sells_1h": 200, "change_1h": 40, "age_hours": 2, "top10_pct": 20, "bundle_held_pct": 5,
        "insider_held_pct": 2, "risks": [], "score": 70, "smart_buyers": []}


def test_looks_fake_and_cats():
    t = RAW["trading"]
    assert looks_fake({"mcap": 143_000_000, "volume_1h": 10_000}, t)          # 截图里的 XTO
    assert not looks_fake({"mcap": 5_000_000, "volume_1h": 200_000}, t)
    assert not looks_fake({"mcap": 800_000, "volume_1h": 100}, t)             # 小市值不判
    assert skip_cat("确认期：带单钱包 30 秒内就卖了") == "带单钱包很快卖出"
    assert skip_cat("确认期：30 秒内已涨 40%，不追") == "确认期价格异动"
    assert skip_cat("捆绑地址持有 30% 超限") == "捆绑多"


def _app(db, msgs):
    async def send(t, *a, **k):
        msgs.append(t)
    state = {"high": 0.0, "ago": 0.0}

    async def recent_high(chain, token, minutes=120):
        return state["high"]

    async def recent_high_and_ago(chain, token, minutes=120, ago_min=5):
        return state["high"], state["ago"]
    return SimpleNamespace(cfg=Cfg(RAW), db=db, tg=SimpleNamespace(send=send), fmt_time=lambda ts: "HH:MM",
                           data=SimpleNamespace(recent_high=recent_high, price=recent_high,
                                                recent_high_and_ago=recent_high_and_ago), _state=state)


def test_entry_filters(tmp_path):
    async def go():
        db = DB(tmp_path / "e.db")
        await db.open()
        app = _app(db, [])
        tr = Trader(app)
        tr.exec = {"solana": object()}
        assert await tr.check("solana", "T", dict(GOOD)) == ""
        assert "不追高" in await tr.check("solana", "T", {**GOOD, "change_1h": 900})
        assert await tr.check("solana", "T", {**GOOD, "change_1h": 900}, ["a", "b", "c"]) == ""   # 3 个聪明钱可以追
        assert "市值虚高" in await tr.check("solana", "T", {**GOOD, "mcap": 50_000_000, "volume_1h": 20_000})
        assert "卖盘" in await tr.check("solana", "T", {**GOOD, "buys_1h": 40, "sells_1h": 100})
        assert "持有人" in await tr.check("solana", "T", {**GOOD, "holders": 30})
        # 不接飞刀：2 小时内高点 10，现价 1（跌了 90%）
        app._state["high"] = 10.0
        assert "不接飞刀" in await tr.check("solana", "T", dict(GOOD))
        assert await tr.check("solana", "T", dict(GOOD), ["a", "b", "c"]) == ""   # 3 个聪明钱一起买除外
        app._state["high"] = 1.5                                                    # 只回撤 33%：正常
        assert await tr.check("solana", "T", dict(GOOD)) == ""
        # 正在砸盘：5 分钟前 1.5，现在 1（跌了 33%），3 个聪明钱也不买
        app._state.update(high=1.5, ago=1.5)
        assert "正在砸盘" in await tr.check("solana", "T", dict(GOOD), ["a", "b", "c"])
        assert skip_cat("最近 5 分钟跌了 33%，正在砸盘") == "正在砸盘"
        app._state["ago"] = 0.0
        app._state["high"] = 0.0
        # 涨幅已兑现：发币价 0.08，现价 1（12.5 倍）
        async def kline(chain, token, res, a, b):
            return [{"time": a * 1000, "open": 0.08}]
        app.gmgn = SimpleNamespace(kline=kline)
        assert "涨幅大部分已经兑现" in await tr.check("solana", "X", dict(GOOD))
        assert await tr.check("solana", "X", dict(GOOD), ["a", "b", "c"]) == ""   # 3 个聪明钱可以追
        assert await tr.check("solana", "Y", {**GOOD, "age_hours": 30}) == ""      # 发币超过 24 小时不看
        assert skip_cat("从发币价已经涨了 12 倍") == "涨幅已兑现"
        rep = dict(GOOD)
        await tr.check("solana", "X", rep)
        assert any(x.startswith("❌ 从发币价涨了 12.5 倍") for x in tr.explain(rep))
        del app.gmgn
        # 团伙盘 / 阴谋地址多
        assert "团伙盘" in await tr.check("solana", "G1", {**GOOD, "gang_n": 3, "gang_tokens": 5})
        assert await tr.check("solana", "G2", {**GOOD, "gang_n": 2}) == ""
        assert "阴谋地址持有 25%" in await tr.check("solana", "G3", {**GOOD, "cabal_pct": 25})
        assert skip_cat("团伙盘：早期买家里有同一个团伙的 3 个成员") == "团伙盘"
        # 被拦下的信号记录下来，同一个币半小时只记一次；持仓满这类和质量无关的不记
        await tr.skip("solana", "T", GOOD, "跟单", "捆绑地址持有 30% 超限")
        await tr.skip("solana", "T", GOOD, "跟单", "捆绑地址持有 31% 超限")
        await tr.skip("solana", "U", GOOD, "跟单", "持仓数量已满")
        rows = await db.fetchall("SELECT * FROM skipped")
        assert len(rows) == 1 and rows[0]["cat"] == "捆绑多"
        # 连亏熔断后不买
        await db.kv_set("streak_pause_until", time.time() + 600)
        assert "连亏" in await tr.check("solana", "T", dict(GOOD))
        await db.close()

    asyncio.run(go())


def test_solo_and_confirm(tmp_path):
    async def go():
        db = DB(tmp_path / "c.db")
        await db.open()
        msgs = []
        app = _app(db, msgs)
        state = {"px": 1.0}
        calls = {"skip": [], "buy": []}

        async def price(chain, tok):
            return state["px"]

        async def skip(chain, token, rep, trig, why):
            calls["skip"].append(why)

        async def consider(chain, token, rep, reason, wallets):
            calls["buy"].append((token, reason, wallets))

        app.data = SimpleNamespace(price=price)
        app.trader = SimpleNamespace(skip=skip, consider=consider)
        RAW["trading"]["confirm_sec"] = 0.05
        hub = SignalHub(app)
        now = time.time()
        await db.execute("INSERT INTO wallets(chain,address,status,source,score,created_at,avg_hold_sec,"
                         "follow_wins,follow_losses) VALUES('solana','FAST','active','x',70,0,0,0,0)")
        # FAST 最近 5 次都是 2 分钟就卖 → 快手
        for i in range(5):
            await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                             (now - 3600 * (i + 1), "solana", "FAST", f"T{i}", "buy", f"b{i}"))
            await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                             (now - 3600 * (i + 1) + 120, "solana", "FAST", f"T{i}", "sell", f"s{i}"))
        med, n = await hub.wallet_speed("solana", "FAST")
        assert n == 5 and med == 120
        w = await db.wallet("solana", "FAST")
        ok, why = await hub.solo_ok("solana", w)
        assert not ok and "跟不上" in why
        # GMGN 平均持仓 2 小时、没有观测样本、跟单战绩一般 → 稳
        ok, _ = await hub.solo_ok("solana", {**w, "address": "NEW", "avg_hold_sec": 7200})
        assert ok
        ok, why = await hub.solo_ok("solana", {**w, "address": "NEW", "avg_hold_sec": 7200,
                                               "follow_wins": 2, "follow_losses": 8})
        assert not ok and "战绩" in why

        sig = Signal("solana", "W", "TOK", "buy", 1.0, 1.0, "tx", now)
        rep = dict(GOOD)
        # 确认期正常 → 买
        await hub._confirm(sig, rep, "跟单")
        assert calls["buy"] and calls["buy"][-1][1] == "跟单"
        # 确认期内拉了 40% → 不追
        async def bump():
            await asyncio.sleep(0.01)
            state["px"] = 1.4
        state["px"] = 1.0
        await asyncio.gather(hub._confirm(sig, rep, "跟单"), bump())
        assert "不追" in calls["skip"][-1]
        # 带单钱包确认期内卖了 → 不买
        state["px"] = 1.0
        await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                         (now + 1, "solana", "W", "TOK", "sell", "sx"))
        await hub._confirm(sig, rep, "跟单")
        assert "卖了" in calls["skip"][-1]
        RAW["trading"]["confirm_sec"] = 30
        await db.close()

    asyncio.run(go())


def test_loss_streak_and_skipped_review(tmp_path):
    from bot.trade.positions import PositionManager
    from bot.trade.review import skipped_review

    async def go():
        db = DB(tmp_path / "l.db")
        await db.open()
        msgs = []
        app = _app(db, msgs)
        pm = PositionManager(app)
        now = time.time()
        for i, got in enumerate((4.0, 4.1, 3.0, 2.0)):
            await db.execute("INSERT INTO positions(status,dry_run,cost_usd,realized_usd,closed_at) "
                             "VALUES('closed',0,4.5,?,?)", (got, now - 100 + i))
        await pm.check_loss_streak()
        assert float(await db.kv_get("streak_pause_until", 0)) > now and any("连续 4 笔亏损" in m for m in msgs)
        # 熔断后计数清零：再亏 1 笔不会马上再熔断
        await db.kv_set("streak_pause_until", 0)
        await db.execute("INSERT INTO positions(status,dry_run,cost_usd,realized_usd,closed_at) "
                         "VALUES('closed',0,4.5,1.0,?)", (time.time() + 1,))
        await pm.check_loss_streak()
        assert float(await db.kv_get("streak_pause_until", 0)) == 0
        # 过滤复盘
        for cat, p0, p1 in (("捆绑多", 1.0, 0.5), ("捆绑多", 1.0, 0.6), ("已经涨太多", 1.0, 1.5)):
            await db.execute("INSERT INTO skipped(ts,chain,token,cat,reason,price,px_1h) VALUES(?,?,?,?,?,?,?)",
                             (now - 7200, "solana", "X", cat, "", p0, p1))
        txt = await skipped_review(app, now - 3 * 3600)
        assert "捆绑多：2 个，中位 -40%，平均 -45%" in txt and "已经涨太多：1 个，中位 +50%，平均 +50%" in txt
        assert "假如都买了约 -39%/笔" in txt
        await db.close()

    asyncio.run(go())


def test_follow_sell_needs_majority(tmp_path):
    async def go():
        db = DB(tmp_path / "m.db")
        await db.open()
        RAW["exit"]["follow_smart_sell"] = True     # 配置里默认已关闭跟卖，这里测试跟卖本身的逻辑
        app = _app(db, [])
        sells = []

        async def sell(p, pct, why, *a, **k):
            sells.append((p["id"], pct, why))
        app.positions = SimpleNamespace(sell=sell)
        tr = Trader(app)
        now = time.time()
        pid = await db.execute(
            "INSERT INTO positions(chain,token,symbol,status,source_wallets,opened_at,tier) "
            "VALUES('solana','TOK','X','open',?,?,'good')", ('["A","B","C"]', now - 60))
        for w in ("A", "B", "C"):
            await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                             (now - 120, "solana", w, "TOK", "buy", "b" + w))
        await tr.on_smart_sell(Signal("solana", "A", "TOK", "sell", 1, 1, "sA", now))
        assert not sells                                   # 1/3 卖出：继续拿
        await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                         (now, "solana", "A", "TOK", "sell", "sA"))
        await tr.on_smart_sell(Signal("solana", "B", "TOK", "sell", 1, 1, "sB", now + 1))
        assert sells and sells[0][0] == pid and "2/3" in sells[0][2]   # 过半：跟卖
        # 单个钱包带单的，它卖就跟
        sells.clear()
        await db.execute("INSERT INTO positions(chain,token,symbol,status,source_wallets,opened_at,tier) "
                         "VALUES('solana','T2','Y','open',?,?,'normal')", ('["D"]', now - 60))
        await tr.on_smart_sell(Signal("solana", "D", "T2", "sell", 1, 1, "sD", now))
        assert sells and sells[0][1] == 100
        await db.close()

    asyncio.run(go())


def test_probe_position(tmp_path):
    """信号很强、只是筹码结构稍微超限：小仓位试探建仓；结构太差或信号一般还是不买。"""
    async def go():
        db = DB(tmp_path / "p.db")
        await db.open()
        try:
            app = _app(db, [])
            tr = Trader(app)
            tr.exec = {"solana": object()}
            assert not tr.probe_ok({}, ["a", "b", "c"], "早期建仓")             # 11-11 起默认关闭
            app.cfg.raw = {**app.cfg.raw, "trading": {**app.cfg.raw["trading"],
                                                      "probe": {**app.cfg.raw["trading"]["probe"], "enabled": True},
                                                      "max_bundle_pct": 25}}
            hot = {**GOOD, "top10_pct": 52, "bundle_held_pct": 28}
            assert "捆绑地址持有" in await tr.check("solana", "T", dict(hot))
            assert await tr.check("solana", "T", dict(hot), relax=True) == ""
            assert "前10持仓" in await tr.check("solana", "T", {**hot, "top10_pct": 80}, relax=True)
            assert "埋伏地址持有" in await tr.check("solana", "T", {**hot, "insider_held_pct": 30}, relax=True)   # 埋伏不放宽
            assert skip_cat("论证：叙事分 95 够，但结构不过关（捆绑交易 75.0%，上限 60%）") == "强叙事结构不过关"
            assert skip_cat("论证：代币评分 40 低于 55") == "主动建仓论证未通过"
            assert tr.probe_ok(hot, [], "早期建仓（持有人 3 分钟 +30%）")
            assert tr.probe_ok(hot, ["a", "b"], "跟单（2 个聪明钱）")
            assert tr.probe_ok(hot, [], "强叙事（95 分：KOL 3 个）") and not tr.probe_ok(hot, [], "强叙事（65 分：KOL 1 个）")
            assert not tr.probe_ok(hot, ["a"], "跟单（1 个聪明钱）")
            assert not tr.probe_ok({**hot, "insider_wallets": ["b"]}, ["a", "b"], "跟单")   # 老鼠仓不算聪明钱
            bought, skipped = [], []

            async def fresh(chain, token, rep):
                return rep

            async def buy(chain, token, rep, reason, wallets, tier=None, probe=False):
                bought.append((token, reason, probe))

            async def skip(chain, token, rep, trigger, why):
                skipped.append((token, why))
            tr.fresh, tr._buy, tr.skip = fresh, buy, skip
            await tr.consider("solana", "A", dict(hot), "跟单（2 个聪明钱）", ["a", "b"])
            assert bought and bought[0][2] is True and "试探仓" in bought[0][1] and "捆绑地址持有" in bought[0][1]
            await tr.consider("solana", "B", dict(hot), "跟单（1 个聪明钱）", ["a"])
            assert skipped and skipped[0][0] == "B" and "捆绑地址持有" in skipped[0][1]
            await tr.consider("solana", "C", {**hot, "top10_pct": 80}, "早期建仓", [])
            assert skipped[-1][0] == "C"                                 # 宽松上限也超了：不买
            await tr.consider("solana", "D", dict(hot), "强叙事（95 分）（试探仓：前10持仓 45%）", [])
            assert bought[-1][0] == "D" and bought[-1][2] is True        # 主动建仓那边已经判定为试探仓
            await tr.consider("solana", "E", dict(GOOD), "跟单（1 个聪明钱）", ["a"])
            assert bought[-1] == ("E", "跟单（1 个聪明钱）", False)       # 结构正常：正常仓位
            app.cfg.raw["trading"]["probe"]["enabled"] = False
            assert not tr.probe_ok(hot, ["a", "b"], "早期建仓")
            app.cfg.raw["trading"]["probe"]["enabled"] = True
            from bot.trade.sizing import source_of
            assert source_of(bought[0][1]) == "试探仓" and source_of("跟单（2 个）") == "跟单"
        finally:
            await db.close()
    asyncio.run(go())


def test_pullable_pool_blocked(tmp_path):
    """Solana：主池子是普通交易池、GMGN 说池子没烧 → 项目方随时能撤池子，不买；已烧 / 还在曲线上 / 不知道 → 不拦。"""
    from bot.trade.trader import pullable_pool, skip_cat

    base = {"chain": "solana"}
    assert "项目方随时能把池子撤走" in pullable_pool({**base, "lp_burn": "", "exchange": "meteora_dlmm"})
    assert pullable_pool({**base, "lp_burn": "", "exchange": "pump_amm"})          # 自己在 PumpSwap 建的池子、没烧
    assert pullable_pool({**base, "lp_burn": "burn", "exchange": "raydium"}) == ""
    assert pullable_pool({**base, "lp_burn": None, "exchange": "raydium"}) == ""    # GMGN 没返回：不知道，不拦
    assert pullable_pool({**base, "lp_burn": "", "exchange": "pump"}) == ""          # 还在 pump.fun 曲线上
    assert pullable_pool({**base, "lp_burn": "", "exchange": "meteora_dbc"}) == ""
    assert pullable_pool({**base, "lp_burn": "", "exchange": ""}) == ""
    assert pullable_pool({"chain": "bsc", "lp_burn": "", "exchange": "pancake"}) == ""
    assert skip_cat(pullable_pool({**base, "lp_burn": "", "exchange": "raydium"})) == "池子能被撤"

    async def go():
        db = DB(tmp_path / "lp.db")
        await db.open()
        try:
            tr = Trader(_app(db, []))
            tr.exec = {"solana": object()}
            rep = {**GOOD, "chain": "solana", "lp_burn": "", "exchange": "raydium_cpmm"}
            assert "池子没烧" in await tr.check("solana", "T", rep)
            assert "池子没烧" not in await tr.check("solana", "T", {**rep, "lp_burn": "burn"})
        finally:
            await db.close()
    asyncio.run(go())
