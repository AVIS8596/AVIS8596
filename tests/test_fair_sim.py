"""影子单公平入场：入场所在的那根 K 线不算、按 30 秒后第一根完整 K 线的开盘价买。"""
from bot.trade.replay import after, fair_entry, simulate, to_path


def c(t, o, h, lo, cl):
    return {"time": t * 1000, "open": o, "high": h, "low": lo, "close": cl}


def test_fair_entry_drops_partial_candle():
    t0 = 1_760_000_100.0                    # 在一根 5 分钟 K 线的中间被拦下
    cs = [c(1_760_000_000, 1.0, 3.0, 1.0, 2.0),          # 这根里 3.0 的高点在拦下之前就出现过
          c(1_760_000_300, 2.0, 2.2, 1.9, 2.1), c(1_760_000_600, 2.1, 2.3, 2.0, 2.2),
          c(1_760_000_900, 2.2, 2.4, 2.1, 2.3)]
    assert [x["open"] for x in after(cs, t0)] == [2.0, 2.1, 2.2]
    px0, path = fair_entry(cs, t0)
    assert px0 == 2.0 and path[0][0] >= t0
    rules = {"stop_loss_pct": -35, "take_profit": [{"gain_pct": 100, "sell_pct": 100}], "max_hold_hours": 24,
             "trailing_start_pct": 10000, "runner": {"enabled": False}}
    # 以前：按记下的价格 1.0 买、整根 K 线拆成点 → 一上来就"碰到" 3.0，翻倍止盈，+100% 以上
    old = simulate(1.0, t0, to_path(cs), dict(rules), None, 100, 0) - 1
    new = simulate(px0, t0, path, dict(rules), None, 100, 0) - 1
    assert old >= 1.0 and new < 0.2


def test_whyloss(tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace

    from bot.db import DB
    from bot.trade.whyloss import whyloss

    async def go():
        db = DB(tmp_path / "w.db")
        await db.open()
        try:
            now = time.time()
            t_open = (now - 6 * 3600) // 300 * 300 + 10

            async def kline(chain, token, res, a, b):
                # 第一次看到时 1.0，买入时已经 2.0，之后跌回 1.2
                out = []
                for i in range(-6, 60):
                    t = t_open // 300 * 300 + i * 300
                    px = 1.0 if i < -2 else (2.0 if i < 2 else 1.2)
                    out.append(c(t, px, px, px, px))
                return out
            cfg = SimpleNamespace(dry_run=True, get=lambda k, d=None: {"stop_loss_pct": -35, "take_profit": [],
                                                                       "max_hold_hours": 24} if k == "exit" else d)
            app = SimpleNamespace(db=db, cfg=cfg, gmgn=SimpleNamespace(kline=kline))
            for i in range(6):
                await db.execute("INSERT INTO positions(chain,token,symbol,status,dry_run,reason,entry_price,cost_usd,realized_usd,"
                                 "opened_at,close_reason) VALUES('solana',?,?,'closed',1,'跟单',2.0,100,40,?,?)",
                                 (f"T{i}", "T", t_open, "🚨紧急卖出" if i == 0 else "止损"))
                await db.execute("INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features) VALUES(?,?,?,?,?,?,?)",
                                 (t_open - 1500, "solana", f"T{i}", "T", "x", 1.0, "{}"))
            import bot.trade.whyloss as wl
            orig = asyncio.sleep

            async def fast(_s):
                await orig(0)
            wl.asyncio.sleep = fast
            try:
                txt = await whyloss(app, 4)
            finally:
                wl.asyncio.sleep = orig
            assert "A. 实际结果：每笔平均 -60.0%" in txt and "B. 同一笔入场" in txt and "C. 如果第一次看到就买" in txt
            assert "已经涨了 50% 以上才买" in txt and "紧急卖出" in txt
        finally:
            await db.close()
    asyncio.run(go())
