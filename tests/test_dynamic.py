"""动态持仓管理：健康分、紧急卖出、保本、观察模式对比（不连网）。"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.db import DB  # noqa: E402
from bot.trade.executors import Fill  # noqa: E402
from bot.trade.health import DYN_DEFAULTS, score_health, smart_stats  # noqa: E402
from bot.trade.positions import URGENT, PositionManager, decide_exit  # noqa: E402

EXIT = {"stop_loss_pct": -35, "take_profit": [{"gain_pct": 100, "sell_pct": 50}, {"gain_pct": 300, "sell_pct": 50}],
        "trailing_start_pct": 60, "trailing_drop_pct": 30, "max_hold_hours": 24}
DC = dict(DYN_DEFAULTS)
H = 3600


def pos(**kw):
    base = {"entry_price": 1.0, "high_price": 1.0, "tp_stage": 0, "opened_at": 1000.0, "tier": "good"}
    base.update(kw)
    return base


def dyn(**kw):
    d = {"cfg": DC, "health": 70, "streak": 0, "emergency": "", "reduced": 0, "why": ""}
    d.update(kw)
    return d


def test_dynamic_decisions():
    t = 1000 + 0.5 * H
    # 紧急：价格没跌也立刻全卖
    pct, why = decide_exit(pos(), 1.0, EXIT, t, dyn(emergency="流动性被撤走"))
    assert pct == 100 and why.startswith(URGENT)
    # 固定规则（dyn=None）没有紧急/保本/健康分
    assert decide_exit(pos(), 1.0, EXIT, t, None)[0] == 0
    # 保本：涨过 +50% 跌回 +3% 以内卖；没涨过 +50% 不触发
    assert decide_exit(pos(high_price=1.55), 1.02, EXIT, t, dyn())[1].startswith("保本止损")
    assert decide_exit(pos(high_price=1.4), 1.02, EXIT, t, dyn())[0] == 0
    assert decide_exit(pos(high_price=1.55), 1.02, EXIT, t, None)[0] == 0
    # 健康分：偏低但只有 1 次不动；连续 2 次卖一半；卖过一半不重复；很低全卖
    assert decide_exit(pos(), 1.0, EXIT, t, dyn(health=35, streak=1))[0] == 0
    pct, why = decide_exit(pos(), 1.0, EXIT, t, dyn(health=35, streak=2, why="聪明钱 2/3 已卖出"))
    assert pct == 50 and "聪明钱" in why
    assert decide_exit(pos(), 1.0, EXIT, t, dyn(health=35, streak=3, reduced=1))[0] == 0
    assert decide_exit(pos(), 1.0, EXIT, t, dyn(health=20, streak=2, reduced=1))[0] == 100
    # 强势：第 2 档止盈暂缓，移动止损多容忍 10%，观察期翻倍
    assert decide_exit(pos(tp_stage=1, high_price=4.1), 4.1, EXIT, t, dyn(health=85))[0] == 0
    assert decide_exit(pos(tp_stage=1, high_price=4.1), 4.1, EXIT, t, dyn(health=70))[0] == 50
    assert decide_exit(pos(tp_stage=1, high_price=2.0), 1.3, EXIT, t, dyn(health=85))[0] == 0     # 回撤 35% < 40%
    assert decide_exit(pos(tp_stage=1, high_price=2.0), 1.3, EXIT, t, dyn(health=70))[0] == 100
    assert decide_exit(pos(), 1.1, EXIT, 1000 + 5 * H, dyn(health=85))[0] == 0                     # 较好档 4h→8h
    assert decide_exit(pos(), 1.1, EXIT, 1000 + 5 * H, dyn(health=70))[0] == 100
    # 翻倍出本在强势时照常执行
    assert decide_exit(pos(), 2.05, EXIT, t, dyn(health=90))[0] == 50
    # 取价失败什么都不做，紧急也不例外（拿不到价格时无法判断）
    assert decide_exit(pos(), 0.0, EXIT, t, dyn(emergency="x"))[0] == 0


ENTRY = {"price": 1.0, "liquidity": 100_000, "holders": 1000, "volume_1h": 50_000,
         "bundle": 10, "insider": 8, "dev": 3, "risks": []}
NO_SMART = {"smart": 0, "sold": 0, "new_buyers": 0}


def test_health_score():
    h, notes, em = score_health(ENTRY, None, dict(ENTRY), NO_SMART, 2, DC)
    assert h == 70 and not notes and not em
    # 价格跌一半，流动性按平方根同步下降，不算撤池子
    cur = {**ENTRY, "price": 0.5, "liquidity": 100_000 * 0.5 ** 0.5}
    assert score_health(ENTRY, None, cur, NO_SMART, 2, DC)[2] == ""
    # 价格不变流动性只剩 30% → 撤池子
    assert "流动性" in score_health(ENTRY, None, {**ENTRY, "liquidity": 30_000}, NO_SMART, 2, DC)[2]
    # dev 两次复查之间清仓 → 紧急；慢慢减仓 → 扣分
    assert "dev" in score_health(ENTRY, ENTRY, {**ENTRY, "dev": 0.1}, NO_SMART, 2, DC)[2]
    h, notes, em = score_health(ENTRY, {**ENTRY, "dev": 1.6}, {**ENTRY, "dev": 1.2}, NO_SMART, 2, DC)
    assert not em and h == 55
    # 聪明钱卖光 + 捆绑出货 + 持有人减少 → 低分
    cur = {**ENTRY, "bundle": 3, "holders": 800}
    h, notes, _ = score_health(ENTRY, None, cur, {"smart": 3, "sold": 3, "new_buyers": 0}, 2, DC)
    assert h == 70 - 30 - 20 - 10 and len(notes) == 3
    # 数据变好 → 高分
    cur = {**ENTRY, "holders": 1600, "volume_1h": 90_000}
    assert score_health(ENTRY, None, cur, {"smart": 3, "sold": 0, "new_buyers": 2}, 2, DC)[0] == 100
    # 新出现风险 → 直接 0 分（仍需连续确认，不算紧急）
    h, _, em = score_health(ENTRY, None, {**ENTRY, "risks": ["铸币权限未放弃（可无限增发）"]}, NO_SMART, 2, DC)
    assert h == 0 and not em
    # 拿到的流动性为 0（接口没数据）不当成撤池子
    assert score_health(ENTRY, None, {**ENTRY, "liquidity": 0}, NO_SMART, 2, DC)[2] == ""


# ---------------------------------------------------------------- 整体流程（假接口）
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


class FakeExec:
    def __init__(self):
        self.sells = []

    async def sell(self, token, raw, decimals, slippage_bps, urgent=False):
        self.sells.append((raw, slippage_bps, urgent))
        return Fill(True, "TX", raw, decimals, 0.05)


def test_full_flow(tmp_path):
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    cfg = Cfg(raw)
    msgs: list[str] = []
    state = {"price": 1.0, "rep": None}

    async def go():
        db = DB(tmp_path / "f.db")
        await db.open()
        ex = FakeExec()

        async def send(t, *a, **k):
            msgs.append(t)

        async def prices(chain, toks):
            return {t: state["price"] for t in toks}

        async def price(chain, tok):
            return state["price"]

        async def analyze(chain, tok, source="", force=False):
            return state["rep"]

        async def trade_sell(*a, **k):
            msgs.append("SELL " + a[3])

        async def native_price(chain):
            return 180.0

        tg = SimpleNamespace(send=send, trade_sell=trade_sell, trade_failed=send, mode=lambda: "💰 实盘")
        app = SimpleNamespace(cfg=cfg, db=db, tg=tg, native_price=native_price,
                              data=SimpleNamespace(prices=prices, price=price, usage_text=lambda: ""),
                              analyzer=SimpleNamespace(analyze=analyze),
                              trader=SimpleNamespace(exec={"solana": ex}), day_start=lambda: time.time() - 3600)
        pm = PositionManager(app)
        app.positions = pm
        now = time.time()
        entry = {"ts": now, "price": 1.0, "liquidity": 100_000, "holders": 1000, "volume_1h": 50_000,
                 "bundle": 10, "insider": 5, "dev": 0, "risks": []}
        pid = await db.execute(
            "INSERT INTO positions(chain,token,symbol,status,dry_run,reason,source_wallets,entry_price,cost_native,"
            "cost_usd,amount_raw,decimals,remaining_raw,high_price,opened_at,tier,entry_snap,health_at) "
            "VALUES('solana','TOK','TEST','open',0,'x','[]',1.0,0.1,18,'1000000',6,'1000000',1.0,?,?,?,0)",
            (now - 600, "good", json.dumps(entry)))

        # 1) 观察模式：建虚拟持仓 + 复查；流动性被撤走 → 新规则虚拟紧急卖出，真实持仓不动
        state["rep"] = {"price": 1.0, "liquidity": 20_000, "holders": 1000, "volume_1h": 50_000,
                        "bundle_held_pct": 10, "insider_held_pct": 5, "dev_held_pct": 0, "risks": []}
        assert await pm.mode() == "shadow"
        await pm.tick()
        await asyncio.gather(*pm._tasks)
        p = await db.fetchone("SELECT * FROM positions WHERE id=?", (pid,))
        assert p["health_emergency"] and p["health"] is not None
        await pm.tick()
        await asyncio.gather(*pm._tasks)
        assert not ex.sells
        sh = {s["variant"]: s for s in await db.fetchall("SELECT * FROM shadow WHERE pid=?", (pid,))}
        assert sh["v2"]["closed"] == 1 and sh["v1"]["closed"] == 0
        assert any("观察模式" in m for m in msgs)

        # 2) 切到接管：真实持仓紧急卖出，滑点调高、带 urgent
        await db.kv_set("dyn_mode", "live")
        await pm.tick()
        await asyncio.gather(*pm._tasks)
        assert ex.sells and ex.sells[0][2] is True and ex.sells[0][1] >= 3000
        p = await db.fetchone("SELECT * FROM positions WHERE id=?", (pid,))
        assert p["status"] == "closed" and p["close_reason"].startswith(URGENT)

        # 3) 复盘、对比、诊断都能正常出文字
        from bot.trade.review import daily_review, shadow_compare, why_text
        await db.execute("UPDATE shadow SET closed=1, last_price=1.0 WHERE pid=?", (pid,))
        assert "已对比 1 个" in await shadow_compare(app)
        assert "紧急卖出" in await daily_review(app, now - 3600)
        assert "健康分" in await why_text(app, pid)
        assert "没有这个持仓" in await why_text(app, 999)

        # 4) 聪明钱统计
        await db.execute("INSERT INTO wallets(chain,address,status,source,created_at) VALUES('solana','W1','active','x',0)")
        await db.execute("INSERT INTO wallets(chain,address,status,source,created_at) VALUES('solana','W2','active','x',0)")
        for w, side, ts in (("W1", "buy", now - 900), ("W1", "sell", now - 60), ("W2", "buy", now - 100)):
            await db.execute("INSERT INTO signals(ts,chain,wallet,token,side,tx) VALUES(?,?,?,?,?,?)",
                             (ts, "solana", w, "TOK", side, f"{w}{side}"))
        st = await smart_stats(db, {"chain": "solana", "token": "TOK", "opened_at": now - 600,
                                    "source_wallets": '["W1"]'})
        assert st == {"smart": 2, "sold": 1, "sources": 1, "sources_sold": 1, "new_buyers": 1}
        await db.close()

    asyncio.run(go())
