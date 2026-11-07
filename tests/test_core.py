"""核心逻辑单元测试（不连网）：python -m pytest -q"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.analysis import bundle as bd  # noqa: E402
from bot.analysis.scoring import TokenFacts, evm_safety, score, solana_safety  # noqa: E402
from bot.backtest import evaluate_curve  # noqa: E402
from bot.discovery.smart_money import wallet_score  # noqa: E402
from bot.monitor.evm_wallets import parse_receipt  # noqa: E402
from bot.monitor.solana_wallets import parse_swaps  # noqa: E402
from bot.trade.positions import decide_exit  # noqa: E402
from bot.utils import TTLSet, fmt_price, fmt_usd  # noqa: E402

SOL = "So11111111111111111111111111111111111111112"


# ---------------------------------------------------------------- 捆绑识别
def test_bundle_detection():
    creator, funder = "DEV", "FUNDER"
    launch = 1000
    buys = [
        bd.EarlyBuy("B1", 1000, 0, 1.0, 10_000, multi_in_tx=True),   # 同slot+同笔交易
        bd.EarlyBuy("B2", 1000, 0, 1.0, 10_000, multi_in_tx=True),
        bd.EarlyBuy("I1", 1002, 0, 0.5, 5_000),                       # 新钱包早期买入 → 埋伏
        bd.EarlyBuy("S1", 1001, 0, 2.0, 3_000),                       # 老钱包早期 → 狙击
        bd.EarlyBuy("N1", 1050, 0, 0.3, 1_000),                       # 普通
        bd.EarlyBuy("DEV", 1000, 0, 2.0, 20_000),
    ]
    launch_ts = 1_700_000_000
    funding = {
        "B1": bd.Funding(funder, launch_ts - 3600), "B2": bd.Funding(funder, launch_ts - 3600),
        "DEV": bd.Funding(funder, launch_ts - 7200),
        "I1": bd.Funding("CEX", launch_ts - 1800),
        "S1": bd.Funding("CEX2", launch_ts - 400 * 86400),
    }
    holdings = {"B1": 10_000, "B2": 0, "I1": 5_000, "DEV": 20_000, "S1": 3_000}
    r = bd.detect(creator=creator, launch_slot=launch, launch_ts=launch_ts, buys=buys, funding=funding,
                  supply_raw=1_000_000, holdings=holdings)
    assert r.verdicts["DEV"].cls == "dev"
    assert r.verdicts["B1"].cls == "bundle" and r.verdicts["B2"].cls == "bundle"
    assert r.verdicts["I1"].cls == "insider"
    assert r.verdicts["S1"].cls == "sniper"
    assert r.verdicts["N1"].cls == "normal"
    assert r.bundle_held_pct == 1.0       # 只有 B1 还拿着 10000/1000000
    assert r.bundle_bought_pct == 2.0
    assert r.dev_held_pct == 2.0
    assert r.clusters and r.clusters[0]["dev_linked"]


def test_bundle_merges_repeat_buys_and_ignores():
    buys = [bd.EarlyBuy("A", 5, 0, 1, 100), bd.EarlyBuy("A", 6, 0, 1, 100), bd.EarlyBuy("POOL", 5, 0, 1, 1)]
    r = bd.detect(creator="", launch_slot=5, launch_ts=0, buys=buys, funding={}, supply_raw=1000,
                  holdings={}, ignore={"POOL"})
    assert "POOL" not in r.verdicts
    assert r.verdicts["A"].bought_raw == 200


def test_similar_amounts():
    buys = [bd.EarlyBuy(f"W{i}", 10, 0, 1.0 + i * 0.01, 1) for i in range(4)] + [bd.EarlyBuy("X", 10, 0, 9, 1)]
    s = bd._similar_amounts(buys, 0.1)
    assert {"W0", "W1", "W2", "W3"} <= s and "X" not in s


# ---------------------------------------------------------------- 评分与安全
def test_score_hard_fail():
    t = TokenFacts("bsc", "x", risks=["貔貅盘"])
    assert score(t)[0] == 0


def test_score_good_vs_bundled():
    good = TokenFacts("solana", "x", liquidity=60_000, holders=800, smart_buyers=3, buys_1h=300, sells_1h=100)
    bad = TokenFacts("solana", "x", liquidity=60_000, holders=800, smart_buyers=3, buys_1h=300, sells_1h=100,
                     bundle_held_pct=35, top10_pct=60)
    assert score(good)[0] > 80
    assert score(bad)[0] < score(good)[0] - 30


def test_solana_safety():
    risks, _ = solana_safety({"mintAuthority": "X", "freezeAuthority": None, "transferFeeBps": 0}, {})
    assert any("铸币" in r for r in risks)
    risks, _ = solana_safety({"mintAuthority": None, "freezeAuthority": None, "transferFeeBps": 300}, {})
    assert any("转账税" in r for r in risks)
    risks, _ = solana_safety({}, {})
    assert risks == []


def test_evm_safety():
    risks, warns, bt, st = evm_safety({"is_honeypot": "1", "sell_tax": "0.25", "buy_tax": "0.05"}, 10)
    assert any("貔貅" in r for r in risks) and any("卖出税" in r for r in risks)
    assert bt == pytest.approx(5) and st == pytest.approx(25)
    risks, warns, *_ = evm_safety({}, 10)
    assert not risks and warns


# ---------------------------------------------------------------- 止盈止损
EXIT = {"stop_loss_pct": -35, "take_profit": [{"gain_pct": 100, "sell_pct": 50}, {"gain_pct": 300, "sell_pct": 50}],
        "trailing_start_pct": 60, "trailing_drop_pct": 30, "max_hold_hours": 24}


def pos(**kw):
    base = {"entry_price": 1.0, "high_price": 1.0, "tp_stage": 0, "opened_at": 1000.0}
    base.update(kw)
    return base


def test_exit_rules():
    assert decide_exit(pos(), 0.6, EXIT, 1001)[0] == 100                      # 止损
    assert decide_exit(pos(), 0.0, EXIT, 1001)[0] == 0                        # 取价失败绝不卖
    assert decide_exit(pos(), 2.1, EXIT, 1001) == (50, "止盈·翻倍出本（+110%，卖出 50% 收回本金）")
    assert decide_exit(pos(tp_stage=1, high_price=2.1), 2.1, EXIT, 1001)[0] == 0
    assert decide_exit(pos(tp_stage=1, high_price=2.0), 1.3, EXIT, 1001)[0] == 100   # 移动止损
    assert decide_exit(pos(), 1.1, EXIT, 1000 + 25 * 3600)[0] == 100          # 超时
    assert decide_exit(pos(), 1.1, EXIT, 1001)[0] == 0
    assert decide_exit(pos(), 1.1, EXIT, 1000 + 3 * 3600)[0] == 0              # 老持仓（无档位）没有"涨不上去"规则


def test_exit_tiers():
    h = 3600
    # 一般档：1 小时还没到 +15% 就卖；到了就继续拿；6 小时超时
    assert decide_exit(pos(tier="normal"), 1.1, EXIT, 1000 + 0.5 * h)[0] == 0
    pct, why = decide_exit(pos(tier="normal"), 1.1, EXIT, 1000 + 1.5 * h)
    assert pct == 100 and why.startswith("涨不上去")
    assert decide_exit(pos(tier="normal"), 1.2, EXIT, 1000 + 1.5 * h)[0] == 0
    assert decide_exit(pos(tier="normal"), 1.2, EXIT, 1000 + 7 * h)[0] == 100
    # 优质档：同样 +10%，5 小时不卖，13 小时卖；+50% 拿到 30 小时也不卖（一般档早超时了）
    assert decide_exit(pos(tier="best"), 1.1, EXIT, 1000 + 5 * h)[0] == 0
    assert decide_exit(pos(tier="best"), 1.1, EXIT, 1000 + 13 * h)[0] == 100
    assert decide_exit(pos(tier="best", high_price=1.5), 1.5, EXIT, 1000 + 30 * h)[0] == 0
    # 优质档翻倍出本卖 50%，第 2 档只卖 30%；回撤 30% 不触发（阈值 35%），较好档会触发
    assert decide_exit(pos(tier="best"), 2.1, EXIT, 1001)[0] == 50
    assert decide_exit(pos(tier="best", tp_stage=1, high_price=4.1), 4.1, EXIT, 1001)[0] == 30
    assert decide_exit(pos(tier="best", tp_stage=1, high_price=2.0), 1.4, EXIT, 1001)[0] == 0
    assert decide_exit(pos(tier="good", tp_stage=1, high_price=2.0), 1.4, EXIT, 1001)[0] == 100
    # 已经止盈过的不受"涨不上去"影响
    assert decide_exit(pos(tier="normal", tp_stage=1, high_price=1.1), 1.05, EXIT, 1000 + 2 * h)[0] == 0
    # config 里的覆盖优先于默认
    cfg = {**EXIT, "tiers": {"normal": {"stall_hours": 3}}}
    assert decide_exit(pos(tier="normal"), 1.1, cfg, 1000 + 2 * h)[0] == 0


# ---------------------------------------------------------------- 交易解析
def _sol_tx(wallet, pre_sol, post_sol, pre_tok, post_tok, mint="MEME", fee=5000):
    return {
        "slot": 1, "blockTime": 1700000000,
        "transaction": {"signatures": ["SIG"], "message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "POOL"}]}},
        "meta": {"err": None, "fee": fee, "preBalances": [pre_sol, 0], "postBalances": [post_sol, 0],
                 "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(pre_tok), "decimals": 6}}],
                 "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(post_tok), "decimals": 6}}]},
    }


def test_parse_solana_buy_sell():
    buy = parse_swaps(_sol_tx("W", 10_000_000_000, 8_999_995_000, 0, 5_000_000), "W", "SIG")
    assert len(buy) == 1 and buy[0].side == "buy" and buy[0].native_amount == pytest.approx(1.0)
    sell = parse_swaps(_sol_tx("W", 1_000_000_000, 2_999_995_000, 5_000_000, 0), "W", "SIG")
    assert sell[0].side == "sell"
    # 只是收到空投（SOL 没减少）不算买入
    air = parse_swaps(_sol_tx("W", 1_000_000_000, 999_995_000, 0, 5_000_000), "W", "SIG")
    assert air == []


def test_parse_solana_wsol_counts_as_sol():
    tx = _sol_tx("W", 1_000_000_000, 999_995_000, 0, 5_000_000)
    tx["meta"]["preTokenBalances"].append({"owner": "W", "mint": SOL, "uiTokenAmount": {"amount": "2000000000", "decimals": 9}})
    tx["meta"]["postTokenBalances"].append({"owner": "W", "mint": SOL, "uiTokenAmount": {"amount": "0", "decimals": 9}})
    s = parse_swaps(tx, "W", "SIG")
    assert s and s[0].side == "buy" and s[0].native_amount == pytest.approx(2.0)


def test_parse_evm_receipt():
    T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    w = "0x" + "a" * 40
    pool = "0x" + "b" * 40
    tok = "0x" + "c" * 40
    pad = lambda a: "0x" + a[2:].rjust(64, "0")  # noqa: E731
    rc = {"logs": [{"address": tok, "topics": [T, pad(pool), pad(w)], "data": hex(10 ** 18)}]}
    tx = {"hash": "0xh", "from": w, "value": hex(5 * 10 ** 16)}
    s = parse_receipt("base", w, tx, rc)
    assert s[0].side == "buy" and s[0].native_amount == pytest.approx(0.05)
    rc2 = {"logs": [{"address": tok, "topics": [T, pad(w), pad(pool)], "data": hex(10 ** 18)}]}
    s2 = parse_receipt("base", w, {"hash": "0xh", "from": w, "value": "0x0"}, rc2)
    assert s2[0].side == "sell"


# ---------------------------------------------------------------- 其他
def test_wallet_score_range():
    lo = wallet_score(15000, 15000, 0.6, 20)
    hi = wallet_score(150000, 15000, 0.8, 200)
    assert 40 < lo < hi <= 100
    assert wallet_score(150000, 15000, 0.8, 200, follow_wins=0, follow_losses=10) < hi


def test_backtest_curve():
    pts = [(0, 1.0), (3600, 2.0), (6 * 3600, 1.5), (24 * 3600, 0.5)]
    r = evaluate_curve(pts, 0)
    assert r["max"] == pytest.approx(100) and r["r6h"] == pytest.approx(50) and r["r24h"] == pytest.approx(-50)


def test_utils():
    s = TTLSet(60)
    assert s.add_if_new("a") and not s.add_if_new("a")
    assert fmt_usd(1234567) == "$1.23M" and fmt_usd(-50) == "-$50.00"
    assert fmt_price(0.00000123) == "0.0{5}123"


def test_db_roundtrip(tmp_path):
    from bot.db import DB

    async def go():
        db = DB(tmp_path / "t.db")
        await db.open()
        await db.upsert_wallet("solana", "W", status="active", source="t", realized_usd=1, win_rate=0.7, trades=20, score=60)
        await db.execute("UPDATE wallets SET status='manual' WHERE address='W'")
        await db.upsert_wallet("solana", "W", status="inactive", source="t", realized_usd=1, win_rate=0.1, trades=20, score=10)
        w = await db.wallet("solana", "W")
        assert w["status"] == "manual"         # 手动添加的不会被自动降级
        await db.kv_set("x", {"a": 1})
        assert await db.kv_get("x") == {"a": 1}
        await db.close()

    asyncio.run(go())


def test_solana_partial_sign():
    """验证签名逻辑能处理 Jupiter / PumpPortal 返回的未签名交易。"""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    from bot.trade.executors import SolanaExecutor

    kp = Keypair()
    ix = Instruction(Pubkey.default(), b"", [AccountMeta(kp.pubkey(), True, True)])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    unsigned = VersionedTransaction.populate(msg, [Signature.default()])
    ex = SolanaExecutor.__new__(SolanaExecutor)
    ex.kp = kp
    signed = VersionedTransaction.from_bytes(ex._sign(bytes(unsigned)))
    assert signed.signatures[0] != Signature.default()
    assert signed.verify_with_results() == [True]


def test_position_tiers_and_sizes(tmp_path):
    from types import SimpleNamespace
    from bot.db import DB
    from bot.trade.trader import Trader

    cfg = SimpleNamespace(get=lambda k, d=None: d,
                          per_chain=lambda k, c, d=None: {"trading.buy_amount_native": 0.05}.get(k, d))

    async def go():
        db = DB(tmp_path / "s.db")
        await db.open()
        t = Trader.__new__(Trader)
        t.cfg, t.app = cfg, SimpleNamespace(db=db)
        assert t.tier({"score": 90, "bundle_held_pct": 5}, []) == "best"
        assert t.tier({"score": 90, "bundle_held_pct": 25}, []) == "good"      # 捆绑太多不给优质
        assert t.tier({"score": 60, "bundle_held_pct": 2}, ["a", "b", "c"]) == "best"
        assert t.tier({"score": 60, "bundle_held_pct": 2}, ["a", "b"]) == "good"
        assert t.tier({"score": 60, "bundle_held_pct": 2}, ["a"]) == "normal"
        assert await t.sizes("solana") == [0.05, 0.1, 0.2]
        await db.kv_set("sizes", {"solana": [0.1, 0.3, 0.5]})
        assert await t.sizes("solana") == [0.1, 0.3, 0.5]
        await db.close()

    asyncio.run(go())
