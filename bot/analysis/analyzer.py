"""代币完整分析：安全检测 + 早期买家还原 + 捆绑/埋伏识别 + 评分。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from solders.pubkey import Pubkey

from ..chains import TRANSFER_TOPIC, norm_addr
from ..sources.birdeye import BudgetExceeded
from ..sources.evm_rpc import topic_to_addr
from ..sources.solana_rpc import signer, wallet_deltas
from ..utils import dig, f
from . import bundle as bd
from .scoring import TokenFacts, evm_safety, score, solana_safety

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.analyzer")


def on_curve(addr: str) -> bool:
    """Solana：普通钱包在曲线上；池子、程序派生地址（PDA）不在曲线上。用来排除池子。"""
    try:
        return Pubkey.from_string(addr).is_on_curve()
    except Exception:  # noqa: BLE001
        return False


def _pct(v: Any) -> float:
    x = f(v)
    return x * 100 if 0 < x <= 1 else x


async def _empty_dict() -> dict:
    return {}


async def _safe(coro, default):
    """节点/接口临时出错时返回默认值，不让整个分析失败。"""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.debug("查询失败（已跳过）: %s", e)
        return default


def _no(v) -> bool:
    """GMGN 的"是否已放弃"字段：0 / false / "no" 表示没放弃。None 表示未知，不算。"""
    return v is not None and str(v).lower() in ("0", "false", "no")


def merge_gmgn_holders(holders: list[dict], buys: list[bd.EarlyBuy], funding: dict[str, bd.Funding],
                       supply: int, launch_slot: int, launch_ts: float, creator: str, ov: dict) -> dict[str, int]:
    """把 GMGN 前 100 持币人的标签、首笔入金来源合并进我们自己的分析。

    - 已在早期买家里的钱包：补上标签
    - GMGN 标记为 bundler / rat_trader / sniper 但我们没还原到的钱包：作为早期买家加入
    - 首笔入金地址（native_transfer）补进资金来源，用于"同一出资人"判断
    返回 {钱包: 当前持仓原始数量}（用 GMGN 的持仓比例换算，省掉逐个查余额）
    """
    by_w = {b.wallet: b for b in buys}
    held: dict[str, int] = {}
    for h in holders or []:
        w = h.get("address")
        if not w or int(f(h.get("addr_type"))) == 2:     # 2 = 交易所 / 池子
            continue
        tags = {str(t).lower() for t in (h.get("maker_token_tags") or []) + (h.get("tags") or [])}
        mapped = set()
        if "bundler" in tags:
            mapped.add("bundler(GMGN)")
        if "rat_trader" in tags:
            mapped.add("insider(GMGN老鼠仓)")
        if "sniper" in tags:
            mapped.add("sniper(GMGN)")
        pct = f(h.get("amount_percentage"))
        if supply:
            held[w] = int(pct * supply)
        if w in by_w:
            by_w[w].tags |= mapped
        elif mapped and w != creator:
            slot = launch_slot + 1   # 只知道是早期买家，不知道具体区块，不伪造"同一区块"证据
            eb = bd.EarlyBuy(w, slot, f(h.get("start_holding_at")), 0.0,
                             held.get(w, 0), tags=mapped)
            buys.append(eb)
            by_w[w] = eb
        nt = h.get("native_transfer") or {}
        src = dig(nt, "address", "from_address", "from", default="") if isinstance(nt, dict) else ""
        if src and w not in funding:
            funding[w] = bd.Funding(src, f(dig(nt, "timestamp", "block_timestamp", "time")))
    dev = ov.get("dev") or {}
    if creator and dev.get("fund_from") and creator not in funding:
        funding[creator] = bd.Funding(dev["fund_from"], f(dev.get("fund_from_ts")))
    return held


class Analyzer:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future] = {}
        self._sem = asyncio.Semaphore(3)   # 同时最多分析 3 个币，防止打爆接口

    async def analyze(self, chain: str, token: str, source: str = "", force: bool = False) -> dict | None:
        token = norm_addr(chain, token)
        key = (chain, token)
        ttl = self.cfg.get("discovery.analysis_cache_min", 10) * 60
        hit = self._cache.get(key)
        if hit and not force and time.time() - hit[0] < ttl:
            rep = dict(hit[1])
            rep["smart_buyers"] = await self.smart_buyers(chain, token)   # 聪明钱数量实时更新
            return rep
        if key in self._inflight:     # 同一个币正在分析，等结果，不重复请求
            return await asyncio.shield(self._inflight[key])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            async with self._sem:
                rep = await self._analyze(chain, token, source)
            if rep:
                self._cache[key] = (time.time(), rep)
                await self._save(rep, source)
            fut.set_result(rep)
            return rep
        except BudgetExceeded as e:
            log.warning("%s", e)
            fut.set_result(None)
            return None
        except Exception as e:  # noqa: BLE001
            log.exception("分析 %s %s 失败: %s", chain, token, e)
            fut.set_result(None)
            return None
        finally:
            if not fut.done():
                fut.set_result(None)
            self._inflight.pop(key, None)
            if len(self._cache) > 5000:
                for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:1000]:
                    self._cache.pop(k, None)

    async def smart_buyers(self, chain: str, token: str, hours: float = 24) -> list[str]:
        rows = await self.app.db.fetchall(
            "SELECT DISTINCT s.wallet FROM signals s JOIN wallets w ON w.chain=s.chain AND w.address=s.wallet "
            "WHERE s.chain=? AND s.token=? AND s.side='buy' AND s.ts>=? AND w.status IN ('active','manual')",
            (chain, token, time.time() - hours * 3600))
        return [r["wallet"] for r in rows]

    # ================================================================== 主流程
    async def _analyze(self, chain: str, token: str, source: str) -> dict | None:
        ov = await self.app.data.overview(chain, token)
        facts = TokenFacts(
            chain=chain, address=token, symbol=ov["symbol"], name=ov["name"], price=ov["price"],
            liquidity=ov["liquidity"], mcap=ov["mcap"], holders=ov["holders"], volume_1h=ov["volume_1h"],
            buys_1h=ov["buys_1h"], sells_1h=ov["sells_1h"], change_1h=ov["change_1h"],
        )
        if chain == "solana":
            extra = await self._solana(token, facts, ov)
        else:
            extra = await self._evm(chain, token, facts, ov)
        smart = await self.smart_buyers(chain, token)
        facts.smart_buyers = len(smart)
        sc, reasons = score(facts)
        rep = {
            "chain": chain, "address": token, "symbol": facts.symbol, "name": facts.name,
            "price": facts.price, "liquidity": facts.liquidity, "mcap": facts.mcap, "holders": facts.holders,
            "volume_1h": facts.volume_1h, "change_1h": facts.change_1h, "age_hours": round(facts.age_hours, 2),
            "buys_1h": facts.buys_1h, "sells_1h": facts.sells_1h,
            "top10_pct": round(facts.top10_pct, 2), "dev_held_pct": facts.dev_held_pct,
            "bundle_held_pct": facts.bundle_held_pct, "insider_held_pct": facts.insider_held_pct,
            "sniper_held_pct": facts.sniper_held_pct, "sell_tax": facts.sell_tax, "buy_tax": facts.buy_tax,
            "risks": facts.risks, "warnings": facts.warnings, "score": sc, "reasons": reasons,
            "smart_buyers": smart, "analyzed_at": time.time(), **extra,
        }
        return rep

    # ================================================================== Solana
    async def _solana(self, mint: str, facts: TokenFacts, ov: dict) -> dict:
        rpc = self.app.sol
        be = self.app.birdeye
        assert rpc is not None
        use_be = bool(be.key)
        sec, mi, gsec, gholders = await asyncio.gather(
            be.token_security("solana", mint) if use_be else _empty_dict(), _safe(rpc.mint_info(mint), {}),
            self.app.data.gm_security("solana", mint), self.app.data.gm_holders("solana", mint))
        if not mi.get("found") and gsec:
            # 节点暂时不可用：用 GMGN 的权限数据兜底
            mi = {"mintAuthority": "未放弃" if _no(gsec.get("renounced_mint")) else None,
                  "freezeAuthority": "未放弃" if _no(gsec.get("renounced_freeze_account")) else None,
                  "supply": int(f(dig(ov, "total_supply", default=0)) * 10 ** int(f(ov.get("decimals") or 6)))}
            facts.warnings.append("Solana 节点查询失败，权限数据来自 GMGN")
        risks, warns = solana_safety(mi, sec)
        facts.risks += risks
        facts.warnings += warns
        supply = mi.get("supply") or 0
        creator = str(dig(sec, "creatorAddress", "creator_address", default="") or ov.get("creator") or "")
        created_ts = f(dig(sec, "creationTime", "creation_time")) or f(ov.get("created_ts"))
        top10 = dig(sec, "top10UserPercent", "top10HolderPercent")
        if top10 is None:
            top10 = dig(gsec, "top_10_holder_rate") if gsec else dig(ov, "stat.top_10_holder_rate")
        facts.top10_pct = _pct(top10) if top10 is not None else 0.0

        # ---- 还原早期买家 ----
        n_early = int(self.cfg.get("discovery.max_early_buyers", 40))
        buys: list[bd.EarlyBuy] = []
        launch_slot, launch_ts = 0, created_ts
        # 逐笔还原开盘要几十次节点请求；Helius 免费版扛不住，默认关闭，改用 GMGN 的捆绑/老鼠仓标签
        if self.cfg.get("discovery.rpc_reconstruct", False):
            sigs = await _safe(rpc.oldest_signatures(mint, n_early * 2, max_pages=2), None)
        else:
            sigs = []
        if sigs:
            sem = asyncio.Semaphore(8)

            async def get(sig: str) -> dict | None:
                async with sem:
                    return await rpc.get_transaction(sig)

            txs = await asyncio.gather(*(get(s["signature"]) for s in sigs if not s.get("err")))
            txs = [t for t in txs if t and not (t.get("meta") or {}).get("err")]
            txs.sort(key=lambda t: (t.get("slot", 0), t.get("blockTime") or 0))
            if txs:
                launch_slot = int(txs[0].get("slot", 0))
                launch_ts = f(txs[0].get("blockTime")) or created_ts
                if not creator:
                    creator = signer(txs[0])
            for tx in txs:
                sig = (tx.get("transaction", {}).get("signatures") or [""])[0]
                owners = {b.get("owner") for b in (tx.get("meta") or {}).get("postTokenBalances") or []
                          if b.get("mint") == mint and b.get("owner")}
                tx_signer = signer(tx)
                tx_buyers = []
                for owner in owners:
                    if not on_curve(owner):
                        continue
                    sol_d, toks = wallet_deltas(tx, owner)
                    d = toks.get(mint, (0, 0))[0]
                    if d <= 0:
                        continue
                    tags = set()
                    if tx_signer == creator and owner != creator and sol_d >= 0:
                        tags.add("bundler(dev转币)")
                    tx_buyers.append(bd.EarlyBuy(owner, int(tx.get("slot", 0)), f(tx.get("blockTime")),
                                                 max(-sol_d, 0.0), d, sig, tags=tags))
                multi = len([b for b in tx_buyers if b.wallet != creator]) >= 2
                for b in tx_buyers:
                    b.multi_in_tx = multi and b.wallet != creator
                buys.extend(tx_buyers)
        elif sigs is None:
            facts.warnings.append("无法完整还原开盘（交易太多或节点不可用），改用 GMGN / Birdeye 标签数据")

        # Birdeye 早期买家标签（insider / bundler / sniper）
        try:
            fb = await be.first_buyers(mint, 100) if use_be else []
        except BudgetExceeded:
            fb = []
        # 自己还原出的买家太少（例如直接在 Raydium 开盘，swap 指令不带 mint 账户），用 Birdeye 数据补充
        need_fallback = len([b for b in buys if b.wallet != creator]) < 3
        known = {b.wallet for b in buys}
        tagmap: dict[str, set[str]] = {}
        for i, x in enumerate(fb):
            w = dig(x, "wallet", "owner", "address", default="")
            tg = dig(x, "tags", "wallet_tags", "labels", default=[]) or []
            if isinstance(tg, str):
                tg = [tg]
            tg = {str(t if not isinstance(t, dict) else t.get("name", t.get("tag", ""))) for t in tg}
            if not w:
                continue
            tagmap[w] = tg
            if need_fallback and w not in known and i < n_early:
                slot = int(f(dig(x, "slot", "block_slot", "first_buy_slot")))
                ts = f(dig(x, "block_unix_time", "first_buy_time", "blockUnixTime", "timestamp"))
                if not slot:   # 没有 slot 就按时间折算（Solana 约 0.4 秒一个 slot）
                    slot = launch_slot + (int(max(ts - launch_ts, 0) / 0.4) if ts and launch_ts else (0 if i < 3 else 99))
                buys.append(bd.EarlyBuy(w, slot, ts, f(dig(x, "amount_in_sol", "sol_amount", "native_amount")),
                                        int(f(dig(x, "amount_raw", "first_buy_amount_raw", "amount"))), tags=tg))
        for b in buys:
            b.tags |= tagmap.get(b.wallet, set())

        # ---- GMGN 持币人标签 + 资金来源 ----
        funding: dict[str, bd.Funding] = {}
        g_hold = merge_gmgn_holders(gholders, buys, funding, supply, launch_slot, launch_ts, creator, ov)

        # ---- 资金溯源 ----
        wallets = list({b.wallet for b in buys} | ({creator} if creator else set()))[: n_early + 30]
        try:
            ff = await be.first_funded([w for w in wallets if w not in funding]) if use_be else {}
        except BudgetExceeded:
            ff = {}
        for w, info in ff.items():
            if info.get("funder"):
                funding[w] = bd.Funding(info["funder"], info.get("ts", 0))
        missing = [w for w in wallets if w not in funding][:15 if self.cfg.get("discovery.rpc_reconstruct", False) else 0]
        sem2 = asyncio.Semaphore(5)

        async def fund(w: str) -> None:
            async with sem2:
                r = await rpc.first_funder(w)
                if r:
                    funding[w] = bd.Funding(r["funder"], r["ts"])

        await asyncio.gather(*(fund(w) for w in missing), return_exceptions=True)

        # ---- 当前持仓 ----
        holdings = await self._sol_holdings(mint, [w for w in wallets if w not in g_hold])
        holdings.update(g_hold)
        res = bd.detect(creator=creator, launch_slot=launch_slot, launch_ts=launch_ts, buys=buys,
                        funding=funding, supply_raw=supply, holdings=holdings,
                        early_window=int(self.cfg.get("discovery.early_slots", 3)),
                        fresh_hours=float(self.cfg.get("discovery.fresh_wallet_hours", 72)),
                        similar_tol=float(self.cfg.get("discovery.similar_amount_cv", 0.15)))
        self._apply(res, facts, gsec)
        facts.age_hours = (time.time() - launch_ts) / 3600 if launch_ts else 0
        return self._extra(res, creator, launch_ts)

    async def _sol_holdings(self, mint: str, wallets: list[str]) -> dict[str, int]:
        rpc = self.app.sol
        out: dict[str, int] = {}
        sem = asyncio.Semaphore(8)

        async def one(w: str) -> None:
            async with sem:
                try:
                    out[w] = (await rpc.token_balance(w, mint))[0]
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(*(one(w) for w in wallets))
        return out

    # ================================================================== EVM
    async def _evm(self, chain: str, token: str, facts: TokenFacts, ov: dict) -> dict:
        rpc = self.app.evm[chain]
        be = self.app.birdeye
        gp, ci, supply, gsec, gholders = await asyncio.gather(
            self.app.goplus.token_security(chain, token),
            be.creation_info(chain, token) if be.key else _empty_dict(),
            _safe(rpc.erc20_total_supply(token), 0), self.app.data.gm_security(chain, token),
            self.app.data.gm_holders(chain, token))
        max_tax = float(self.cfg.get("trading.max_sell_tax_pct", 10))
        risks, warns, btax, stax = evm_safety(gp, max_tax)
        if gsec:   # GMGN 安全数据作为第二意见
            hp = any(str(gsec.get(k, "")).lower() in ("yes", "1", "true") for k in ("is_honeypot", "honeypot"))
            if hp and not any("貔貅" in r for r in risks):
                risks.append("貔貅盘（GMGN 检测）")
            if str(gsec.get("can_not_sell", "")).lower() in ("1", "true", "yes") and not any("卖" in r for r in risks):
                risks.append("无法卖出（GMGN 检测）")
            g_sell = f(gsec.get("sell_tax")) * 100
            if g_sell > stax:
                stax = g_sell
                if g_sell > max_tax and not any("卖出税" in r for r in risks):
                    risks.append(f"卖出税 {g_sell:.1f}% 过高（GMGN）")
            btax = max(btax, f(gsec.get("buy_tax")) * 100)
            if not gp and warns and "GoPlus" in warns[0]:
                warns.pop(0)
        facts.risks += risks
        facts.warnings += warns
        facts.buy_tax, facts.sell_tax = btax, stax
        creator = norm_addr(chain, str(dig(ci, "owner", "creator", "creatorAddress", default="")
                                       or gp.get("creator_address", "") or ov.get("creator") or ""))
        created_block = int(f(dig(ci, "slot", "blockNumber", "block_number")))
        created_ts = f(dig(ci, "blockUnixTime", "block_unix_time")) or f(ov.get("created_ts"))
        if not created_block and created_ts:
            created_block = await self._block_at(chain, created_ts - 60)

        # 前10持仓（排除合约/锁仓地址）
        top = 0.0
        for h in (gp.get("holders") or [])[:20]:
            if str(h.get("is_contract", "0")) == "1" or str(h.get("is_locked", "0")) == "1":
                continue
            top += f(h.get("percent")) * 100
        if not gp and gsec:
            top = _pct(gsec.get("top_10_holder_rate"))
        facts.top10_pct = top

        buys: list[bd.EarlyBuy] = []
        launch_block, launch_ts = created_block, created_ts
        if created_block:
            try:
                buys, launch_block = await self._evm_early(chain, token, created_block, creator)
            except Exception as e:  # noqa: BLE001
                log.warning("%s 开盘还原失败（节点问题）: %s", chain, e)
                facts.warnings.append("节点查询失败，开盘还原不完整")
        else:
            facts.warnings.append("拿不到创建区块，跳过开盘还原")
        if launch_block and launch_block != created_block:
            try:
                b = await rpc.call("eth_getBlockByNumber", [hex(launch_block), False])
                launch_ts = int(b["timestamp"], 16) if b else created_ts
            except Exception:  # noqa: BLE001
                pass

        funding: dict[str, bd.Funding] = {}
        g_hold = merge_gmgn_holders(gholders, buys, funding, supply, launch_block, launch_ts, creator, ov)
        wallets = list({b.wallet for b in buys} | ({creator} if creator else set()))
        es = self.app.etherscan
        if es.enabled:
            sem = asyncio.Semaphore(3)

            async def fund(w: str) -> None:
                async with sem:
                    r = await es.first_funder(chain, w)
                    if r:
                        funding[w] = bd.Funding(r["funder"], r["ts"])

            await asyncio.gather(*(fund(w) for w in [x for x in wallets if x not in funding][:26]),
                                 return_exceptions=True)

        holdings: dict[str, int] = {}
        sem3 = asyncio.Semaphore(10)

        async def bal(w: str) -> None:
            async with sem3:
                try:
                    holdings[w] = await rpc.erc20_balance(token, w)
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(*(bal(w) for w in wallets if w not in g_hold))
        holdings.update(g_hold)
        res = bd.detect(creator=creator, launch_slot=launch_block, launch_ts=launch_ts, buys=buys,
                        funding=funding, supply_raw=supply, holdings=holdings,
                        early_window=int(self.cfg.get("discovery.early_blocks", 3)),
                        fresh_hours=float(self.cfg.get("discovery.fresh_wallet_hours", 72)),
                        similar_tol=float(self.cfg.get("discovery.similar_amount_cv", 0.15)))
        self._apply(res, facts, gsec)
        facts.age_hours = (time.time() - launch_ts) / 3600 if launch_ts else 0
        return self._extra(res, creator, launch_ts)

    async def _block_at(self, chain: str, ts: float) -> int:
        """按时间找区块号（二分查找），GMGN 只给创建时间不给区块号时用。"""
        rpc = self.app.evm[chain]
        try:
            hi = await rpc.block_number()
            b = await rpc.call("eth_getBlockByNumber", [hex(hi), False])
            t_hi = int(b["timestamp"], 16)
            if ts >= t_hi:
                return hi
            bt = {"ethereum": 12.0, "bsc": 0.75, "base": 2.0}.get(chain, 2.0)
            lo = max(0, hi - int((t_hi - ts) / bt * 1.5) - 100)
            for _ in range(30):
                if hi - lo <= 1:
                    break
                mid = (lo + hi) // 2
                b = await rpc.call("eth_getBlockByNumber", [hex(mid), False])
                if int(b["timestamp"], 16) < ts:
                    lo = mid
                else:
                    hi = mid
            return lo
        except Exception as e:  # noqa: BLE001
            log.debug("按时间找区块失败: %s", e)
            return 0

    async def _evm_early(self, chain: str, token: str, start: int, creator: str) -> tuple[list[bd.EarlyBuy], int]:
        """扫描代币 Transfer 日志，找出"从合约（池子/曲线）转给个人钱包"的记录 = 买入。"""
        rpc = self.app.evm[chain]
        n_early = int(self.cfg.get("discovery.max_early_buyers", 40))
        head = await rpc.block_number()
        contract_cache: dict[str, bool] = {}

        async def is_c(a: str) -> bool:
            if a not in contract_cache:
                contract_cache[a] = await rpc.is_contract(a)
            return contract_cache[a]

        transfers: list[dict] = []
        blk, step, scanned = start, 500, 0
        while blk <= head and scanned < 5000 and len(transfers) < 400:
            to_blk = min(blk + step - 1, head)
            try:
                lg = await rpc.logs({"address": token, "topics": [TRANSFER_TOPIC],
                                     "fromBlock": hex(blk), "toBlock": hex(to_blk)})
            except Exception as e:  # noqa: BLE001 - 范围太大时缩小重试
                if step > 50:
                    step //= 2
                    continue
                log.debug("getLogs 失败 %s: %s", token, e)
                break
            transfers += lg
            scanned += to_blk - blk + 1
            blk = to_blk + 1
            if len(transfers) >= 150:
                break

        buys: list[bd.EarlyBuy] = []
        launch_block = 0
        by_tx: dict[str, list[bd.EarlyBuy]] = {}
        seen: set[str] = set()
        zero = "0x" + "0" * 40
        for lg in transfers:
            if len(lg.get("topics", [])) < 3:
                continue
            frm, to = topic_to_addr(lg["topics"][1]), topic_to_addr(lg["topics"][2])
            if to in (zero, token) or frm == zero:
                continue
            if await is_c(to):
                continue
            from_contract = await is_c(frm)
            tags = set()
            if not from_contract:
                if frm == creator:
                    tags.add("bundler(dev转币)")
                else:
                    continue   # 个人之间转账，不算买入
            amount = int(lg.get("data", "0x0"), 16) if lg.get("data") not in (None, "0x") else 0
            b = int(lg["blockNumber"], 16)
            if from_contract and not launch_block:
                launch_block = b
            eb = bd.EarlyBuy(to, b, 0.0, 0.0, amount, lg.get("transactionHash", ""), tags=tags)
            by_tx.setdefault(eb.tx, []).append(eb)
            buys.append(eb)
            seen.add(to)
            if len(seen) >= n_early:
                break
        # 同一笔交易里多个钱包收到币 = 合约批量买入（典型捆绑手法）
        for tx_hash, lst in by_tx.items():
            if len({x.wallet for x in lst}) >= 2:
                for x in lst:
                    x.multi_in_tx = True
        # 读取交易的原生币花费
        sem = asyncio.Semaphore(8)

        async def fill(tx_hash: str, lst: list[bd.EarlyBuy]) -> None:
            async with sem:
                try:
                    t = await rpc.tx(tx_hash)
                except Exception:  # noqa: BLE001
                    return
                if not t:
                    return
                val = int(t.get("value", "0x0"), 16) / 1e18
                for x in lst:
                    x.native = val / max(len(lst), 1)

        await asyncio.gather(*(fill(h, l) for h, l in list(by_tx.items())[:60]))
        return buys, launch_block or start

    # ================================================================== 汇总
    @staticmethod
    def _apply(res: bd.BundleResult, facts: TokenFacts, gsec: dict | None = None) -> None:
        facts.bundle_held_pct = res.bundle_held_pct
        facts.insider_held_pct = res.insider_held_pct
        facts.sniper_held_pct = res.sniper_held_pct
        facts.dev_held_pct = res.dev_held_pct
        if gsec:   # GMGN 自己算的"疑似老鼠仓持仓"，取两者较大值（宁可保守）
            facts.insider_held_pct = max(facts.insider_held_pct, round(_pct(gsec.get("suspected_insider_hold_rate")), 2))
            facts.dev_held_pct = max(facts.dev_held_pct, round(_pct(gsec.get("creator_balance_rate")), 2))

    @staticmethod
    def _extra(res: bd.BundleResult, creator: str, launch_ts: float) -> dict:
        def pack(cls: str) -> list[dict]:
            return [{"wallet": v.wallet, "evidence": v.evidence, "held": v.held_raw}
                    for v in res.verdicts.values() if v.cls == cls]

        return {
            "creator": creator, "launch_ts": launch_ts,
            "bundle_bought_pct": res.bundle_bought_pct, "insider_bought_pct": res.insider_bought_pct,
            "bundle_wallets": pack("bundle"), "insider_wallets": pack("insider"),
            "sniper_wallets": pack("sniper"), "clusters": res.clusters[:5],
        }

    async def _save(self, rep: dict, source: str) -> None:
        db = self.app.db
        t = time.time()
        await db.execute(
            """INSERT INTO tokens(chain,address,symbol,name,creator,created_at,source,score,bundle_pct,insider_pct,
                 safety_ok,report,analyzed_at,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chain,address) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,
                 score=excluded.score,bundle_pct=excluded.bundle_pct,insider_pct=excluded.insider_pct,
                 safety_ok=excluded.safety_ok,report=excluded.report,analyzed_at=excluded.analyzed_at""",
            (rep["chain"], rep["address"], rep["symbol"], rep["name"], rep.get("creator", ""),
             rep.get("launch_ts", 0), source, rep["score"], rep["bundle_held_pct"], rep["insider_held_pct"],
             0 if rep["risks"] else 1, json.dumps(rep, ensure_ascii=False, default=str), t, t))
        rows = []
        for cls in ("bundle", "insider", "sniper"):
            for w in rep.get(f"{cls}_wallets", []):
                rows.append((rep["chain"], w["wallet"], cls, rep["address"], "；".join(w["evidence"]), t))
        if rep.get("creator"):
            rows.append((rep["chain"], rep["creator"], "dev", rep["address"], "", t))
        await db.executemany(
            "INSERT OR IGNORE INTO address_tags(chain,address,tag,token,detail,created_at) VALUES(?,?,?,?,?,?)", rows)
        # 结果档案：同一个币半小时内只记一次
        if float(rep.get("price") or 0) > 0 and not await db.fetchone(
                "SELECT 1 FROM token_outcomes WHERE chain=? AND token=? AND ts>?", (rep["chain"], rep["address"], t - 1800)):
            feats = {k: rep.get(k) for k in ("score", "liquidity", "mcap", "holders", "volume_1h", "change_1h",
                                             "age_hours", "buys_1h", "sells_1h", "top10_pct", "bundle_held_pct",
                                             "insider_held_pct", "dev_held_pct")}
            feats["smart"] = len(rep.get("smart_buyers") or [])
            feats["risks"] = len(rep.get("risks") or [])
            await db.execute(
                "INSERT INTO token_outcomes(ts,chain,token,symbol,source,price,features) VALUES(?,?,?,?,?,?,?)",
                (t, rep["chain"], rep["address"], rep.get("symbol", ""), source, float(rep["price"]),
                 json.dumps(feats, default=str)))
