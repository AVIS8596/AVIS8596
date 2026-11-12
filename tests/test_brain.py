"""自我学习：每笔复盘、漏掉的好币、自动调参（不连网）。"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.brain import (EARLY_PARAMS, ENTRY_PARAMS, NEWTOKEN_KEYS, Brain, Case, early_entry, entry_pass,  # noqa: E402
                       lesson, missed_winners, proxy_return, tune)
from bot.db import DB  # noqa: E402


class Cfg:
    def __init__(self, raw, dry=True):
        self.raw, self.dry_run = raw, dry

    def get(self, dotted, default=None):
        cur = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def test_proxy_return():
    assert proxy_return(1.0, 0.6, 3.0) == -0.35 - 0.04          # 1 小时内跌破 -35%：按止损算
    assert abs(proxy_return(1.0, 1.2, 2.0) - 0.96) < 1e-9
    assert proxy_return(1.0, 1.2, 100.0) == 5.0 - 0.04          # 封顶 +500%
    assert proxy_return(1.0, None, None) is None
    # 之后取不到价格（-1）：默认跳过；调参用 dead=True 按亏光算，不再只统计活下来的币
    assert proxy_return(1.0, -1, None) is None and proxy_return(1.0, -1, None, dead=True) == -1.0
    assert proxy_return(1.0, 1.2, -1, dead=True) == -1.0
    assert abs(proxy_return(1.0, -1, 2.0, dead=True) - 0.96) < 1e-9    # 1 小时取不到、4 小时又有价：数据源一时出错
    assert proxy_return(1.0, 0.5, -1, dead=True) == -0.35 - 0.04        # 先跌破止损已经卖了
    # 有影子采样（按真实卖出规则完整模拟）就用它；-8 / -9 是"没采 / 没 K 线"
    assert proxy_return(1.0, 1.2, 3.0, sim=-0.4) == -0.4 and proxy_return(1.0, 1.2, 3.0, sim=-8) == 3.0 - 1 - 0.04


def test_lesson_tags():
    snap = json.dumps({"launch_x": 14, "change_1h": 25})
    assert lesson({"cost_usd": 100, "realized_usd": 65, "close_reason": "止损 -35%", "post_max": 1.8,
                   "entry_snap": snap}) == ["买在高位", "止损后又涨回来"]
    assert lesson({"cost_usd": 100, "realized_usd": 65, "close_reason": "止损", "post_max": 1.05}) == ["止损及时"]
    assert lesson({"cost_usd": 100, "realized_usd": 250, "close_reason": "止盈", "post_max": 2.5}) == ["拿住大赢家", "卖早了"]


def _cur():
    return {p.key: float(p.default) for p in ENTRY_PARAMS}


def test_tune_loosens_when_both_halves_agree():
    cases = []
    for i in range(200):
        ft = {"score": 72 if i % 2 else 80, "smart": 2, "liquidity": 50000, "holders": 500, "age_hours": 2,
              "change_1h": 50, "top10_pct": 20, "bundle_held_pct": 5, "insider_held_pct": 1, "risks": 0}
        ret = 0.5 if ft["score"] == 72 else 0.1          # 评分 70~75 的币之后都涨
        cases.append(Case(i, lambda v, ft=ft, ret=ret: ret if entry_pass(ft, v) else None))
    r = tune(cases, tuple(p for p in ENTRY_PARAMS if p.key in NEWTOKEN_KEYS), _cur())
    assert r and r["key"] == "trading.auto_min_score" and r["new"] == 70 and r["n_new"] > r["n_old"]


def test_tune_rejects_luck_and_one_sided():
    # 新加进来的币里只有一个大赢家，其余都亏：去掉最赚的一个就不成立
    cases = []
    for i in range(200):
        s = 72 if i % 2 else 80
        ft = {"score": s, "smart": 2, "liquidity": 50000, "holders": 500, "age_hours": 2, "change_1h": 50,
              "top10_pct": 20, "bundle_held_pct": 5, "insider_held_pct": 1, "risks": 0}
        ret = (4.96 if i == 1 else -0.2) if s == 72 else 0.1
        cases.append(Case(i, lambda v, ft=ft, ret=ret: ret if entry_pass(ft, v) else None))
    assert tune(cases, tuple(p for p in ENTRY_PARAMS if p.key in NEWTOKEN_KEYS), _cur()) is None
    assert tune(cases[:30], ENTRY_PARAMS, _cur()) is None      # 样本太少不学


def test_early_entry_matches_strategy():
    base = {"max_age_min": 180, "growth_window_min": 3, "max_rug_ratio": 0.2, "max_creator_rate": 0.05}
    cur = {p.key: float(p.default) for p in EARLY_PARAMS}
    s0 = {"rug": 0, "top10": 0.2, "bundler": 0.05, "insider": 0.05, "creator": 0, "wash": 0, "created": 0,
          "volume": 20000, "price": 1}
    ss = [dict(s0, ts=0, holders=100), dict(s0, ts=150, holders=110), dict(s0, ts=300, holders=140)]
    assert early_entry(ss, cur, base) == 2                       # 300 秒前 100 → 140（+40%，要按 5 分钟折算 25%）
    loose = {**cur, "strategies.early.min_holder_growth_pct": 6}
    assert early_entry(ss, loose, base) == 1                     # 放宽后早一份快照买入（110，+10%）
    assert early_entry([dict(s, insider=0.3) for s in ss], cur, base) is None


def test_brain_apply_reset_and_missed(tmp_path):
    async def go():
        db = DB(tmp_path / "b.db")
        await db.open()
        raw = {"trading": {"auto_min_score": 75, "auto_min_cluster": 2}, "learning": {"auto_tune": "auto"}}
        app = SimpleNamespace(db=db, cfg=Cfg(raw))
        br = Brain(app)
        await br.apply("trading.auto_min_score", 70, "测试")
        assert app.cfg.get("trading.auto_min_score") == 70
        raw2 = {"trading": {"auto_min_score": 75}}
        app2 = SimpleNamespace(db=db, cfg=Cfg(raw2))
        await db.kv_set("data_fix_v1", True)
        await Brain(app2).load()                                  # 重启后自动加载
        assert raw2["trading"]["auto_min_score"] == 70
        assert await br.reset() == 1 and app.cfg.get("trading.auto_min_score") == 75
        assert "还没有改过参数" in await br.status_text()
        # 漏掉的好币：看过、4 小时后涨 5 倍、没买，卡在"新币评分不够"
        t0 = time.time() - 6 * 3600
        await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features,p60,p240) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (t0, "solana", "W", "WIN", "x", 1.0,
                                                       json.dumps({"score": 50, "smart": 1}), 2.0, 5.0))
        await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features,p60,p240) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (t0, "solana", "L", "LOSE", "x", 1.0, "{}", 0.5, 0.2))
        await db.execute("INSERT INTO skipped(ts,chain,token,symbol,trigger,cat,reason,price) VALUES(?,?,?,?,?,?,?,?)",
                         (t0 + 60, "solana", "L", "LOSE", "跟单", "捆绑多", "捆绑地址持有 30%", 1.0))
        m = await missed_winners(app)
        assert m["seen"] == 2 and m["n_x"] == 1 and m["missed"][0][1] == "WIN"
        assert "新币评分不够" in m["missed"][0][2]
        await db.close()
    asyncio.run(go())


def test_strategy_records_early_samples(tmp_path):
    from collections import deque
    from bot.strategies import Strategies
    from tests.test_strategies import CLEAN

    async def go():
        db = DB(tmp_path / "s.db")
        await db.open()
        st = Strategies(SimpleNamespace(cfg=SimpleNamespace(get=lambda k, d=None: d), db=db,
                                        hub=SimpleNamespace(_tasks=set())))

        async def fire(*a):
            pass
        st.fire = fire
        t0 = time.time()
        it = dict(CLEAN, price=0.001, created_timestamp=t0 - 600)
        st.hist[("solana", "TOK1")] = deque([(t0 - 180, 200)])
        await st.poll("solana", [it], now=t0)
        await st.poll("solana", [it], now=t0 + 30)                # 2.5 分钟内不重复记
        await st.poll("solana", [it], now=t0 + 160)
        await asyncio.sleep(0.05)
        n = await db.fetchone("SELECT COUNT(*) n FROM early_obs")
        tok = await db.fetchall("SELECT * FROM early_tok")
        assert n["n"] == 2 and len(tok) == 1 and abs(tok[0]["first_ts"] - t0) < 1
        await db.close()
    asyncio.run(go())


def test_tune_once_end_to_end(tmp_path):
    import random
    random.seed(1)

    async def go():
        db = DB(tmp_path / "t.db")
        await db.open()
        raw = {"trading": {"auto_min_score": 75, "auto_min_cluster": 2, "min_liquidity_usd": 8000},
               "learning": {"auto_tune": "auto"}}
        sent = []
        app = SimpleNamespace(db=db, cfg=Cfg(raw), tg=SimpleNamespace(send=lambda m: sent.append(m)))
        now = time.time()
        for i in range(300):
            s = random.choice([65, 72, 80])
            ft = {"score": s, "smart": 2, "liquidity": 50000, "holders": 500, "age_hours": 2, "change_1h": 50,
                  "top10_pct": 20, "bundle_held_pct": 5, "insider_held_pct": 1, "risks": 0}
            p240 = 1.6 if s == 72 else (0.9 if s == 80 else 0.5)
            await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features,p15,p60,p240) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (now - 3 * 86400 + i * 600, "solana", f"T{i}", "S", "x", 1.0, json.dumps(ft), 1, 1.1, p240))
        for i in range(60):
            tk = f"E{i}"
            await db.execute("INSERT INTO early_tok(chain,token,first_ts,p60,p240) VALUES(?,?,?,?,?)",
                             ("solana", tk, now - 86400 + i * 60, 1.2, 2.0))
            for k, h in enumerate((100, 108, 116)):
                await db.execute("INSERT INTO early_obs(ts,chain,token,price,holders,volume,rug,top10,bundler,insider,"
                                 "creator,wash,smart,kol,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (now - 86400 + i * 60 + k * 150, "solana", tk, 1.0, h, 20000, 0, 0.2, 0.05, 0.05, 0, 0,
                                  0, 0, 0))
        br = Brain(app)
        msgs = await br.tune_once(force=True)
        assert any("自我调参" in m for m in msgs), msgs
        assert raw["trading"]["auto_min_score"] == 70 or raw.get("strategies")
        st = await br.status_text()
        assert "学到的参数" in st
        await db.close()
    asyncio.run(go())


def test_entry_cases_count_dead_and_use_shadow(tmp_path):
    """调参数据：取不到价格的币按亏光算（以前直接跳过），有影子采样就用影子采样的结果。"""
    async def go():
        db = DB(tmp_path / "e.db")
        await db.open()
        t0 = time.time() - 6 * 3600
        ft = json.dumps({"score": 80, "smart": 2, "liquidity": 50000, "holders": 500})
        for tok, p60, p240, sim in (("A", 1.2, 2.0, None), ("B", -1, -1, None), ("C", 1.1, 3.0, -0.3),
                                    ("D", 1.1, 1.5, -8)):
            await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features,p60,p240,sim_ret) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)", (t0, "solana", tok, tok, "x", 1.0, ft, p60, p240, sim))
        br = Brain(SimpleNamespace(db=db, cfg=Cfg({"trading": {}})))
        newtok, _ = await br.entry_cases()
        assert len(newtok) == 4
        assert br.data_note == {"n": 4, "sim": 1, "dead": 1}
        cur = br.current(ENTRY_PARAMS)
        rets = sorted(c.ret_fn(cur) for c in newtok if c.ret_fn(cur) is not None)
        assert rets[0] == -1.0 and -0.3 in rets
        await db.close()
    asyncio.run(go())


def test_tune_pending_multiple(tmp_path):
    """同时有好几条调参建议：各自有编号，同一参数的新建议替换旧的。"""
    async def go():
        db = DB(tmp_path / "p.db")
        await db.open()
        br = Brain(SimpleNamespace(db=db, cfg=Cfg({"trading": {}})))
        assert await br.add_pending({"key": "exit.stop_loss_pct", "new": -25, "why": "止损"}) == 1
        assert await br.add_pending({"key": "trading.min_smart", "new": 1, "why": "聪明钱"}) == 2
        assert await br.add_pending({"key": "exit.stop_loss_pct", "new": -20, "why": "止损2"}) == 2
        ps = await br.pending()
        assert [p["why"] for p in ps] == ["聪明钱", "止损2"]
        await db.kv_set("tune_pending", {"key": "x", "new": 1, "why": "旧格式"})
        assert len(await br.pending()) == 1
        await db.close()
    asyncio.run(go())
