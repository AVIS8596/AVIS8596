"""入口。

  python main.py            启动机器人
  python main.py check      检查配置和所有 API key 是否可用（第一次部署先跑这个）
  python main.py discover   只跑一轮聪明钱发现然后退出
  python main.py backtest --days 7 [--apply]   回测聪明钱信号
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from bot.app import App
from bot.chains import CHAINS
from bot.config import ConfigError, load_config
from bot.utils import run_forever, setup_logging

BASE = Path(__file__).resolve().parent
log = logging.getLogger("memebot")


def _secrets(cfg) -> list[str]:
    s = cfg.secrets
    pem_lines = [ln for ln in s.gmgn_private_pem.splitlines() if ln and "-----" not in ln]
    return [s.telegram_token, s.helius_key, s.jupiter_key, s.birdeye_key, s.etherscan_key, s.zerox_key,
            s.solana_private_key, s.evm_private_key, s.gmgn_key, *pem_lines,
            *s.rpc_http.values(), *s.rpc_wss.values()]


async def daily_report(app: App) -> None:
    while True:
        now = datetime.now(app.tz)
        nxt = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        from bot.trade.review import daily_review
        try:
            await app.tg.send(await daily_review(app, app.day_start()))
        except Exception as e:  # noqa: BLE001
            log.exception("每日复盘失败: %s", e)


async def run_bot(cfg) -> None:
    from bot.monitor.evm_wallets import EvmWalletMonitor
    from bot.monitor.gmgn_feed import GmgnFeed
    from bot.monitor.new_tokens import NewTokenMonitor
    from bot.monitor.solana_wallets import SolanaWalletMonitor

    app = App(cfg)
    await app.start()
    bad = app.trader.setup()
    err = app.tg.error
    jobs: list = [
        ("TG发送", app.tg.sender), ("TG命令", app.tg.poller),
        ("聪明钱发现", app.smart.run), ("持仓管理", app.positions.run), ("每日总结", lambda: daily_report(app)),
    ]
    newtok = NewTokenMonitor(app)
    if "solana" in cfg.enabled_chains:
        sm = SolanaWalletMonitor(app)
        jobs.append(("Solana聪明钱监听", sm.run))
        if cfg.get("discovery.pumpportal_new_tokens", True):
            jobs.append(("pump新币", newtok.pumpportal))
    for c in cfg.evm_chains:
        m = EvmWalletMonitor(app, c)
        jobs.append((f"{c}聪明钱监听", m.run))
    if app.gmgn:
        from bot.strategies import Strategies
        strat = Strategies(app)
        app.strategies = strat
        for c in cfg.enabled_chains:
            jobs.append((f"{c}早期/叙事建仓", lambda c=c: strat.run(c)))
    for c in cfg.enabled_chains:
        if app.data.use_gmgn or c != "solana":
            jobs.append((f"{c}新币", lambda c=c: newtok.listings(c)))
        if app.gmgn:
            jobs.append((f"{c} GMGN交易流", GmgnFeed(app, c).run))

    tasks = [asyncio.create_task(run_forever(n, fn, err), name=n) for n, fn in jobs]

    mode = "🧪 模拟盘（不会花真钱）" if cfg.dry_run else "💰 实盘"
    src = "GMGN" if app.data.use_gmgn else "Birdeye"
    ex = "GMGN 钱包" if cfg.get("trading.executor", "gmgn") == "gmgn" else "自托管钱包"
    lines = [f"🤖 <b>扫链机器人已启动</b> {mode}", "链：" + "、".join(CHAINS[c].name for c in cfg.enabled_chains),
             f"数据源：{src} | 下单：{ex}"]
    for c in cfg.enabled_chains:
        lines.append(f"{CHAINS[c].name} 交易地址：<code>{app.trader.address(c)}</code>")
    if bad:
        lines.append("⚠️ 这些链的交易不可用：" + "、".join(bad))
    for w in getattr(cfg, "warnings", []):
        lines.append("⚠️ " + w)
    lines.append("发送 /help 查看命令")
    await app.tg.send("\n".join(lines))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    log.info("收到停止信号，正在退出……")
    await app.tg.send("🛑 机器人已停止（持仓不会自动卖出）")
    await asyncio.sleep(2)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.close()


async def check(cfg) -> bool:
    """逐项测试 API 连通性，告诉用户哪个 key 有问题。"""
    app = App(cfg)
    await app.start()
    ok_all = True

    async def item(name: str, coro) -> None:
        nonlocal ok_all
        try:
            r = await coro
            print(f"  ✅ {name}: {r}")
        except Exception as e:  # noqa: BLE001
            ok_all = False
            print(f"  ❌ {name}: {e}")

    import subprocess
    try:
        running = subprocess.run(["systemctl", "is-active", "memebot"], capture_output=True,
                                 text=True, timeout=5).stdout.strip() == "active"
    except Exception:  # noqa: BLE001
        running = False
    if running and app.gmgn:
        print("⚠️ 机器人正在后台运行，和检查程序共用 GMGN 额度，容易被限流。")
        print("   建议先执行 sudo systemctl stop memebot，等 2 分钟再检查，检查完再启动。\n")
    print("检查 API 连通性：")
    s = cfg.secrets
    await item("Telegram", _tg_check(app))
    if app.gmgn:
        await item("GMGN API Key", _gm_user(app))
        await item("GMGN 价格(SOL)", _nonzero(app.data.native_usd("solana")))
        await item("GMGN 聪明钱交易流 + 钱包胜率接口", _gm_feed(app))
    if app.birdeye.key:
        await item("Birdeye 价格(SOL)", _nonzero(app.birdeye.price("solana", CHAINS["solana"].wrapped)))
    if app.sol:
        await item("Helius RPC", app.sol.call("getSlot"))
    for c, rpc in app.evm.items():
        await item(f"{c} RPC", rpc.block_number())
    if not cfg.dry_run:
        bad = app.trader.setup()
        for c in cfg.enabled_chains:
            if c in bad:
                ok_all = False
                print(f"  ❌ {c} 交易初始化失败（检查私钥 / 钱包地址）")
                continue
            ex = app.trader.exec[c]
            await item(f"{c} 交易钱包 {app.trader.address(c)} 余额", _bal(ex, c))
        if cfg.get("trading.executor", "gmgn") == "gmgn":
            await item("GMGN 交易签名", _gm_sign(app))
        else:
            if "solana" in cfg.enabled_chains:
                await item("Jupiter key", app.http.get("jupiter", "https://api.jup.ag/swap/v2/order",
                                                        headers={"x-api-key": s.jupiter_key}, retries=0, params={
                        "inputMint": CHAINS["solana"].wrapped,
                        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "amount": "10000000"}))
            if cfg.evm_chains:
                await item("0x key", app.http.get("zerox", "https://api.0x.org/swap/allowance-holder/price",
                                                   headers={"0x-api-key": s.zerox_key, "0x-version": "v2"},
                                                   retries=0, params={
                        "chainId": 8453, "sellToken": CHAINS["base"].wrapped,
                        "buyToken": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "sellAmount": str(10 ** 16)}))
    await app.close()
    print("\n全部通过 ✅" if ok_all else "\n有项目未通过，请按上面的提示修改 .env ❌")
    return ok_all


async def _tg_check(app: App) -> str:
    r = await app.http.get("tg", f"{app.tg.base}/getMe")
    await app.http.post("tg", f"{app.tg.base}/sendMessage",
                        json_body={"chat_id": app.tg.chat, "text": "✅ 扫链机器人连通测试成功"})
    return "@" + r["result"]["username"] + "（已给你发了一条测试消息）"


async def _nonzero(coro) -> str:
    v = await coro
    if not v:
        raise RuntimeError("返回为空，检查 BIRDEYE_API_KEY")
    return f"${v:.2f}"


async def _gm_user(app: App) -> str:
    info = await app.gmgn.user_info()
    txt = str(info)
    return "可用。绑定信息：" + (txt[:300] + "…" if len(txt) > 300 else txt)


async def _gm_feed(app: App) -> str:
    trades = await app.gmgn.smartmoney("solana", 5)
    if not trades:
        return "交易流可用（暂时没有数据）"
    w = trades[0].get("maker")
    stats = await app.data.wallet_pnl("solana", [w], "30d")
    st = stats.get(w)
    if not st:
        return f"交易流 {len(trades)} 条，但钱包胜率接口没返回数据，请把日志发给开发者"
    return f"交易流 {len(trades)} 条；示例钱包30天利润 ${st['realized_usd']:.0f} 胜率 {st['win_rate'] * 100:.0f}%"


async def _gm_sign(app: App) -> str:
    """用一个不存在的订单号测试签名：签名错会报 401/403，签名对只会报"订单不存在"。"""
    from bot.sources.gmgn import GmgnError
    try:
        await app.gmgn.query_order("solana" if "solana" in app.cfg.enabled_chains else app.cfg.enabled_chains[0],
                                   "memebot-signature-test")
        return "签名通过"
    except GmgnError as e:
        if e.status in (401, 403) or "SIGN" in (e.error or "").upper() or "AUTH" in (e.error or "").upper():
            raise RuntimeError(f"签名或 IP 白名单不通过：{e}。检查私钥是否对应申请时的公钥、服务器 IP 是否加入白名单、是否关闭了 IPv6")
        return f"签名通过（测试订单号不存在属正常：{e.error or e.message}）"


async def _bal(ex, chain: str) -> str:
    b = await ex.native_balance()
    return f"{b:.4f} {CHAINS[chain].native_symbol}"


async def trenches_debug(cfg) -> None:
    """诊断主动建仓：看 GMGN 快毕业/已毕业列表实际返回了哪些字段、各条件卡掉了多少个币。"""
    from collections import Counter
    from bot.strategies import EARLY_DEFAULTS, NARRATIVE_DEFAULTS, narrative_score, structure_problem, volume
    from bot.utils import f
    app = App(cfg)
    items = await app.gmgn.trenches("solana", ["near_completion", "completed"], 80)
    await app.gmgn.close()
    print(f"共 {len(items)} 个：", dict(Counter(it.get("_stage") for it in items)))
    keys = ["holder_count", "volume_1h", "volume_24h", "swaps_1h", "created_timestamp", "renowned_count",
            "smart_degen_count", "x_user_follower", "twitter", "rug_ratio", "top_10_holder_rate",
            "bundler_trader_amount_rate", "rat_trader_amount_rate", "creator_balance_rate", "usd_market_cap"]
    print("字段有值的个数：", {k: sum(1 for it in items if it.get(k) not in (None, "", 0, "0")) for k in keys})
    now = time.time()
    fails: Counter = Counter()
    for it in items:
        nc = NARRATIVE_DEFAULTS
        if f(it.get("created_timestamp")) and now - f(it.get("created_timestamp")) > nc["max_age_hours"] * 3600:
            fails["超过24小时"] += 1
        elif int(f(it.get("holder_count"))) < nc["min_holders"]:
            fails[f"持有人<{nc['min_holders']}"] += 1
        elif volume(it) < nc["min_volume"]:
            fails["成交额不够"] += 1
        elif structure_problem(it, nc):
            fails["结构:" + structure_problem(it, nc).split(" ")[0]] += 1
        else:
            fails["通过基础条件"] += 1
    print("强叙事基础条件：", dict(fails))
    early = Counter()
    for it in items:
        if it.get("_stage") != "即将毕业":
            continue
        if int(f(it.get("holder_count"))) < EARLY_DEFAULTS["min_holders"]:
            early[f"持有人<{EARLY_DEFAULTS['min_holders']}"] += 1
        elif volume(it) < EARLY_DEFAULTS["min_volume"]:
            early["成交额不够"] += 1
        elif structure_problem(it, EARLY_DEFAULTS):
            early["结构:" + structure_problem(it, EARLY_DEFAULTS).split(" ")[0]] += 1
        else:
            early["通过(还要看增长)"] += 1
    print("早期建仓条件（快毕业的币）：", dict(early))
    scored = sorted(((narrative_score(it, 1, True)[0], it.get("symbol"), narrative_score(it, 1, True)[1])
                     for it in items), key=lambda x: -x[0])[:5]
    print("叙事分前 5：")
    for sc, sym, notes in scored:
        print(f"  {sym} {sc} 分：{'、'.join(notes)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check", "discover", "backtest", "trenches"])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    try:
        cfg = load_config(BASE)
    except ConfigError as e:
        print(e)
        sys.exit(1)
    setup_logging(cfg.path(cfg.get("general.log_dir", "logs")), cfg.get("general.log_level", "INFO"), _secrets(cfg))

    if a.cmd == "check":
        sys.exit(0 if asyncio.run(check(cfg)) else 1)
    elif a.cmd == "discover":
        async def go() -> None:
            app = App(cfg)
            await app.start()
            await app.smart.load_manual()
            for c in cfg.enabled_chains:
                n = await app.smart.refresh_chain(c)
                print(f"{c}: 新增 {n} 个聪明钱")
            await app.close()
        asyncio.run(go())
    elif a.cmd == "trenches":
        asyncio.run(trenches_debug(cfg))
    elif a.cmd == "backtest":
        async def bt() -> None:
            from bot.backtest import run_backtest
            app = App(cfg)
            await app.start()
            out = await run_backtest(app, a.days, a.limit, a.apply)
            print(out.replace("<b>", "").replace("</b>", ""))
            await app.close()
        asyncio.run(bt())
    else:
        t0 = time.time()
        asyncio.run(run_bot(cfg))
        log.info("运行了 %.1f 小时", (time.time() - t0) / 3600)


if __name__ == "__main__":
    main()
