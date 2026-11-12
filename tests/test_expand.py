"""扩充钱包池：信号质量、大涨币挖矿、KOL（不连网）。"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.db import DB  # noqa: E402
from bot.discovery.expand import WalletExpander, classify_hits, quality_from  # noqa: E402


def test_quality_and_classify():
    rows = [{"wallet": "A", "token": f"T{i}", "price": 1, "p60": 0.5} for i in range(10)]
    rows += [{"wallet": "A", "token": "T0", "price": 1, "p60": 9}]          # 同一个币只算第一次
    rows += [{"wallet": "B", "token": f"T{i}", "price": 1, "p60": 1.4} for i in range(10)]
    q = quality_from(rows)
    assert q["A"]["n"] == 10 and q["A"]["ret"] == -0.5 and q["A"]["dump"] == 1
    assert q["B"]["ret"] > 0.39 and q["B"]["hit"] == 1
    assert classify_hits([{"insider": 1, "kol": 0}, {"insider": 1, "kol": 0}, {"insider": 0, "kol": 0}]) == "insider"
    assert classify_hits([{"insider": 0, "kol": 1}, {"insider": 0, "kol": 0}]) == "kol"
    assert classify_hits([{"insider": 0, "kol": 0}] * 3) == "winner"
    assert classify_hits([{"insider": 0, "kol": 0, "early": 1}] * 2) == "early"
    from bot.discovery.expand import is_early_finder
    t = {"start_holding_at": 1000 + 20 * 60, "avg_cost": 0.00005}
    assert is_early_finder(t, 1000, 1e9, 500_000)                     # 发币 20 分钟、市值 $5 万买，现在 $50 万
    assert not is_early_finder({**t, "start_holding_at": 1060}, 1000, 1e9, 500_000)   # 开盘 1 分钟：狙击
    assert not is_early_finder({**t, "avg_cost": 0.0005}, 1000, 1e9, 5_000_000)       # 入场市值 $50 万，太晚


def test_solo_uses_signal_quality(tmp_path):
    from bot.signals import SignalHub

    async def go():
        db = DB(tmp_path / "q.db")
        await db.open()
        app = SimpleNamespace(cfg=SimpleNamespace(get=lambda k, d=None: d), db=db)
        hub = SignalHub(app)
        base = {"address": "W", "avg_hold_sec": 30, "follow_wins": 0, "follow_losses": 0, "kind": "smart"}
        ok, why = await hub.solo_ok("solana", {**base, "sig_n": 10, "sig_ret": -0.25, "sig_hit": 0.1, "sig_dump": 0.6})
        assert not ok and "跟不赚" in why
        ok, _ = await hub.solo_ok("solana", {**base, "sig_n": 10, "sig_ret": 0.12, "sig_hit": 0.4, "sig_dump": 0.1})
        assert ok                                            # 快手但跟它买确实赚 → 可以单独跟
        ok, why = await hub.solo_ok("solana", {**base, "avg_hold_sec": 7200, "kind": "kol"})
        assert not ok and "KOL" in why
        await db.close()

    asyncio.run(go())


def test_mining_and_quality_flow(tmp_path):
    async def go():
        db = DB(tmp_path / "m.db")
        await db.open()
        now = time.time()
        changed = SimpleNamespace(set=lambda: None)

        class GM:
            async def top_traders(self, chain, tok, limit, order_by="profit"):
                return [{"address": "PRO", "profit": "5000", "start_holding_at": now - 3000, "tags": []},
                        {"address": "RAT", "profit": "9000", "maker_token_tags": ["rat_trader"]},
                        {"address": "LP", "addr_type": 2, "profit": "1"}]

            async def kol(self, chain, n):
                return [{"maker": "K1"}, {"maker": "K2"}]

            async def wallet_profits(self, chain, ws, period):
                return [{"wallet_address": "K1", "realized_profit": "30000"}, {"wallet_address": "K2", "realized_profit": "-5"}]

        async def overview(chain, tok):
            return {"created_ts": now - 3600}

        app = SimpleNamespace(db=db, gmgn=GM(), cfg=SimpleNamespace(get=lambda k, d=None: d),
                              data=SimpleNamespace(overview=overview), wallets_changed={"solana": changed})
        try:
            for i in range(3):   # 3 个涨了 4 倍的币
                await db.execute("INSERT INTO token_outcomes(ts,chain,token,price,p60,p240) VALUES(?,?,?,?,?,?)",
                                 (now - 3600, "solana", f"WIN{i}", 1.0, 2.0, 4.0))
            ex = WalletExpander(app)
            assert await ex.mine_winners("solana") == 2
            pro, rat = await db.wallet("solana", "PRO"), await db.wallet("solana", "RAT")
            assert pro["kind"] == "winner" and rat["kind"] == "insider" and pro["status"] == "active"
            assert await db.wallet("solana", "LP") is None
            assert await ex.refresh_kols("solana") == 1
            assert (await db.wallet("solana", "K1"))["kind"] == "kol"
            # 信号质量：PRO 买的 15 个币 1 小时后都跌一半 → 移出监听
            for i in range(15):
                await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                                 (now - 7200, "solana", "PRO", f"X{i}", "buy", f"t{i}"))
                await db.execute("INSERT INTO token_outcomes(ts,chain,token,price,p60) VALUES(?,?,?,?,?)",
                                 (now - 7190, "solana", f"X{i}", 1.0, 0.5))
            assert await ex.update_quality("solana") == 1
            pro = await db.wallet("solana", "PRO")
            assert pro["sig_n"] == 15 and pro["status"] == "inactive"
            # 再挖一次矿：被移出的 PRO 不会被拉回来（以前每次挖矿都把被移出的钱包重新激活，名单来回变）
            await db.kv_set("mined_tokens", [])
            await ex.mine_winners("solana")
            assert (await db.wallet("solana", "PRO"))["status"] == "inactive"
            # 一次性恢复：被挤出去的挖矿钱包恢复（信号质量差的 PRO 不恢复），只做一次
            await db.execute("UPDATE wallets SET status='inactive' WHERE address='RAT'")
            assert await ex.restore_pool_1112() == 1
            assert (await db.wallet("solana", "RAT"))["status"] == "active"
            assert (await db.wallet("solana", "PRO"))["status"] == "inactive"
            assert await ex.restore_pool_1112() == 0
            # 聪明钱库保留监听上限的 2 倍：库里 700 个、监听上限 300 → 留 600 个
            from bot.discovery.smart_money import SmartMoney
            for i in range(700):
                await db.upsert_wallet("solana", f"S{i}", status="active", source="t", realized_usd=1, win_rate=0.5,
                                       trades=10, score=i)
            sm = SmartMoney.__new__(SmartMoney)
            sm.app, sm.cfg = app, SimpleNamespace(get=lambda k, d=None: 300 if k == "smart_money.max_wallets_per_chain" else d)
            await sm.trim("solana")
            n = await db.fetchone("SELECT COUNT(*) n FROM wallets WHERE chain='solana' AND status='active'")
            assert n["n"] == 600
        finally:
            await db.close()

    asyncio.run(go())


def test_follow_quality_counts_dead_and_ranks_wallets(tmp_path):
    """跟单战绩：1 小时后取不到价格的按 -100% 算；监听名单 / 聪明钱库按"跟着买赚不赚"调整排名。"""
    from bot.discovery.expand import quality_from
    rows = [{"wallet": "A", "token": f"T{i}", "price": 1.0, "p60": -1 if i < 6 else 1.5} for i in range(10)]
    q = quality_from(rows)
    assert q["A"]["n"] == 10 and q["A"]["ret"] == -1.0 and q["A"]["dump"] == 0.6

    async def go():
        db = DB(tmp_path / "r.db")
        await db.open()
        try:
            # 评分高但跟着买亏（GOOD_PNL）vs 评分稍低但跟着买赚（FOLLOW）vs 样本不够（NEW）
            for addr, sc, n, ret, hit, dump in (("GOOD_PNL", 80, 20, -0.3, 0.1, 0.5), ("FOLLOW", 60, 12, 0.2, 0.4, 0.1),
                                                ("NEW", 70, 3, -0.9, 0, 1)):
                await db.upsert_wallet("solana", addr, status="active", source="t", realized_usd=1, win_rate=0.5,
                                       trades=10, score=sc)
                await db.execute("UPDATE wallets SET sig_n=?, sig_ret=?, sig_hit=?, sig_dump=? WHERE address=?",
                                 (n, ret, hit, dump, addr))
            order = [w["address"] for w in await db.active_wallets("solana", 10)]
            assert order == ["FOLLOW", "NEW", "GOOD_PNL"]       # 60+20+6=86 / 70（样本不够不调）/ 80-30-8=42
            from bot.db import rank_score
            assert abs(rank_score(await db.wallet("solana", "FOLLOW")) - 86) < 1e-9
        finally:
            await db.close()
    asyncio.run(go())
