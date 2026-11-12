"""真实成本校准、按战绩分仓、卖出规则自动调整、上实盘标准、备份恢复（不连网）。"""
import asyncio
import gzip
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.db import DB  # noqa: E402
from tests.test_brain import Cfg  # noqa: E402


class PCfg(Cfg):
    def per_chain(self, dotted, chain, default=None):
        v = self.get(dotted, {}) or {}
        return v.get(chain, default) if isinstance(v, dict) else default


def test_costs_calibrate_and_dryrun(tmp_path):
    from bot.trade.costs import calibrate, measured, text
    from bot.trade.executors import DryRunExecutor

    async def go():
        db = DB(tmp_path / "c.db")
        await db.open()
        now = time.time()

        async def kline(chain, token, res, a, b):
            return [{"time": (now - 3600 - 30) * 1000, "open": 1.0, "close": 1.0}]
        app = SimpleNamespace(db=db, gmgn=SimpleNamespace(kline=kline))
        pid = await db.execute("INSERT INTO positions(chain,token,status,dry_run,decimals) VALUES('solana','T','closed',0,6)")
        for i in range(10):
            # 买入：成交价 1.06（报价 1.0，多花 6%）；卖出：报价 2.0，实际拿到 1.9（少拿 5%）
            await db.execute("INSERT INTO trades(ts,chain,token,side,native_amount,token_raw,price_usd,usd_value,dry_run,ok) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)", (now - 3600, "solana", "T", "buy", 1, "1000000", 1.06, 100, 0, 1))
            await db.execute("INSERT INTO trades(ts,chain,token,side,position_id,native_amount,token_raw,price_usd,usd_value,"
                             "dry_run,ok) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (now - 3000, "solana", "T", "sell", pid, 1, "1000000", 2.0, 1.9, 0, 1))
        await db.execute("INSERT INTO trades(ts,chain,token,side,dry_run,ok,usd_value) VALUES(?,?,?,?,?,?,?)",
                         (now, "solana", "T", "sell", 0, 0, 0))
        c = await calibrate(app)
        assert abs(c["all"]["buy"]["med"] - 0.06) < 1e-6 and abs(c["solana"]["sell"]["med"] - 0.05) < 1e-6
        assert c["all"]["sell"]["fail"] > 0 and abs(measured(c, "bsc", "buy") - 0.06) < 1e-6   # bsc 没样本：用全部
        assert "实盘真实成本" in text(c)
        # 模拟盘：成本取 max(公式, 实测)，并扣固定优先费
        cfg = PCfg({"trading": {"sim_fee_pct": 1, "sim_fixed_fee_native": {"solana": 0.01}}})

        async def ov(chain, token):
            return {"liquidity": 1e9}
        app2 = SimpleNamespace(cfg=cfg, live_costs=c, data=SimpleNamespace(overview=ov))
        ex = DryRunExecutor(app2, "solana")
        assert abs(await ex._cost("T", 100, "buy") - 0.06) < 1e-6
        app2.live_costs = None
        assert abs(await ex._cost("T", 100, "buy") - 0.01) < 1e-6
        assert ex._fixed() == 0.01
        await db.close()
    asyncio.run(go())


def test_evidence_sizing_and_exposure(tmp_path):
    from bot.trade import sizing

    async def go():
        db = DB(tmp_path / "s.db")
        await db.open()
        try:
            await body(db)
        finally:
            await db.close()

    async def body(db):
        app = SimpleNamespace(db=db, cfg=Cfg({"trading": {"max_exposure_usd_dry": 1000}}))
        now = time.time()
        for i in range(20):    # 跟单 20 笔每笔 -20%；早期建仓 20 笔每笔 +30%
            await db.execute("INSERT INTO positions(status,dry_run,reason,tier,cost_usd,realized_usd,closed_at) "
                             "VALUES('closed',1,'跟单 | 👍较好','good',100,80,?)", (now,))
            await db.execute("INSERT INTO positions(status,dry_run,reason,tier,cost_usd,realized_usd,closed_at) "
                             "VALUES('closed',1,'早期建仓（快毕业…）','normal',100,130,?)", (now,))
        sizing._cache["t"] = 0
        m, why = await sizing.size_mult(app, "跟单 | 👍较好", "good")
        assert m == 0.25 and "跟单近 7 天 20 笔每笔 -20%" in why
        m2, _ = await sizing.size_mult(app, "早期建仓（快毕业）", "normal")
        assert m2 == 1.5
        assert sizing.mult([0.5] + [-0.01] * 14) == 1.0          # 只靠一笔大赚：不加仓
        # 按链：BSC 20 笔每笔 -30% → BSC 上的单子缩到 ×0.25，Solana 不受影响
        for i in range(20):
            await db.execute("INSERT INTO positions(status,dry_run,chain,reason,tier,cost_usd,realized_usd,closed_at) "
                             "VALUES('closed',1,'bsc','新币高评分','best',100,70,?)", (now,))
        sizing._cache["t"] = 0
        m3, why3 = await sizing.size_mult(app, "新币高评分", "normal", "bsc")
        assert m3 == 0.25 and "BSC近 7 天 20 笔每笔 -30%" in why3
        m4, _ = await sizing.size_mult(app, "强叙事（90 分）", "x", "solana")
        m5, why5 = await sizing.size_mult(app, "强叙事（90 分）", "x", "bsc")
        assert m4 == 1.0 and m5 == 0.25 and "BSC" in why5
        await db.execute("INSERT INTO positions(status,dry_run,cost_usd,amount_raw,remaining_raw) "
                         "VALUES('open',1,900,'100','50')")       # 卖掉一半，剩 $450
        assert (await sizing.exposure_ok(app, 500))[0]
        ok, why = await sizing.exposure_ok(app, 600)
        assert not ok and "超过上限" in why
    asyncio.run(go())


def test_exit_tuning_picks_wider_ladder(tmp_path, monkeypatch):
    from bot import brain as brain_mod
    from bot.brain import Brain
    from bot.trade import replay
    from tests.test_replay import _candles

    exit_cfg = {"stop_loss_pct": -35, "take_profit": [{"gain_pct": 100, "sell_pct": 50}, {"gain_pct": 300, "sell_pct": 50}],
                "trailing_start_pct": 10000, "max_hold_hours": 24, "follow_smart_sell": False,
                "runner": {"enabled": True, "trigger_gain_pct": 100, "floor_gain_pct": 20,
                           "ladder": [{"drop_pct": 25, "sell_pct": 33}, {"drop_pct": 40, "sell_pct": 50},
                                      {"drop_pct": 55, "sell_pct": 100}]}}
    # 每个币：涨到 3 倍 → 回撤 28%（2.15）→ 再涨到 6 倍。回撤档位更宽的不会在 2.15 卖
    path = replay.to_path(_candles([1, 1.5, 2.2, 3.0, 2.15, 2.15, 3.5, 5.0, 6.0, 6.0]))
    trades = [{"p": {"entry_price": 1.0, "tier": "normal"}, "path": path, "ft": None,
               "tier_rules": replay.exit_rules(exit_cfg, "normal"), "fpct": 100, "cost_usd": 100, "symbol": f"S{i}",
               "opened_at": float(i * 3600), "actual": 1.0} for i in range(40)]

    async def fake_collect(app, days=3):
        return [dict(t, path=[(ts + t["opened_at"], px) for ts, px in path], opened_at=t["opened_at"]) for t in trades], 40
    monkeypatch.setattr(replay, "collect", fake_collect)

    async def go():
        db = DB(tmp_path / "e.db")
        await db.open()
        app = SimpleNamespace(db=db, cfg=Cfg({"exit": exit_cfg, "learning": {"auto_tune": "auto"}}))
        msgs = await Brain(app).tune_exits(force=True)
        assert any("强势长拿回撤档位 25/40/55% → 30/45/60%" in m for m in msgs), msgs
        assert app.cfg.get("exit.runner.ladder")[0]["drop_pct"] == 30
        assert "强势长拿回撤档位：25/40/55% → 30/45/60%" in await Brain(app).status_text()
        await db.close()
    asyncio.run(go())
    assert brain_mod.LADDERS[1] == (25, 40, 55)


def test_golive_evaluate(tmp_path):
    from bot import golive

    async def go():
        db = DB(tmp_path / "g.db")
        await db.open()
        app = SimpleNamespace(db=db, cfg=Cfg({}), tz=ZoneInfo("Asia/Phnom_Penh"), live_costs=None)
        now = time.time()
        await db.kv_set("golive_since", now - 8 * 86400)
        await db.kv_set("cost_applied_at", 1)
        for i in range(160):
            win = i % 3 != 0
            await db.execute("INSERT INTO positions(status,dry_run,opened_at,closed_at,cost_usd,realized_usd) "
                             "VALUES('closed',1,?,?,100,?)", (now - 7 * 86400 + i * 3600, now - 7 * 86400 + i * 3600 + 600,
                                                             160 if win else 70))
        r = await golive.evaluate(app)
        assert r["ok"], r["checks"]
        assert "已达到上实盘标准" in await golive.text(app)
        assert "达到上实盘标准" in await golive.daily_line(app)
        assert "全部达标" in await golive.daily_line(app)       # 只庆祝一次
        await db.kv_set("golive_since", now - 2 * 86400)
        r2 = await golive.evaluate(app)
        assert not r2["ok"] and "跑满 7 天" in next(t for ok, t in r2["checks"] if not ok)
        await db.close()
    asyncio.run(go())


def test_backup_slim_and_restore_check(tmp_path):
    from bot.safety import check_backup, make_slim

    async def go():
        db = DB(tmp_path / "m.db")
        await db.open()
        await db.execute("INSERT INTO positions(status,dry_run,opened_at) VALUES('closed',1,?)", (time.time(),))
        await db.execute("INSERT INTO kline_cache(chain,token,t0,data) VALUES('s','t',1,?)", ("x" * 10000,))
        await db.close()
    asyncio.run(go())
    out = tmp_path / "b" / "memebot-x-slim.db.gz"
    out.parent.mkdir()
    size = make_slim(tmp_path / "m.db", out)
    assert size > 0 and out.exists()
    raw = gzip.decompress(out.read_bytes())
    (tmp_path / "r.db").write_bytes(raw)
    con = sqlite3.connect(str(tmp_path / "r.db"))
    assert con.execute("SELECT COUNT(*) FROM kline_cache").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    con.close()
    assert check_backup(out).startswith("✅ 备份可用：持仓记录 1 条")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a db")
    assert check_backup(bad).startswith("❌")


def test_owner_report(tmp_path):
    from bot import owner

    async def go():
        db = DB(tmp_path / "o.db")
        await db.open()
        now = time.time()
        acts = [
            # 机器人下的单（哈希对得上）
            {"event_type": "buy", "tx_hash": "BOT1", "timestamp": now - 7200, "price_usd": 1.0, "cost_usd": 10,
             "token": {"address": "B", "symbol": "BOTC"}},
            # 你手动的：WIN 买 1.0 卖 3.0；LOSE 买 1.0 卖 0.5
            {"event_type": "buy", "tx_hash": "M1", "timestamp": now - 7000, "price_usd": 1.02, "cost_usd": 50,
             "token": {"address": "WIN", "symbol": "WIN"}},
            {"event_type": "sell", "tx_hash": "M2", "timestamp": now - 3000, "price_usd": 2.85, "cost_usd": 140,
             "token": {"address": "WIN", "symbol": "WIN"}},
            {"event_type": "buy", "tx_hash": "M3", "timestamp": now - 6000, "price_usd": 1.0, "cost_usd": 50,
             "token": {"address": "LOSE", "symbol": "LOSE"}},
            {"event_type": "sell", "tx_hash": "M4", "timestamp": now - 5000, "price_usd": 0.5, "cost_usd": 25,
             "token": {"address": "LOSE", "symbol": "LOSE"}},
        ]
        holds = [{"token": {"address": "WIN", "creation_timestamp": now - 8000, "total_supply": 1e9},
                  "history_bought_cost": 50, "history_sold_income": 140, "balance": 0, "usd_value": 0},
                 {"token": {"address": "LOSE", "creation_timestamp": now - 6600, "total_supply": 1e9},
                  "history_bought_cost": 50, "history_sold_income": 25, "balance": 0, "usd_value": 0}]

        async def wallet_activity(chain, addr, n, cursor):
            return {"activities": acts if chain == "solana" else []}

        async def wallet_holdings(chain, addr, n, cursor):
            return {"list": holds if chain == "solana" else []}

        async def kline(chain, token, res, a, b):
            px = {"WIN": 1.0, "LOSE": 1.0}.get(token, 1.0)
            return [{"time": (a + 170) * 1000, "open": px, "close": px if a + 180 < now - 4000 else px * 3}]
        app = SimpleNamespace(db=db, cfg=Cfg({}), live_costs=None,
                              gmgn=SimpleNamespace(wallet_activity=wallet_activity, wallet_holdings=wallet_holdings,
                                                   kline=kline))
        app.cfg.secrets = SimpleNamespace(gmgn_wallet_sol="SoLwallet", gmgn_wallet_evm="0xABC")
        await db.execute("INSERT INTO trades(ts,chain,token,side,tx,dry_run,ok) VALUES(?,?,?,?,?,?,?)",
                         (now - 7200, "solana", "B", "buy", "BOT1", 0, 1))
        await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features) VALUES(?,?,?,?,?,?,?)",
                         (now - 7100, "solana", "WIN", "WIN", "x", 1.0, '{"score": 50, "smart": 0}'))
        try:
            txt = await owner.report(app)
        except Exception:
            await db.close()
            raise
        assert "你在 solana" in txt and "已清仓 2 个币" in txt
        assert "WIN 你赚 +180%（机器人：新币评分不够" in txt
        assert "完全没出现在机器人的数据源里 ×1" in txt
        n = await db.fetchone("SELECT COUNT(*) n FROM manual_costs")
        assert n["n"] == 4                                   # 只记你手动的 4 笔，机器人那笔排除
        buy = await db.fetchone("SELECT cost FROM manual_costs WHERE tx='m1'")
        assert abs(buy["cost"] - 0.02) < 1e-6
        assert app.live_costs["all"]["buy"]["n"] == 2 and app.live_costs["all"]["me"] == 4
        prof = await db.fetchone("SELECT label, source FROM learn_wallets WHERE address='SoLwallet'")
        assert prof["label"] == "我自己" and prof["source"] == "owner"
        await db.close()
    asyncio.run(go())


def test_dex_fallback_when_gmgn_paused():
    from bot.data import DataHub

    calls = []

    async def get(service, url, **kw):
        calls.append(url)
        return [{"baseToken": {"address": "TokA"}, "priceUsd": "0.5", "liquidity": {"usd": 1000}},
                {"baseToken": {"address": "TokA"}, "priceUsd": "0.6", "liquidity": {"usd": 9000}},
                {"baseToken": {"address": "Other"}, "priceUsd": "9", "liquidity": {"usd": 1}}]

    class H:
        _limiters: dict = {}

        def set_rate(self, s, r):
            self._limiters[s] = r
    h = H()
    h.get = get
    gm = SimpleNamespace(paused_until=time.time() + 600)
    app = SimpleNamespace(cfg=Cfg({}), gmgn=gm, http=h)
    d = DataHub(app)

    async def go():
        assert await d.price("solana", "TokA") == 0.6               # 选流动性最大的池子
        assert await d.prices("solana", ["TokA"]) == {"TokA": 0.6}  # 8 秒内走缓存
        assert len(calls) == 1 and "tokens/v1/solana/TokA" in calls[0]
    asyncio.run(go())


def test_outage_does_not_zero_positions(tmp_path):
    from bot.trade.positions import PositionManager

    db_holder: list = []

    async def go():
        db = DB(tmp_path / "z.db")
        db_holder.append(db)
        await db.open()
        sent = []

        async def send(m, low=False):
            sent.append(m)
        app = SimpleNamespace(db=db, cfg=Cfg({"exit": {}}), tg=SimpleNamespace(send=send),
                              gmgn=SimpleNamespace(paused_until=0, consec=0))
        pm = PositionManager.__new__(PositionManager)
        pm.app, pm.cfg, pm._px_ok_at, pm._selling, pm._started = app, app.cfg, {}, set(), time.time() - 3600
        closed = []

        async def close(p, realized, reason):
            closed.append(reason)
        pm._close = close
        old = {"id": 1, "opened_at": time.time() - 5 * 3600, "dry_run": 1, "realized_usd": 0, "symbol": "X"}
        # 刚重启 1 小时、但上次取到价格的时间不在内存里：从启动时间算起，不会立刻归零
        pm._started = time.time() - 600
        await pm._no_price(dict(old), time.time())
        assert not closed
        # 数据源故障：不计时
        pm._started = time.time() - 7200
        await pm._no_price(dict(old), time.time(), outage=True)
        assert not closed and pm._px_ok_at[1] > time.time() - 5
        # 真的 30 分钟以上取不到：按归零平仓
        pm._px_ok_at[1] = time.time() - 2000
        await pm._no_price(dict(old), time.time())
        assert closed
        # 修复 11-08 晚上误平仓的持仓
        await db.execute("INSERT INTO wallets(chain,address,status,follow_losses) VALUES('solana','W','active',3)")
        await db.execute("INSERT INTO positions(id,chain,token,status,dry_run,close_reason,closed_at,source_wallets,"
                         "realized_usd,cost_usd) VALUES(9,'solana','T','closed',1,'取不到价格超过 30 分钟（大概率已归零），"
                         "按归零平仓',1794150540,'[\"W\"]',0,200)")
        assert await pm.repair_outage_zeroes() == 1
        p = await db.fetchone("SELECT status, closed_at FROM positions WHERE id=9")
        assert p["status"] == "open" and p["closed_at"] is None
        assert (await db.fetchone("SELECT follow_losses FROM wallets WHERE address='W'"))["follow_losses"] == 2
        assert await pm.repair_outage_zeroes() == 0          # 只修一次
        assert "恢复了 1 个模拟持仓" in sent[-1]

    async def main():
        try:
            await go()
        finally:
            await db_holder[0].close()
    asyncio.run(main())


def test_smart_buyers_excludes_exited(tmp_path):
    from bot.analysis.analyzer import Analyzer

    async def go():
        db = DB(tmp_path / "sb.db")
        await db.open()
        try:
            now = time.time()
            for w in ("A", "B", "C"):
                await db.execute("INSERT INTO wallets(chain,address,status) VALUES('solana',?,'active')", (w,))
            for w, side, dt in (("A", "buy", 100), ("A", "sell", 70), ("B", "buy", 90), ("C", "buy", 80),
                                ("C", "sell", 60), ("C", "buy", 50)):
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (now - dt, "solana", w, "T", side, f"{w}{dt}"))
            an = Analyzer.__new__(Analyzer)
            an.app = SimpleNamespace(db=db)
            assert sorted(await an.smart_buyers("solana", "T")) == ["B", "C"]     # A 已经卖了，不算
        finally:
            await db.close()
    asyncio.run(go())


def test_fresh_reanalyzes_stale_report():
    from bot.trade.trader import Trader
    calls = []

    async def analyze(chain, token, source="", force=False):
        calls.append(force)
        return {"price": 2.0, "analyzed_at": time.time(), "smart_buyers": []}
    app = SimpleNamespace(cfg=Cfg({"trading": {"max_rep_age_sec": 20}}), analyzer=SimpleNamespace(analyze=analyze))
    tr = Trader.__new__(Trader)
    tr.app, tr.cfg = app, app.cfg

    async def go():
        fresh = {"price": 1.0, "analyzed_at": time.time() - 5, "smart_buyers": ["A"]}
        assert await tr.fresh("solana", "T", fresh) is fresh and not calls        # 5 秒前的：直接用
        old = {"price": 1.0, "analyzed_at": time.time() - 300, "smart_buyers": ["A"], "symbol": "X"}
        new = await tr.fresh("solana", "T", old)
        assert calls == [True] and new["price"] == 2.0 and new["smart_buyers"] == [] and new["symbol"] == "X"
    asyncio.run(go())


def test_bad_actor_memory(tmp_path):
    from bot.trade.positions import PositionManager
    from bot.trade.trader import Trader, skip_cat

    async def go():
        db = DB(tmp_path / "bad.db")
        await db.open()
        try:
            now = time.time()
            app = SimpleNamespace(db=db, cfg=Cfg({"trading": {}}))
            pm = PositionManager.__new__(PositionManager)
            pm.app, pm.cfg = app, app.cfg
            await db.execute("INSERT INTO tokens(chain,address,symbol,creator,first_seen) VALUES('solana','T1','TAOLF','DEV',?)",
                             (now,))
            await db.execute("INSERT INTO wallets(chain,address,status) VALUES('solana','RUGW','active')")
            await db.execute("INSERT INTO bad_actors(chain,kind,key,ts,token,reason) VALUES('solana','wallet_rug',"
                             "'RUGW:T0',?,'T0','x')", (now - 3600,))
            await db.execute("INSERT INTO positions(id,chain,token,symbol,status,cost_usd,realized_usd,source_wallets,"
                             "opened_at) VALUES(1,'solana','T1','TAOLF','open',100,0,'[\"RUGW\"]',?)", (now - 600,))
            p = dict(await db.fetchone("SELECT * FROM positions WHERE id=1"))
            await pm._close(p, 0.05, "止损（-99.9%）")
            w = await db.fetchone("SELECT status FROM wallets WHERE address='RUGW'")
            assert w["status"] == "suspect"                     # 7 天内第 2 次带进亏光的币：标为可疑，不再监听
            tr = Trader.__new__(Trader)
            tr.app, tr.cfg = app, app.cfg
            # 同一个项目方发的新币
            why = await tr.bad_actor("solana", "T2", {"creator": "DEV", "symbol": "OTHER"})
            assert why.startswith("项目方黑名单") and skip_cat(why) == "坏项目方"
            # 换了项目方、同名重发
            why = await tr.bad_actor("solana", "T3", {"creator": "DEV2", "symbol": "$taolf"})
            assert why.startswith("同名币黑名单") and skip_cat(why) == "同名币"
            # 发过一大堆币的项目方
            assert "批量发币" in await tr.bad_actor("solana", "T4", {"creator": "X", "symbol": "NEW", "dev_launches": 40})
            assert await tr.bad_actor("solana", "T5", {"creator": "Y", "symbol": "FINE", "dev_launches": 2}) == ""
            # 数据源故障导致的"取不到价格"平仓不算坏人
            await db.execute("INSERT INTO positions(id,chain,token,symbol,status,cost_usd,realized_usd,source_wallets) "
                             "VALUES(2,'solana','T9','OKCOIN','open',100,0,'[]')")
            await pm._close(dict(await db.fetchone("SELECT * FROM positions WHERE id=2")), 0, "取不到价格超过 30 分钟")
            assert not await db.fetchone("SELECT 1 FROM bad_actors WHERE key='OKCOIN'")
        finally:
            await db.close()
    asyncio.run(go())


def test_wallet_audit_judge(tmp_path):
    from bot.discovery.audit import audit, judge

    def P(tok, pnl=0.5, sym=None, age=5.0, hold=10.0):
        return {"token": tok, "pnl": pnl, "symbol": sym or tok, "entry_age_min": age, "hold_min": hold}
    sells = {t: 1.0 for t in "ABCDEF"}
    # 赚钱的 6 个币里 5 个在它卖出后跌了 90% 以上、而且快进快出：割完即死
    dead = {"A": 0.01, "B": 0.05, "C": 0.02, "D": 0.09, "E": 0.03, "F": 0.8}
    v, notes = judge([P(t) for t in "ABCDEF"], sells, dead, 0)
    assert v == "suspect" and notes[0].startswith("割完即死")
    assert judge([P(t, hold=300) for t in "ABCDEF"], sells, dead, 0)[0] == "ok"      # 拿得久的不算
    assert judge([P(t) for t in "ABCDEF"], sells, {"F": 0.8}, 0)[0] == "ok"           # 取不到价格不算归零
    # 同名多合约：只认让我们亏光过的名字
    v, notes = judge([P("A", sym="TAOLF"), P("B", sym="taolf"), P("C", sym="$TAOLF")], sells, {}, 0, bad_symbols={"TAOLF"})
    assert v == "suspect" and "TAOLF×3" in notes[0]
    assert judge([P("A", sym="PUSSY"), P("B", sym="pussy"), P("C", sym="PUSSY")], sells, {}, 0)[0] == "ok"
    assert judge([P("A", sym="PEPE"), P("B", sym="pepe")], sells, {}, 0)[0] == "ok"
    assert judge([P("A", sym="PEPE"), P("B", sym="pepe")], sells, {}, 0, bad_symbols={"PEPE"})[0] == "suspect"
    # 被标记过（3 次以上）
    assert judge([P("A")], sells, {"A": 1}, 3)[0] == "suspect"
    assert judge([P("A")], sells, {"A": 1}, 2)[0] == "ok"
    # 开盘狙击（没有其他问题）
    v, notes = judge([P(t, age=0.1) for t in "ABCDE"], sells, {t: 1.0 for t in "ABCDE"}, 0)
    assert v == "sniper"
    # 正常
    assert judge([P(t) for t in "ABCDE"], sells, {t: 2.0 for t in "ABCDE"}, 0)[0] == "ok"

    async def go():
        db = DB(tmp_path / "au.db")
        await db.open()
        try:
            await db.execute("INSERT INTO wallets(chain,address,status) VALUES('solana','W','active')")
            now = time.time()
            acts, holds = [], []
            for i, t in enumerate("ABCDEF"):
                acts += [{"event_type": "buy", "timestamp": now - 7200 + i, "price_usd": 0.1, "cost_usd": 10,
                          "token": {"address": t, "symbol": t}},
                         {"event_type": "sell", "timestamp": now - 3600 + i, "price_usd": 1.0, "cost_usd": 100,
                          "token": {"address": t, "symbol": t}}]
                holds.append({"token": {"address": t, "creation_timestamp": now - 8000}, "history_bought_cost": 10,
                              "history_sold_income": 100, "balance": 0, "usd_value": 0})

            async def wallet_activity(chain, addr, n, cursor):
                return {"activities": acts}

            async def wallet_holdings(chain, addr, n, cursor):
                return {"list": holds}

            async def prices(chain, toks):
                return {t: 0.001 for t in toks}             # 全部归零了
            app = SimpleNamespace(db=db, gmgn=SimpleNamespace(wallet_activity=wallet_activity,
                                                              wallet_holdings=wallet_holdings),
                                  data=SimpleNamespace(prices=prices))
            r = await audit(app, "solana", "W")
            assert r["verdict"] == "suspect"
            w = await db.fetchone("SELECT status, audit_verdict FROM wallets WHERE address='W'")
            assert w["status"] == "suspect" and w["audit_verdict"] == "suspect"
            assert (await audit(app, "solana", "W"))["cached"]           # 24 小时内用缓存
            # 重新评估聪明钱时不会把可疑钱包改回 active
            await db.upsert_wallet("solana", "W", status="active", source="x", realized_usd=1, win_rate=1, trades=1, score=1)
            assert (await db.fetchone("SELECT status FROM wallets WHERE address='W'"))["status"] == "suspect"
        finally:
            await db.close()
    asyncio.run(go())


def test_audit_token_roles_and_creator_affinity(tmp_path):
    from bot.discovery.audit import creator_affinity, judge, token_roles
    rep = {"creator": "DEV", "bundle_wallets": [{"wallet": "B1"}], "insider_wallets": [{"wallet": "I1"}],
           "sniper_wallets": [{"wallet": "S1"}]}
    assert token_roles(rep) == {"I1": "老鼠仓", "S1": "开盘狙击", "DEV": "项目方"}
    assert token_roles(rep, include_bundle=True)["B1"] == "捆绑"
    # 好币大多最终也会归零：一半归零不算割完即死，要 70% 以上
    sells = {t: 1.0 for t in "ABCDEF"}
    pos = [{"token": t, "pnl": 0.5, "symbol": t, "entry_age_min": 5} for t in "ABCDEF"]
    pos = [dict(p, hold_min=10) for p in pos]
    assert judge(pos, sells, {"A": 0.01, "B": 0.01, "C": 0.01, "D": 2, "E": 2, "F": 2}, 0)[0] == "ok"
    assert judge(pos, sells, {"A": 0.01, "B": 0.01, "C": 0.01, "D": 0.01, "E": 0.01, "F": 2}, 0)[0] == "suspect"

    async def go():
        db = DB(tmp_path / "ca.db")
        await db.open()
        try:
            for t in ("X1", "X2"):
                await db.execute("INSERT INTO tokens(chain,address,creator) VALUES('solana',?,'RUGDEV')", (t,))
            app = SimpleNamespace(db=db)
            assert await creator_affinity(app, "solana", ["X1", "X2", "Z"]) == ""      # 2 个、项目方没上黑名单
            await db.execute("INSERT INTO bad_actors(chain,kind,key,ts,token,reason) VALUES('solana','creator','RUGDEV',?,"
                             "'X1','x')", (time.time(),))
            assert "跟项目方走得近" in await creator_affinity(app, "solana", ["X1", "X2"])
        finally:
            await db.close()
    asyncio.run(go())


def test_gangs_find_and_hits(tmp_path):
    from bot.discovery.gangs import find_gangs, hits, rebuild
    # A/B/C 一起出现在 T1~T4 的早期买家里；D 只出现 1 次；E/F 一起出现 3 次但只有 2 个人（不成团伙）
    sets = {"T1": ["A", "B", "C", "D"], "T2": ["A", "B", "C", "E", "F"], "T3": ["A", "B", "C", "E", "F"],
            "T4": ["A", "B", "C", "E", "F"], "T5": ["X"]}
    g = find_gangs(sets)
    assert set(g) == {"A", "B", "C", "E", "F"} or set(g) == {"A", "B", "C"}
    assert g["A"][1] >= 3 and g["A"][2] >= 3 and "D" not in g

    async def go():
        db = DB(tmp_path / "g.db")
        await db.open()
        try:
            now = time.time()
            for tok, addrs in sets.items():
                for a in addrs:
                    await db.execute("INSERT INTO address_tags(chain,address,tag,token,detail,created_at) "
                                     "VALUES('solana',?,'bundle',?,'',?)", (a, tok, now))
            app = SimpleNamespace(db=db)
            assert await rebuild(app) >= 1
            rep = {"bundle_wallets": [{"wallet": "A"}, {"wallet": "B"}], "sniper_wallets": [{"wallet": "C"}]}
            n, gid, toks = await hits(app, "solana", rep)
            assert n == 3 and gid > 0 and toks >= 4
            assert (await hits(app, "solana", {"bundle_wallets": [{"wallet": "Z"}]}))[0] == 0
        finally:
            await db.close()
    asyncio.run(go())


def test_skip_shadow_sim(tmp_path, monkeypatch):
    from bot.trade import skipsim
    from tests.test_replay import _candles

    async def fake_candles(app, chain, token, t0):
        pxs = [1, 0.9, 0.7, 0.6, 0.5, 0.5, 0.5, 0.5] if token == "DUMP" else [1, 1.5, 2.2, 3.0, 3.0, 2.0, 1.5, 1.4]
        return [dict(c, time=(t0 + i * 300) * 1000) for i, c in enumerate(_candles(pxs))]
    monkeypatch.setattr(skipsim, "candles", fake_candles)

    async def go():
        db = DB(tmp_path / "s.db")
        await db.open()
        try:
            now = time.time()
            for tok, cat in (("DUMP", "团伙盘"), ("PUMP", "团伙盘"), ("DUMP", "团伙盘")):
                await db.execute("INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price) VALUES(?,?,?,?,?,?,?,?)",
                                 (now - 5 * 3600, "solana", tok, tok, "跟单", cat, "x", 1.0))
            exit_cfg = {"stop_loss_pct": -35, "take_profit": [{"gain_pct": 100, "sell_pct": 50}], "max_hold_hours": 24,
                        "trailing_start_pct": 10000, "follow_smart_sell": False}
            app = SimpleNamespace(db=db, cfg=Cfg({"exit": exit_cfg}))
            assert await skipsim.run_batch(app) == 3
            rows = {r["token"]: r["sim_ret"] for r in await db.fetchall("SELECT token, sim_ret FROM skipped")}
            assert rows["DUMP"] < -0.3 and rows["PUMP"] > 0.3
            s = await skipsim.summary(app, now - 86400)
            assert s["团伙盘"][0] == 3
        finally:
            await db.close()
    asyncio.run(go())


def test_sane_ratio_guards():
    from bot.brain import proxy_return
    from bot.trade.insight import analyze_rows
    from bot.utils import sane_ratio
    assert sane_ratio(3) and sane_ratio(0.01) and not sane_ratio(5e13) and not sane_ratio(1e-9)
    assert proxy_return(1e-15, 1.0, 2.0) is None                    # 记录的价格错了：不拿来学
    assert proxy_return(1.0, 1.2, 2.0) is not None
    rows = [{"price": 1e-15, "p60": 1.0, "features": "{}"}] + [{"price": 1.0, "p60": 1.1, "features": "{}"}] * 3
    assert analyze_rows(rows, "p60")["n"] == 3


def test_dryrun_sell_in_drained_pool(tmp_path, monkeypatch):
    """模拟卖出按池子里真有的钱算：池子被抽干只能卖回一点点；数据一时出错（复查不一致 / 复查拿不到）不会把持仓算成归零。"""
    from bot.trade import executors
    from bot.trade.executors import DryRunExecutor, Fill, pool_impact
    from bot.trade.positions import PositionManager

    monkeypatch.setattr(executors, "RECHECK_SEC", 0)
    assert abs(pool_impact(100, 1_000_000) - 2 * 100 / 1_000_000) < 1e-7      # 小单：和原来的估算一样
    assert pool_impact(100, 2) > 0.98 and pool_impact(100, 200) == 0.5 and pool_impact(100, 0) == 0.0

    async def go():
        db = DB(tmp_path / "d.db")
        await db.open()
        try:
            st = {"liq": 1_000_000.0, "again": 0.0, "checks": 0}

            async def ov(chain, token):
                return {"liquidity": st["liq"]}

            async def price(chain, token):
                return 1.0

            async def check(chain, token):
                st["checks"] += 1
                return st["again"]

            async def native_price(chain):
                return 100.0
            cfg = PCfg({"trading": {"sim_fee_pct": 1, "sim_fixed_fee_native": {"solana": 0}}, "exit": {}})
            app = SimpleNamespace(cfg=cfg, live_costs=None, db=db, native_price=native_price,
                                  data=SimpleNamespace(overview=ov, price=price, liquidity_check=check))
            ex = DryRunExecutor(app, "solana")
            raw = 100 * 10 ** 6                                   # 100 个币，报价 $1 → 值 $100

            f1 = await ex.sell("T", raw, 6, 1000)                 # 池子很深：只扣 1% 手续费
            assert abs(f1.native * 100 - 100 * (1 - 0.01 - 0.0002)) < 0.01 and not f1.note and st["checks"] == 0
            st.update(liq=2.0, again=2.0)                          # 池子被撤空，复查也是空的：几乎卖不回钱
            f2 = await ex.sell("T", raw, 6, 1000)
            assert f2.ok and f2.native * 100 < 1.0 and "池子只剩 $2" in f2.note and st["checks"] == 1
            st.update(liq=2.0, again=500_000.0)                    # 第一次读数是错的，复查说池子还在：正常成交
            f3 = await ex.sell("T", raw, 6, 1000)
            assert f3.native * 100 > 98 and not f3.note
            st.update(liq=2.0, again=0.0)                          # 复查拿不到数：确认不了，最多扣 30%（老办法）
            f4 = await ex.sell("T", raw, 6, 1000)
            assert abs(f4.native * 100 - 69) < 0.01 and not f4.note
            st.update(liq=0.0)                                     # 一开始就取不到流动性：按 5% 估
            f5 = await ex.sell("T", raw, 6, 1000)
            assert abs(f5.native * 100 - 94) < 0.01 and not f5.note
            st.update(liq=200.0, again=200.0)                      # 池子被抽走大半：卖 $100 只能拿回一半
            f6 = await ex.sell("T", raw, 6, 1000)
            assert abs(f6.native * 100 - 49) < 0.01 and "实际只能卖回约 49%" in f6.note
            st.update(liq=0.0, again=0.0)                          # 池子已确认被撤空：不按"确认不了扣 30%"，按剩的钱成交
            executors.POOL_GONE.add(("solana", "T"))
            fg = await ex.sell("T", raw, 6, 1000)
            executors.POOL_GONE.discard(("solana", "T"))
            assert fg.native * 100 < 0.01 and "池子只剩" in fg.note
            st.update(liq=2.0, again=2.0)                          # 买入沿用 30% 封顶
            assert abs(await ex._cost("T", 100, "buy") - 0.31) < 1e-6

            # 持仓管理：这种成交会写进卖出原因
            sent, closed = [], []

            async def trade_sell(p, fill, usd, reason, is_closed, realized):
                sent.append((reason, usd))

            async def sell(token, amt, dec, slip, urgent=False):
                return Fill(True, "DRYRUN", amt, dec, 0.004, quote=1.0, note="池子只剩 $2，这笔按报价值 $100，实际只能卖回约 0%")
            app.trader = SimpleNamespace(exec={"solana": SimpleNamespace(sell=sell)})
            app.tg = SimpleNamespace(trade_sell=trade_sell)
            pm = PositionManager.__new__(PositionManager)
            pm.app, pm.cfg, pm._selling, pm._next_try = app, cfg, set(), {}

            async def close(p, realized, reason):
                closed.append((reason, realized))
            pm._close = close
            pid = await db.execute(
                "INSERT INTO positions(chain,token,symbol,status,dry_run,decimals,amount_raw,remaining_raw,cost_usd,"
                "realized_usd,tp_stage,opened_at) VALUES('solana','T','X','open',1,6,?,?,100,0,0,?)",
                (str(raw), str(raw), time.time() - 600))
            await pm.sell({"id": pid}, 100, "止损 -35%", 1.0)
            assert closed and closed[0][0].startswith("止损 -35%｜池子只剩 $2") and abs(closed[0][1] - 0.4) < 1e-9
            assert sent and "池子只剩" in sent[0][0]
        finally:
            await db.close()
    asyncio.run(go())


def test_liquidity_check_takes_larger_source():
    from bot.data import DataHub

    async def go():
        d = DataHub.__new__(DataHub)
        d.provider, d.app, d._dex_liq = "gmgn", SimpleNamespace(gmgn=object()), {}
        st = {"gm": 5.0, "dex": 900.0}

        async def gm_info(chain, token, max_age=5):
            assert max_age == 0                                   # 复查不走缓存
            return {"liquidity": st["gm"]} if st["gm"] else {}

        async def dex_prices(chain, tokens):
            if st["dex"]:
                d._dex_liq[(chain, tokens[0])] = (time.time(), st["dex"])
            return {}
        d._gm_info, d.dex_prices = gm_info, dex_prices
        assert await d.liquidity_check("solana", "T") == 900.0    # GMGN 说 $5、DexScreener 说 $900：取大的
        d._dex_liq.clear()
        st.update(gm=3.0, dex=0.0)
        assert await d.liquidity_check("solana", "T") == 3.0
        st.update(gm=0.0, dex=0.0)
        assert await d.liquidity_check("solana", "T") == 0.0      # 都取不到 = 不知道
    asyncio.run(go())


def test_funnel_shows_where_volume_goes(tmp_path):
    """漏斗：每天进来多少信号、多少来自后来被淘汰的钱包、被拦在哪、买了多少、赚没赚。"""
    from bot.trade.review import funnel, funnel_text

    async def go():
        db = DB(tmp_path / "f.db")
        await db.open()
        try:
            tz = ZoneInfo("Asia/Phnom_Penh")
            from datetime import datetime
            day0 = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            app = SimpleNamespace(db=db, cfg=PCfg({"trading": {"auto_min_score": 75}}), tz=tz, day_start=lambda: day0)
            app.cfg.dry_run = True
            y = day0 - 86400 + 3600                                     # 昨天
            for addr, st in (("BOT", "suspect"), ("W1", "active"), ("W2", "active")):
                await db.execute("INSERT INTO wallets(chain,address,status) VALUES('solana',?,?)", (addr, st))
            n = 0
            for i in range(40):                                          # 昨天：机器人钱包刷了 40 条，正常钱包 4 条
                n += 1
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (y + i, "solana", "BOT", f"T{i}", "buy", f"tx{n}"))
            for w in ("W1", "W2", "W1", "W2"):
                n += 1
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (y + 100 + n, "solana", w, "TA", "buy", f"tx{n}"))
            await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                             (y + 500, "solana", "W1", "TA", "sell", "txs"))       # 卖出不算
            for i in range(3):                                           # 今天：只有正常钱包的 3 条
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (day0 + 60 + i, "solana", "W1", f"N{i}", "buy", f"txn{i}"))
            for i, (trig, cat) in enumerate((("跟单", "钱包体检不过"), ("跟单", "钱包体检不过"), ("跟单", "快手/战绩差钱包"),
                                             ("强叙事", "强叙事结构不过关"), ("新币高评分（Pump.fun已毕业）", "流动性低"))):
                await db.execute("INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price) "
                                 "VALUES(?,?,?,?,?,?,?,?)", (y + i, "solana", f"S{i}", "S", trig, cat, "x", 1.0))
            for i, sc in enumerate((80, 60, 76, 30)):
                await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features) "
                                 "VALUES(?,?,?,?,?,?,?)", (y + i, "solana", f"K{i}", "K", "x", 1.0, '{"score": %d}' % sc))
            await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features) "
                             "VALUES(?,?,?,?,?,?,?)", (y + 9, "solana", "K1", "K", "x", 1.0, '{"score": 90}'))   # 同一个币再看一次
            for reason, status, cost, real in (("跟单 | 👍较好", "closed", 100, 80), ("早期建仓（快毕业）", "closed", 100, 250),
                                               ("跟单（试探仓：前10持仓 50% 超限）", "open", 25, 0)):
                await db.execute("INSERT INTO positions(chain,token,symbol,status,dry_run,reason,cost_usd,realized_usd,"
                                 "opened_at) VALUES('solana','P','P',?,1,?,?,?,?)", (status, reason, cost, real, y + 50))
            d = await funnel(app, 2)
            yd, td = d[0], d[1]
            assert yd["sig"] == 44 and yd["wallets"] == 3 and yd["gone"] == 40 and yd["gone_w"] == 1 and yd["top10"] == 44
            assert yd["seen"] == 4 and yd["high"] == 3                              # K0 80、K1 90、K2 76
            assert yd["skips"]["跟单"]["钱包体检不过"] == 2 and yd["skips"]["主动建仓"]["强叙事结构不过关"] == 1
            assert yd["skips"]["新币"]["流动性低"] == 1
            assert yd["buys"] == {"跟单": 1, "早期建仓": 1, "试探仓": 1} and abs(yd["pnl"] - 130) < 1e-9 and yd["open"] == 1
            assert td["sig"] == 3 and td["gone"] == 0 and not td["buys"]
            txt = await funnel_text(app, 2)
            assert "其中 40 条来自现在已不再监听的 1 个钱包" in txt and "买入 3 笔" in txt and "买入 0 笔" in txt
            assert yd["toks"] == 41 and yd["multi"] == 1 and "2 个以上聪明钱一起买的 1 个" in txt   # 只有 TA 是 2 个钱包
            # 只看一条链：BSC 的信号 / 买入不混进 Solana
            await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                             (y + 900, "bsc", "B1", "0xb", "buy", "txb"))
            await db.execute("INSERT INTO positions(chain,token,symbol,status,dry_run,reason,cost_usd,realized_usd,"
                             "opened_at) VALUES('bsc','0xb','B','open',1,'跟单',10,0,?)", (y + 60,))
            sol = (await funnel(app, 2, "solana"))[0]
            assert sol["sig"] == 44 and sum(sol["buys"].values()) == 3
            assert (await funnel(app, 2))[0]["sig"] == 45
            assert "只看 Solana" in await funnel_text(app, 2, "solana")
            assert sol["mact"] == 1 and sol["mfate"] == {"没留下拦截记录": 1}      # TA：W1、W2 一起买，没买也没拦截记录
        finally:
            await db.close()
    asyncio.run(go())


def test_solana_monitor_health_and_watchdog(tmp_path):
    """Solana 监听连不上会被看见：/status 有状态、断 10 分钟报警（6 小时一次）、恢复后说一声；某条链信号骤降也报警。"""
    from bot.monitor.solana_wallets import SolanaWalletMonitor, ws_error
    from bot.monitor.watchdog import Watchdog

    class Resp:
        status_code, body = 429, b'{"jsonrpc":"2.0","error":{"code":-32429,"message":"max usage reached"}}'

    class Rejected(Exception):
        response = Resp()

        def __str__(self):
            return "server rejected WebSocket connection: HTTP 429"
    assert ws_error(Rejected()) == ("server rejected WebSocket connection: HTTP 429："
                                    '{"jsonrpc":"2.0","error":{"code":-32429,"message":"max usage reached"}}')
    assert ws_error(ConnectionError("boom")) == "boom" and ws_error(TimeoutError()) == "TimeoutError"

    async def go():
        db = DB(tmp_path / "w.db")
        await db.open()
        try:
            app = SimpleNamespace(db=db, cfg=SimpleNamespace(enabled_chains=["solana", "base"], get=lambda k, d=None: d))
            for c in ("solana", "base"):                           # 每条链都有足够的钱包（"钱包太少"另有测试）
                for i in range(6):
                    await db.execute("INSERT INTO wallets(chain,address,status) VALUES(?,?,'active')", (c, f"{c}{i}"))
            sm = SolanaWalletMonitor(app)
            app.solmon = sm
            sm.wanted = 300
            now = time.time()
            dog = Watchdog(app)
            sm.down_since = now - 120                              # 刚断 2 分钟：不报警
            sm._down(0, ws_error(Rejected()))
            assert await dog.check(now) == [] and "❌ 连不上" in sm.status_text()
            sm.down_since = now - 1500                             # 断了 25 分钟
            msgs = await dog.check(now)
            assert len(msgs) == 1 and "Solana 聪明钱监听断了" in msgs[0] and "max usage reached" in msgs[0]
            assert "额度用完" in msgs[0]
            assert await dog.check(now + 600) == []                # 6 小时内不重复
            assert len(await dog.check(now + 7 * 3600)) == 1       # 还没好：6 小时后再提醒一次
            sm._up(0, 300)                                         # 连上了
            assert "✅ 已订阅 300/300" in sm.status_text()
            assert await dog.check(now + 7 * 3600 + 300) == ["✅ Solana 聪明钱监听恢复了"]
            # 连上了但取不到交易（HTTP 接口被限流）
            sm.notes, sm.failed, sm.last_tx_err = 100, 80, "helius HTTP 429: max usage reached"
            m2 = await dog.check(now + 8 * 3600)
            assert len(m2) == 1 and "取不到交易内容" in m2[0] and "80/100" in m2[0]
            sm.notes, sm.failed = 1000, 3
            # Base：前两天每 3 小时 ~60 条，最近 3 小时 0 条 → 骤降；Solana 一直很少（不到门槛）→ 不报
            n = 0
            for i in range(960):
                n += 1
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (now - 2 * 86400 - 3 * 3600 + i * 180, "base", "W", f"T{i}", "buy", f"b{n}"))
            for i in range(20):
                n += 1
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (now - 86400 + i * 3600, "solana", "W", f"S{i}", "buy", f"s{n}"))
            probs = await dog.problems(now)
            assert list(probs) == ["quiet_base"] and "Base 的聪明钱信号骤降" in probs["quiet_base"]
            for i in range(30):                                    # Base 恢复了
                n += 1
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (now - 600 + i, "base", "W", f"R{i}", "buy", f"r{n}"))
            assert "quiet_base" not in await dog.problems(now)
        finally:
            await db.close()
    asyncio.run(go())


def test_solana_monitor_does_not_reconnect_for_every_wallet_change(monkeypatch):
    """钱包名单没变不重连；变了也要等够间隔才换；300 个钱包只用 1 条连接。"""
    from bot.monitor import solana_wallets as sw

    async def go():
        wallets = [f"W{i}" for i in range(300)]
        ev = asyncio.Event()

        async def active_wallets(chain, n):
            return [{"address": w} for w in wallets]
        async def fetchall(sql, args=()):
            return []
        app = SimpleNamespace(db=SimpleNamespace(active_wallets=active_wallets, fetchall=fetchall), wallets_changed={"solana": ev},
                              cfg=SimpleNamespace(get=lambda k, d=None: 0.2 if k.endswith("resubscribe_min_sec")
                                                  else 400 if k.endswith("max_wallets_per_chain") else d))
        sm = sw.SolanaWalletMonitor(app)
        started = []

        async def conn(cid, ws):
            started.append((cid, len(ws)))
            await asyncio.sleep(3600)
        sm._conn = conn
        task = asyncio.create_task(sm.run())
        await asyncio.sleep(0.05)
        assert started == [(0, 300)] and sm.wanted == 300          # 1 条连接
        ev.set()                                                   # 名单其实没变：不重连
        await asyncio.sleep(0.4)
        assert started == [(0, 300)]
        wallets.append("NEW")
        ev.set()
        await asyncio.sleep(0.05)
        assert started == [(0, 300)]                               # 还没到间隔：先不换
        await asyncio.sleep(3.6)
        assert started[-1] == (0, 301) and len(started) == 2
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())


def test_solana_monitor_real_websocket_roundtrip(monkeypatch):
    """用本地 WebSocket 服务跑一遍真实流程：第一次连接被 429 拒绝（记下节点给的原因）→ 重连成功 → 订阅 → 收到通知。"""
    import json as _json
    from http import HTTPStatus

    import websockets
    from bot.monitor import solana_wallets as sw

    async def go():
        seen = {"conns": 0, "subs": 0}

        def gate(connection, request):
            seen["conns"] += 1
            if seen["conns"] == 1:                                 # 第一次：像 Helius 那样拒绝
                r = connection.respond(HTTPStatus.TOO_MANY_REQUESTS, '{"error":"max usage reached"}')
                return r
            return None

        async def handler(ws):
            async for raw in ws:
                m = _json.loads(raw)
                if m.get("method") == "logsSubscribe":
                    seen["subs"] += 1
                    await ws.send(_json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": 1000 + m["id"]}))
                    if m["id"] == 3:                               # 订阅完了，推一条 W1 的交易通知 + 一条失败交易（应忽略）
                        await ws.send(_json.dumps({"method": "logsNotification", "params": {
                            "subscription": 1002, "result": {"value": {"signature": "SIG1", "err": None}}}}))
                        await ws.send(_json.dumps({"method": "logsNotification", "params": {
                            "subscription": 1003, "result": {"value": {"signature": "SIG2", "err": {"x": 1}}}}}))

        async with websockets.serve(handler, "127.0.0.1", 0, process_request=gate) as server:
            port = server.sockets[0].getsockname()[1]
            app = SimpleNamespace(sol=SimpleNamespace(wss=f"ws://127.0.0.1:{port}"))
            sm = sw.SolanaWalletMonitor(app)
            sm.wanted = 3
            got = []

            async def handle(sig, wallet):
                got.append((sig, wallet))
            sm._handle = handle
            monkeypatch.setattr(sw.random, "random", lambda: 0.0)   # 退避 2 × 0.7 = 1.4 秒
            task = asyncio.create_task(sm._conn(0, ["W0", "W1", "W2"]))
            await asyncio.sleep(0.4)
            assert not sm.live and "HTTP 429" in sm.last_err and "max usage reached" in sm.last_err
            assert "❌ 连不上" in sm.status_text()
            for _ in range(40):
                if got:
                    break
                await asyncio.sleep(0.1)
            assert got == [("SIG1", "W1")] and seen["subs"] == 3 and seen["conns"] == 2
            assert sm.live == {0: 3} and sm.health()["ok"] and sm.last_err == "" and "✅ 已订阅 3/3" in sm.status_text()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not sm.live and sm.down_since > 0
    asyncio.run(go())


def test_helius_quota_out_fails_fast_and_paper_buys_still_work(monkeypatch):
    """Helius 额度用完（429 max usage reached）：不重试、10 分钟内直接报错不敲节点；模拟盘买 Solana 币照常（精度用 GMGN 的）。"""
    from bot.http import Http, QuotaError
    from bot.sources import solana_rpc as sr
    from bot.trade.executors import DryRunExecutor

    calls = {"n": 0}

    class Resp:
        def __init__(self, status, text):
            self.status, self._t, self.headers = status, text, {}

        async def text(self, errors=None):
            return self._t

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    state = {"status": 429, "text": '{"jsonrpc":"2.0","error":{"code":-32429,"message":"max usage reached"}}'}

    class Sess:
        closed = False

        def request(self, method, url, **kw):
            calls["n"] += 1
            return Resp(state["status"], state["text"])

    async def go():
        http = Http()
        http._session = Sess()
        rpc = sr.SolanaRPC(http, "KEY", 100, 100)
        t0 = time.time()
        try:
            await rpc.call("getSlot")
            raise AssertionError("应该报错")
        except sr.QuotaOut as e:
            assert isinstance(e.__cause__, QuotaError)
        assert calls["n"] == 1 and time.time() - t0 < 1 and rpc.out          # 没有重试、没有等
        for _ in range(5):                                                   # 10 分钟内直接报错，不敲节点
            try:
                await rpc.call("getBalance", ["x"])
            except sr.QuotaOut as e:
                assert "分钟后再试" in str(e)
        assert calls["n"] == 1
        rpc.out_until = 0                                                    # 10 分钟到了：放一个请求过去
        state.update(status=200, text='{"jsonrpc":"2.0","result":123}')
        assert await rpc.call("getSlot") == 123 and not rpc.out and rpc.out_since == 0
        state.update(status=429, text='{"error":"Too many requests"}')       # 普通限流：照常重试
        real_sleep = asyncio.sleep
        monkeypatch.setattr("bot.http.asyncio.sleep", lambda s: real_sleep(0))
        n0 = calls["n"]
        try:
            await rpc.call("getSlot", retries=2)
        except Exception as e:  # noqa: BLE001
            assert not isinstance(e, sr.QuotaOut)
        assert calls["n"] - n0 == 3 and not rpc.out

        # 模拟盘：节点查不到精度 → 用 GMGN 的；GMGN 也没有 → Solana 按 6
        rpc.out_until = time.time() + 600
        ov = {"decimals": 9}

        async def overview(chain, token):
            return ov

        async def price(chain, token):
            return 0.001

        async def native_price(chain):
            return 100.0
        app = SimpleNamespace(sol=rpc, cfg=PCfg({"trading": {"sim_fee_pct": 1}}), live_costs=None,
                              native_price=native_price, data=SimpleNamespace(overview=overview, price=price))
        ex = DryRunExecutor(app, "solana")
        f1 = await ex.buy("T", 1.0, 1000)
        assert f1.ok and f1.decimals == 9 and f1.token_raw > 0
        ov.clear()
        assert (await ex.buy("T", 1.0, 1000)).decimals == 6
    asyncio.run(go())


def test_solana_rpc_failover_between_nodes(monkeypatch):
    """多个 Solana 节点：前面的额度用完自动换下一个、10 分钟内不再敲它；临时出错这次换一个试；全部用不了马上报错。"""
    from bot.http import Http
    from bot.sources import solana_rpc as sr

    assert sr.parse_urls("https://solana-mainnet.core.chainstack.com/abc, https://x.io/k|wss://y.io/k") == [
        ("https://solana-mainnet.core.chainstack.com/abc", "wss://solana-mainnet.core.chainstack.com/abc"),
        ("https://x.io/k", "wss://y.io/k")]
    assert sr.parse_urls("") == [] and sr.parse_urls("solana.com") == []
    assert sr.provider_name("https://solana-mainnet.core.chainstack.com/abc") == "Chainstack"
    assert sr.provider_name("https://solana-mainnet.g.alchemy.com/v2/k") == "Alchemy"
    assert sr.provider_name("https://api.mainnet.solana.com") == "Solana 公共节点"

    hits = []
    state = {"helius": (429, '{"error":{"code":-32429,"message":"max usage reached"}}'),
             "chainstack": (200, '{"jsonrpc":"2.0","id":1,"result":7}')}

    class Resp:
        def __init__(self, status, text):
            self.status, self._t, self.headers = status, text, {}

        async def text(self, errors=None):
            return self._t

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        closed = False

        def request(self, method, url, **kw):
            who = "helius" if "helius" in url else "chainstack"
            hits.append(who)
            return Resp(*state[who])

    real_sleep = asyncio.sleep
    monkeypatch.setattr("bot.http.asyncio.sleep", lambda s: real_sleep(0))

    async def go():
        http = Http()
        http._session = Sess()
        rpc = sr.SolanaRPC(http, "HKEY", 100, 100, [("https://solana-mainnet.core.chainstack.com/abc",
                                                       "wss://solana-mainnet.core.chainstack.com/abc")])
        assert [e.name for e in rpc.eps] == ["Helius", "Chainstack"]
        assert await rpc.call("getSlot") == 7 and hits == ["helius", "chainstack"]    # Helius 额度用完 → Chainstack
        assert rpc.eps[0].out and not rpc.out
        hits.clear()
        assert await rpc.call("getSlot") == 7 and hits == ["chainstack"]              # 10 分钟内不再敲 Helius
        assert "额度用完" in await rpc.probe(rpc.eps[0]) and (await rpc.probe(rpc.eps[1])).startswith("✅ 正常，今天已用约")
        # Chainstack 也用完了（JSON 里报错的那种）→ 全部用不了：马上报错，不重试不等待
        state["chainstack"] = (200, '{"jsonrpc":"2.0","id":1,"error":{"code":-32007,"message":"Monthly capacity limit exceeded"}}')
        try:
            await rpc.call("getSlot")
            raise AssertionError("应该报错")
        except sr.QuotaOut as e:
            assert "Helius 额度用完" in str(e) and "Chainstack 额度用完" in str(e)
        hits.clear()
        assert await rpc.get_transaction("SIG") is None and hits == []                # 都用不了：直接返回，不等
        # 10 分钟到了：Helius 恢复
        for ep in rpc.eps:
            ep.out_until = 0
        state["helius"] = (200, '{"jsonrpc":"2.0","id":1,"result":9}')
        assert await rpc.call("getSlot") == 9 and rpc.eps[0].out_since == 0 and rpc.eps[0].out_why == ""
        # Helius 临时出错（502）：这次换 Chainstack 试，不把 Helius 标成用不了
        state["helius"] = (502, "bad gateway")
        state["chainstack"] = (200, '{"jsonrpc":"2.0","id":1,"result":11}')
        rpc.eps[1].out_until = 0
        for _ in range(2):                                                         # 不管轮到谁先
            assert await rpc.call("getSlot", retries=0) == 11 and not rpc.eps[0].out
        # 轮流用：两个节点都正常时，请求平均分到两个节点
        state["helius"] = (200, '{"jsonrpc":"2.0","id":1,"result":9}')
        hits.clear()
        got = [await rpc.call("getSlot") for _ in range(4)]
        assert sorted(got) == [9, 9, 11, 11] and hits.count("helius") == 2
        # 前面的节点被每秒限流（429）：不在原地等着重试，马上换下一个
        state["helius"] = (429, "Too many requests")
        hits.clear()
        assert [await rpc.call("getSlot") for _ in range(2)] == [11, 11] and hits.count("helius") <= 1
        state["helius"] = (502, "bad gateway")
        # 密钥有效、只是套餐不含某个方法（403）：只有这个方法改用别的节点，其他照常用 Helius
        bodies = []
        orig_request = Sess.request

        def picky(self, method, url, **kw):
            who = "helius" if "helius" in url else "chainstack"
            m = (kw.get("json") or {}).get("method")
            bodies.append((who, m))
            if who == "helius" and m == "getTokenAccountsByOwner":
                return Resp(403, "Method not available on your plan")
            if who == "helius":
                return Resp(200, '{"jsonrpc":"2.0","id":1,"result":5}')
            return Resp(200, '{"jsonrpc":"2.0","id":1,"result":11}')
        Sess.request = picky
        rpc.eps[0].key_ok_at, rpc._rr = 0, 0                                                   # 1 分钟内没确认过密钥
        try:
            assert await rpc.call("getTokenAccountsByOwner", ["w"], retries=0) == 11
            assert not rpc.eps[0].out and not rpc.eps[0].can("getTokenAccountsByOwner")
            assert ("helius", "getSlot") in bodies                                   # 先验了一下密钥
            bodies.clear()
            assert await rpc.call("getTokenAccountsByOwner", ["w"], retries=0) == 11
            assert bodies == [("chainstack", "getTokenAccountsByOwner")]              # 1 小时内直接用别的节点
            got = {await rpc.call("getAccountInfo", ["x"], retries=0) for _ in range(2)}
            assert got == {5, 11}                                                     # 别的方法照常轮流用 Helius
            assert "套餐不含：getTokenAccountsByOwner" in await rpc.probe(rpc.eps[0])
            # 验密钥那一下被限速（429 每秒限流）：不能判定密钥无效，只停用这个方法 10 分钟，节点照常用
            def rps(self, method, url, **kw):
                who = "helius" if "helius" in url else "chainstack"
                m = (kw.get("json") or {}).get("method")
                if who == "helius" and m == "getProgramAccounts":
                    return Resp(403, "Method not available on your plan")
                if who == "helius" and m == "getSlot":
                    return Resp(429, "exceeded the RPS limit")
                return Resp(200, '{"jsonrpc":"2.0","id":1,"result":' + ("5" if who == "helius" else "11") + "}")
            Sess.request = rps
            rpc.eps[0].key_ok_at, rpc._rr = 0, 0
            assert await rpc.call("getProgramAccounts", ["p"], retries=0) == 11
            assert not rpc.eps[0].out and not rpc.eps[0].can("getProgramAccounts")
            assert rpc.eps[0].blocked["getProgramAccounts"][0] - time.time() < 700
            assert 5 in {await rpc.call("getAccountInfo", ["x"], retries=0) for _ in range(2)}
            # 同时在路上的请求成功返回：说明节点能用，连停用时间一起清掉（以前留下"❌ ，54 分钟后再试"）
            rpc.eps[0].out_until, rpc.eps[0].out_why, rpc.eps[0].out_since = time.time() + 3000, "x", time.time()
            await rpc._post(rpc.eps[0], {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": []}, 0, False)
            assert not rpc.eps[0].out and rpc.eps[0].out_until == 0
        finally:
            Sess.request = orig_request
        # key 无效：1 小时内不用它
        state["helius"] = (401, "unauthorized")
        rpc._rr, rpc.eps[0].key_ok_at = 0, 0
        assert await rpc.call("getSlot", retries=0) == 11 and rpc.eps[0].out
        assert rpc.eps[0].out_until - time.time() > 3000
        # 普通 JSON-RPC 错误照常抛出，不换节点
        state["chainstack"] = (200, '{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid param"}}')
        try:
            await rpc.call("getAccountInfo", ["x"])
            raise AssertionError("应该报错")
        except sr.RpcError as e:
            assert not isinstance(e, sr.QuotaOut) and not rpc.eps[1].out
    asyncio.run(go())


def test_worth_fetching_and_noisy_wallets(tmp_path):
    """省额度：只转账 / 建账户的通知不去取交易；刷单型钱包不监听（手动加的照样监听）。"""
    from bot.monitor.solana_wallets import SolanaWalletMonitor, worth_fetching

    transfer = ["Program ComputeBudget111111111111111111111111111111 invoke [1]",
                "Program ComputeBudget111111111111111111111111111111 success",
                "Program 11111111111111111111111111111111 invoke [1]", "Program 11111111111111111111111111111111 success",
                "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [1]",
                "Program log: Instruction: Transfer"]
    pump_buy = transfer[:2] + ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
                               "Program log: Instruction: Buy",
                               "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]"]
    assert not worth_fetching(transfer)
    assert worth_fetching(pump_buy)
    assert worth_fetching(["Program log: something"]) and worth_fetching([]) and worth_fetching(None)
    assert worth_fetching(transfer + ["Log truncated"])                 # 日志被截断：看不全，照样取
    bot_route = ["Program BoTxx1111111111111111111111111111111111111 invoke [1]",
                 "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]"]
    assert worth_fetching(bot_route)                                    # 不认识的程序（交易机器人路由）：照样取

    async def go():
        db = DB(tmp_path / "n.db")
        await db.open()
        try:
            for i, (addr, st, sc) in enumerate((("NOISY", "active", 99), ("MAN", "manual", 10), ("A", "active", 80),
                                                 ("B", "active", 70), ("C", "active", 60))):
                await db.execute("INSERT INTO wallets(chain,address,status,score) VALUES('solana',?,?,?)", (addr, st, sc))
            now = time.time()
            n = 0
            for w, per_day in (("NOISY", 400), ("MAN", 300), ("A", 20)):
                for day in range(2):
                    for k in range(per_day):
                        n += 1
                        await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                         (now - day * 86400 - k * 10, "solana", w, f"T{k}", "buy", f"t{n}"))
            app = SimpleNamespace(db=db, cfg=SimpleNamespace(get=lambda k, d=None: d))
            sm = SolanaWalletMonitor(app)
            assert await sm.pick_wallets(3) == ["MAN", "A", "B"] and sm.noisy == 1    # NOISY 每天买 200 多个币：跳过
            # 只是被别人交易提到的钱包：40 条通知只有 2 条是自己发起的 → 停 24 小时
            ev = asyncio.Event()
            app.wallets_changed = {"solana": ev}
            for i in range(40):
                sm._seen_tx("B", i < 2)
                sm._seen_tx("A", i % 2 == 0)                                          # A 一半是自己发起的：正常
            assert set(sm.mention_out) == {"B"} and ev.is_set()
            assert await sm.pick_wallets(3) == ["MAN", "A", "C"]
            sm.wanted = 3
            sm._up(0, 3)
            assert "1 个只是被别人交易提到的钱包不监听" in sm.status_text()
            sm.mention_out["B"] = time.time() - 86401                                # 24 小时到了：重新监听
            assert await sm.pick_wallets(3) == ["MAN", "A", "B"] and "B" not in sm.wstat
            app.cfg = SimpleNamespace(get=lambda k, d=None: 0 if k.endswith("sol_max_daily_tokens") else d)
            assert await sm.pick_wallets(3) == ["MAN", "NOISY", "A"]                 # 设成 0 = 不跳过
        finally:
            await db.close()
    asyncio.run(go())


def test_solana_ws_switches_node_when_rejected(monkeypatch):
    """第一个节点拒绝 WebSocket（429 额度用完）→ 马上换第二个节点连上，不傻等。"""
    import json as _json
    from http import HTTPStatus

    import websockets
    from bot.http import Http
    from bot.monitor import solana_wallets as sw
    from bot.sources.solana_rpc import SolanaRPC

    async def go():
        def reject(connection, request):
            return connection.respond(HTTPStatus.TOO_MANY_REQUESTS, '{"error":"max usage reached"}')

        async def handler(ws):
            async for raw in ws:
                m = _json.loads(raw)
                await ws.send(_json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": 500 + m["id"]}))

        async with websockets.serve(handler, "127.0.0.1", 0, process_request=reject) as bad, \
                websockets.serve(handler, "127.0.0.1", 0) as good:
            pb, pg = bad.sockets[0].getsockname()[1], good.sockets[0].getsockname()[1]
            rpc = SolanaRPC(Http(), "", 5, 1, [(f"http://127.0.0.1:{pb}/k1", f"ws://127.0.0.1:{pb}/k1"),
                                               (f"http://127.0.0.1:{pg}/k2", f"ws://127.0.0.1:{pg}/k2")])
            sm = sw.SolanaWalletMonitor(SimpleNamespace(sol=rpc))
            sm.wanted = 2
            task = asyncio.create_task(sm._conn(0, ["W0", "W1"]))
            for _ in range(50):
                if sm.live.get(0) == 2:
                    break
                await asyncio.sleep(0.1)
            assert sm.live == {0: 2} and sm.node == "127.0.0.1"
            assert rpc.eps[0].ws_out_until > time.time() and "429" in rpc.eps[0].ws_why
            assert rpc.ws_pick() is rpc.eps[1]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())



def test_solana_handle_waits_then_dispatches_separately(monkeypatch):
    """取交易前等一下；分析放到单独的队列里（分析慢不占取交易的名额）；不是钱包自己发起的不算信号。"""
    from bot.monitor import solana_wallets as sw

    monkeypatch.setattr(sw, "FETCH_DELAY", 0.05)

    async def go():
        got, gate = [], asyncio.Event()

        async def on_signal(s):
            await gate.wait()                  # 分析很慢
            got.append(s.tx)

        async def get_transaction(sig):
            return {"sig": sig}
        monkeypatch.setattr(sw, "signer", lambda tx: "W" if tx["sig"] != "OTHER" else "X")
        monkeypatch.setattr(sw, "parse_swaps", lambda tx, w, sig: [SimpleNamespace(tx=sig)])
        app = SimpleNamespace(sol=SimpleNamespace(get_transaction=get_transaction), hub=SimpleNamespace(on_signal=on_signal))
        sm = sw.SolanaWalletMonitor(app)
        t0 = time.time()
        await sm._handle("S1", "W")
        await sm._handle("OTHER", "W")
        assert time.time() - t0 >= 0.1                                        # 每次都先等了一下
        assert len(sm._sig_tasks) == 1 and got == [] and sm.wstat["W"] == [2, 1]   # _handle 已经返回，分析还在排队
        gate.set()
        await asyncio.sleep(0.05)
        assert got == ["S1"] and not sm._sig_tasks
    asyncio.run(go())


def test_diag_splits_by_chain_and_shows_failures(tmp_path):
    """自检：按开仓日期、按链拆盈亏；模拟买入失败的原因；去掉最赚的 3 笔还剩多少；止损后又涨回来的；参数变化。"""
    from bot.trade.diag import diag, diag_text

    async def go():
        db = DB(tmp_path / "g.db")
        await db.open()
        try:
            tz = ZoneInfo("Asia/Phnom_Penh")
            from datetime import datetime
            day0 = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            app = SimpleNamespace(db=db, cfg=PCfg({}), tz=tz, day_start=lambda: day0)
            app.cfg.dry_run = True
            y = day0 - 86400 + 3600
            rows = [("solana", "跟单", 100, 300, "止盈", None), ("solana", "早期建仓", 100, 62, "止损 -35%", 2.0),
                    ("base", "跟单", 50, 31, "止损 -35%", 1.1), ("base", "跟单", 50, 30, "止损 -35%", None),
                    ("bsc", "强叙事", 50, 70, "止盈", None),
                    ("bsc", "跟单", 0, 0, "止损 -35%", 3.0),             # 成本记成 0 的老单子
                    ("bsc", "跟单", 20, 0, "止损（池子被撤空）", 5.0)]     # 归零的单子：收益 -100%，不能除以 0
            for c, r, cost, real, why, pm in rows:
                await db.execute("INSERT INTO positions(chain,symbol,reason,status,dry_run,cost_usd,realized_usd,opened_at,"
                                 "closed_at,close_reason,post_max) VALUES(?,?,?,?,1,?,?,?,?,?,?)",
                                 (c, "S", r, "closed", cost, real, y, y + 600, why, pm))
            await db.execute("INSERT INTO positions(chain,symbol,reason,status,dry_run,cost_usd,realized_usd,opened_at) "
                             "VALUES('base','S','跟单','open',1,50,0,?)", (day0 + 60,))
            for i in range(3):
                await db.execute("INSERT INTO trades(ts,chain,token,side,dry_run,ok,error) VALUES(?,?,?,?,1,0,?)",
                                 (day0 + 100 + i, "solana", "T", "buy", "helius HTTP 429: max usage reached"))
            await db.kv_set("tune_log", [{"ts": y, "key": "strategies.early.min_holders", "old": 80, "new": 50, "why": "x"}])
            d = await diag(app, 2)
            yd, td = d["days"]
            assert yd["opened"] == {"solana": 2, "base": 2, "bsc": 3}
            assert yd["chains"]["base"][0] == 2 and abs(yd["chains"]["base"][1] - (-39)) < 1e-9
            assert td["fails"]["solana"]["helius HTTP 429: max usage reached"] == 3 and td["open_now"] == 1
            assert d["n"] == 7 and abs(d["pnl"] - 123) < 1e-9 and abs(d["top3"] - 220) < 1e-9
            assert d["stops"] == 4 and d["stop_known"] == 3 and d["stop_back"] == 1     # 2.0 倍回到成本以上，1.1 倍、归零的没有
            txt = await diag_text(app, 2)
            assert "Solana 模拟买入失败 3 次" in txt and "去掉它们 -$97" in txt and "min_holders 80→50" in txt
        finally:
            await db.close()
    asyncio.run(go())



def test_watchdog_reports_any_problem(tmp_path, monkeypatch):
    """体检：EVM 监听断了、钱包太少、节点额度用太快、GMGN 限流、长时间没买入、连续亏损、硬盘快满、日志报错——都主动提醒。"""
    import logging as _logging
    from collections import namedtuple
    from bot.monitor import watchdog as wd

    async def go():
        db = DB(tmp_path / "p.db")
        await db.open()
        pl = wd.ProblemLog(["SECRETKEY123"])
        root = _logging.getLogger()
        root.addHandler(pl)
        try:
            now = time.time()
            cfg = SimpleNamespace(enabled_chains=["solana", "base"], get=lambda k, d=None: d, dry_run=True)
            app = SimpleNamespace(db=db, cfg=cfg, started_at=now - 10 * 3600, fmt_time=lambda ts: "20:30")
            for i in range(6):
                await db.execute("INSERT INTO wallets(chain,address,status) VALUES('solana',?,'active')", (f"S{i}",))
            await db.execute("INSERT INTO wallets(chain,address,status) VALUES('base','B0','active')")
            Du = namedtuple("Du", "total used free")
            monkeypatch.setattr(wd.shutil, "disk_usage", lambda p: Du(100 * 1024 ** 3, 99 * 1024 ** 3, 1 * 1024 ** 3))
            # EVM 监听断了 20 分钟
            from bot.monitor.evm_wallets import EvmWalletMonitor
            m = EvmWalletMonitor.__new__(EvmWalletMonitor)
            m.wanted, m.ok, m.down_since, m.last_err = 135, False, now - 1200, "server rejected WebSocket connection: HTTP 429"
            app.evmmons = {"base": m}
            # Solana 节点：Chainstack 照今天的速度会超预算
            from bot.sources.solana_rpc import Endpoint
            ep = Endpoint("Chainstack", "https://x", "wss://x", "solrpc1")
            ep.day, ep.today, ep.since = int(now // 86400), 10 ** 9, now - 7200
            app.sol = SimpleNamespace(eps=[ep], out=False)
            app.gmgn = SimpleNamespace(paused_until=now + 900, bans=3)
            # 10 小时前买过最后一笔；最近 8 笔全亏
            for i in range(8):
                await db.execute("INSERT INTO positions(status,dry_run,cost_usd,realized_usd,opened_at,closed_at) "
                                 "VALUES('closed',1,50,30,?,?)", (now - 10 * 3600, now - 3600 + i))
            await db.execute("INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price) "
                             "VALUES(?,?,?,?,?,?,?,?)", (now - 3600, "base", "T", "T", "跟单", "快手/战绩差钱包", "x", 1.0))
            # 日志：一条报错（带密钥，要打码）、一条反复出现的警告、一条已有专门体检项的警告（不重复）
            lg = _logging.getLogger("memebot.review")
            lg.error("每日复盘失败: key=%s", "SECRETKEY123")
            for _ in range(25):
                _logging.getLogger("memebot.data").warning("取价格失败 %s", "TOKEN")
            for _ in range(30):
                _logging.getLogger("memebot.solmon").warning("Solana WS 断开（%s），%ds 后重连", "x", 2)
            _logging.getLogger("other.lib").error("不是机器人自己的日志")
            if now % 86400 < 3600:                                 # UTC 当天开始不到 1 小时：不估算额度
                ep.day = -1
            dog = wd.Watchdog(app)
            probs = await dog.problems(now)
            for k in ("evm_base", "gmgn", "few_base", "no_buys", "streak", "disk"):
                assert k in probs, (k, list(probs))
            assert "few_solana" not in probs and "Base 聪明钱监听断了" in probs["evm_base"]
            assert "QuickNode" in probs["evm_base"] and "快手/战绩差钱包 1" in probs["no_buys"]
            if ep.day != -1:
                assert "Chainstack 额度用得太快" in probs["budget_Chainstack"]
            msgs = await dog.check(now)
            logmsg = [x for x in msgs if "机器人日志里有问题" in x]
            assert len(logmsg) == 1 and "每日复盘失败" in logmsg[0] and "SECRETKEY123" not in logmsg[0]
            assert "取价格失败 TOKEN" in logmsg[0] and "Solana WS 断开" not in logmsg[0] and "不是机器人" not in logmsg[0]
            assert not [x for x in await dog.check(now + 300) if "机器人日志" in x]     # 6 小时内不重复
            # 恢复：EVM 连上、GMGN 解除
            m.ok, m.down_since = True, 0.0
            app.gmgn.paused_until = 0
            again = await dog.check(now + 600)
            assert "✅ Base 聪明钱监听恢复了" in again and "✅ GMGN 限流结束，恢复正常" in again
            # /problems：现在所有问题，包括已经提醒过的
            rep = await dog.report()
            assert "只监听 1 个聪明钱" in rep and "每日复盘失败" in rep
        finally:
            root.removeHandler(pl)
            await db.close()
    asyncio.run(go())



def test_solana_subscriptions_paced_and_retried(monkeypatch):
    """订阅请求慢慢发；被节点拒绝（超频）的过几秒重发；"已订阅"只算节点真正确认的。"""
    import json as _json

    import websockets
    from bot.monitor import solana_wallets as sw

    monkeypatch.setattr(sw, "SUB_GAP", 0.01)
    monkeypatch.setattr(sw, "SUB_RETRY", 0.2)

    async def go():
        seen = {"n": 0, "w": []}

        async def handler(ws):
            async for raw in ws:
                m = _json.loads(raw)
                w = m["params"][0]["mentions"][0]
                seen["n"] += 1
                seen["w"].append(w)
                if w in ("W1", "W3") and seen["w"].count(w) == 1:     # 第一次订阅 W1、W3 被拒（超频）
                    await ws.send(_json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {
                        "code": -32005, "message": "You've exceeded the RPS limit available on the current plan."}}))
                else:
                    await ws.send(_json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": 900 + m["id"]}))

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            sm = sw.SolanaWalletMonitor(SimpleNamespace(sol=SimpleNamespace(wss=f"ws://127.0.0.1:{port}")))
            sm.wanted = 5
            task = asyncio.create_task(sm._conn(0, [f"W{i}" for i in range(5)]))
            for _ in range(30):
                if sm.live.get(0) == 5:
                    break
                await asyncio.sleep(0.05)
            assert sm.live.get(0) == 5 and seen["n"] == 7 and sm.sub_errors == 2   # 被拒的 2 个重发后订阅上
            assert "已订阅 5/5" in sm.status_text() and sm.fails == 0             # 没有断线重连
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())



def test_busy_wallets_dropped_without_spending_quota():
    """1 小时 60 笔以上成功买卖的钱包（交易机器人）：暂停监听 6 小时，之后的通知直接丢、不花额度取交易。"""
    from bot.monitor import solana_wallets as sw

    ev = asyncio.Event()
    sm = sw.SolanaWalletMonitor(SimpleNamespace(wallets_changed={"solana": ev}))
    t0 = time.time()
    assert not any(sm._busy("CALM", t0 + i * 300) for i in range(12))        # 1 小时 12 条：正常
    hits = [sm._busy("BOT", t0 + i * 10) for i in range(sw.BUSY_PER_H)]
    assert not any(hits[:-1]) and hits[-1] and "BOT" in sm.busy_out and ev.is_set()
    assert sm._busy("BOT", t0 + 9999)
    assert not any(sm._busy("SLOW", t0 + i * 200) for i in range(40))       # 40 条但分散在 2 小时多：不算
    assert sm.busy_per_h == 60 and not any(sm._busy("MID", t0 + i * 60) for i in range(45))   # 1 小时 45 笔：不算


def test_pool_gone_detected_even_without_dynamic_mode():
    """池子被撤空：报价停在高位也要认出来（换数据源确认）；读数一次出错、池子只是变小，不算。"""
    from bot.trade.positions import PositionManager

    async def go():
        st = {"liq": 0.0, "fail": False}

        async def check(chain, token):
            if st["fail"]:
                raise RuntimeError("timeout")
            return st["liq"]
        pm = PositionManager.__new__(PositionManager)
        pm.app = SimpleNamespace(data=SimpleNamespace(liquidity_check=check))
        p = {"chain": "solana", "token": "T"}
        entry = {"liquidity": 36353, "price": 1.0}
        gone = await pm._pool_gone(p, entry, {"liquidity": 0, "price": 5.7})
        assert gone == "池子被撤（跑路）：流动性 $36,353 → $0"
        st["liq"] = 30000.0                                                   # 另一个数据源说池子还在：不算
        assert await pm._pool_gone(p, entry, {"liquidity": 0, "price": 5.7}) == ""
        st["fail"] = True                                                     # 确认不了：先不动
        assert await pm._pool_gone(p, entry, {"liquidity": 0, "price": 5.7}) == ""
        st["fail"] = False
        assert await pm._pool_gone(p, entry, {"liquidity": 12000, "price": 0.5}) == ""   # 只是变小
        assert await pm._pool_gone(p, {"liquidity": 3000}, {"liquidity": 0}) == ""        # 买入时池子就很小：不判断
    asyncio.run(go())


def test_gm_extra_keys_do_not_clobber_report_lists():
    """新记录的 GMGN 字段不能和报告里已有的字段重名（11-12：sniper_wallets 被写成数字，分析全部报错）。"""
    from bot.analysis.analyzer import gm_extra, gsec_extra
    keys = set(gm_extra({"stat": {"fresh_wallet_rate": 0.3}, "tags_stat": {"sniper_wallets": 4}, "link": {}})) | \
        set(gsec_extra({"rug_ratio": 0.1, "sniper_count": 3}))
    reserved = {f"{c}_wallets" for c in ("bundle", "insider", "sniper", "smart")} | {
        "smart_buyers", "risks", "warnings", "reasons", "clusters", "price", "liquidity", "holders", "score"}
    assert not keys & reserved, keys & reserved
    assert gm_extra({"stat": {}, "tags_stat": {"sniper_wallets": 4}, "link": {}})["sniper_tag_n"] == 4


def test_evm_monitor_status_line():
    """/status 里 BSC 链上监听一行：连没连着、最近 1 小时收到多少转账 / 买卖。"""
    from bot.monitor.evm_wallets import EvmWalletMonitor
    m = EvmWalletMonitor.__new__(EvmWalletMonitor)
    m.chain, m.wss, m.rpc = "bsc", "wss://bsc-rpc.publicnode.com", SimpleNamespace(url="")
    m.wanted, m.ok, m.down_since, m.last_err = 27, True, 0.0, ""
    from collections import deque
    now = time.time()
    m.hits, m.sigs = deque([now - 7200, now - 60, now - 30]), deque([now - 30])
    t = m.status_text()
    assert "BSC" in t and "publicnode" in t and "监听 27 个钱包" in t and "转账 2 笔" in t and "买卖 1 笔" in t
    m.ok, m.down_since, m.last_err = False, now - 600, "HTTP 429"
    assert "❌ 断开 10 分钟" in m.status_text()


def test_capped_node_paced_over_the_day():
    """有月额度的节点（Chainstack）今天用得比预算快：排到最后，先用不限次数的节点（Shyft）。"""
    from bot.sources.solana_rpc import Endpoint, SolanaRPC
    rpc = SolanaRPC.__new__(SolanaRPC)
    rpc.budgets = {"Chainstack": 100_000}
    now = 86400 * 20000 + 6 * 3600                       # UTC 早上 6 点
    cs = Endpoint("Chainstack", "https://c", "wss://c", "c")
    cs.day, cs.since = 20000, now - 6 * 3600
    cs.today = 20_000                                    # 6 小时该用 2.5 万（+1 小时余地 = 2.9 万）：没超
    assert not rpc._ahead(cs, now)
    cs.today = 40_000
    assert rpc._ahead(cs, now)
    cs.since, cs.today = now - 600, 5_000                # 刚重启 10 分钟就用了 5 千：超
    assert rpc._ahead(cs, now)
    assert not rpc._ahead(Endpoint("Shyft", "https://s", "wss://s", "s"), now)   # 不限次数的永远不算超
