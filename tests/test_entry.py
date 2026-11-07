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
    return SimpleNamespace(cfg=Cfg(RAW), db=db, tg=SimpleNamespace(send=send), fmt_time=lambda ts: "HH:MM")


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
        assert "捆绑多：2 个，平均 -45%" in txt and "已经涨太多：1 个，平均 +50%" in txt
        await db.close()

    asyncio.run(go())
