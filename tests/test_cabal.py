from bot.trade.trader import cabal_signs, ladder, skip_cat

T = {"max_entrap_pct": 50, "max_mcap_liq_ratio": 30}


def test_entrap_blocks():
    why = cabal_signs({"entrap_pct": 79.1, "liquidity": 30550, "mcap": 189090}, T)
    assert why.startswith("钓鱼钱包") and skip_cat(why) == "钓鱼钱包多"
    assert cabal_signs({"entrap_pct": 12, "liquidity": 30550, "mcap": 189090}, T) == ""


def test_mcap_liq_blocks_xto():
    why = cabal_signs({"entrap_pct": 0, "liquidity": 833_710, "mcap": 122_590_000}, T)
    assert why.startswith("市值是池子") and skip_cat(why) == "市值虚高"


def test_wash_flag():
    assert skip_cat(cabal_signs({"wash": 1}, T)) == "刷量盘"


def _bars(closes):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"time": 1_700_000_000_000 + i * 60_000, "open": prev, "close": c})
        prev = c
    return out


def test_ladder_smooth_vs_organic():
    smooth = _bars([1 * 1.015 ** i for i in range(40)])          # 根根收阳、没有回调
    why = ladder(smooth, {})
    assert why.startswith("拉盘太平滑") and skip_cat(why) == "拉盘太平滑"
    org, px = [], 1.0
    for i in range(40):                                           # 涨得多但有回调
        px *= 1.08 if i % 3 else 0.88
        org.append(px)
    assert ladder(_bars(org), {}) == ""
    assert ladder(smooth[:10], {}) == ""                          # K 线太少不判断


def test_ws_prefers_unmetered_nodes():
    import time
    from bot.sources.solana_rpc import SolanaRPC

    class H:
        def set_rate(self, *a, **k):
            pass
    sol = SolanaRPC(H(), "k", 10, extra_urls=[("https://solana-mainnet.core.chainstack.com/abcdefabcdefabcdefabcdefabcdefab", ""),
                                             ("https://api.mainnet-beta.solana.com", "")])
    names = [e.name for e in sol.eps]
    assert sol.ws_pick().name == "Solana 公共节点", names       # 不限次数的先用，有月额度的（Helius / Chainstack）放后面
    pub = sol.eps[-1]
    pub.ws_out_until = time.time() + 600
    assert sol.ws_pick().name in ("Helius", "Chainstack")
    hel = sol.eps[0]
    sol.ws_note(hel, 10_000_000)                                 # 推送也算用量：Helius 今天超预算了
    assert sol.ws_pick().name != "Helius"
    assert sol.ws_should_move(hel)
