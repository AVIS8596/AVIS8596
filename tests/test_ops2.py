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
        assert "WIN 你赚 +180%（机器人新币评分不够" in txt
        assert "机器人没看到 ×1" in txt
        n = await db.fetchone("SELECT COUNT(*) n FROM manual_costs")
        assert n["n"] == 4                                   # 只记你手动的 4 笔，机器人那笔排除
        buy = await db.fetchone("SELECT cost FROM manual_costs WHERE tx='m1'")
        assert abs(buy["cost"] - 0.02) < 1e-6
        assert app.live_costs["all"]["buy"]["n"] == 2 and app.live_costs["all"]["me"] == 4
        prof = await db.fetchone("SELECT label, source FROM learn_wallets WHERE address='SoLwallet'")
        assert prof["label"] == "我自己" and prof["source"] == "owner"
        await db.close()
    asyncio.run(go())
