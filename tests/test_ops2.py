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
        await db.execute("INSERT INTO positions(status,dry_run,cost_usd,amount_raw,remaining_raw) "
                         "VALUES('open',1,900,'100','50')")       # 卖掉一半，剩 $450
        assert (await sizing.exposure_ok(app, 500))[0]
        ok, why = await sizing.exposure_ok(app, 600)
        assert not ok and "超过上限" in why
        await db.close()
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
