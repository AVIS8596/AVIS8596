"""阴谋团伙识别：总是一起出现在同一批币最早买家里的钱包（被分析为捆绑 / 老鼠仓 / 开盘狙击），就是一个团伙。

做法（每 2 小时一次，最近 3 天机器人分析过的币）：
1. 每个币的"早期可疑买家"= 被认定为捆绑 / 老鼠仓 / 狙击的地址（每个币最多取 30 个）
2. 只保留在 3 个以上的币里都出现过的地址（反复出现的早期买家）
3. 两两统计一起出现的次数，一起出现在 3 个以上的币里就连一条线；连成一片、至少 3 个人的算一个团伙
新币的早期买家里有同一个团伙的 3 个以上成员 → 团伙盘，不买（被拦下的照样走影子单，事后看拦得对不对）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("memebot.gangs")

MIN_TOKENS = 3        # 一起出现在几个币里才算有关系
MIN_SIZE = 3          # 团伙至少几个人
PER_TOKEN = 30


def find_gangs(token_sets: dict[str, list[str]]) -> dict[str, tuple[int, int, int]]:
    """token → 早期可疑买家列表。返回 地址 → (团伙编号, 团伙人数, 团伙一起出现过的币数)。"""
    freq = Counter(a for addrs in token_sets.values() for a in set(addrs[:PER_TOKEN]))
    keep = {a for a, n in freq.items() if n >= MIN_TOKENS}
    pairs: Counter = Counter()
    toks_of: dict[str, set[str]] = defaultdict(set)
    for tok, addrs in token_sets.items():
        mem = sorted({a for a in addrs[:PER_TOKEN] if a in keep})
        for a in mem:
            toks_of[a].add(tok)
        for a, b in combinations(mem, 2):
            pairs[(a, b)] += 1
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), n in pairs.items():
        if n >= MIN_TOKENS:
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[ra] = rb
    groups: dict[str, list[str]] = defaultdict(list)
    for a in parent:
        groups[root(a)].append(a)
    out: dict[str, tuple[int, int, int]] = {}
    gid = 0
    for members in sorted(groups.values(), key=len, reverse=True):
        if len(members) < MIN_SIZE:
            continue
        gid += 1
        n_tok = len(set().union(*(toks_of[m] for m in members)))
        for m in members:
            out[m] = (gid, len(members), n_tok)
    return out


async def rebuild(app: "App", days: float = 3) -> int:
    db = app.db
    rows = await db.fetchall("SELECT chain, token, address FROM address_tags WHERE tag IN ('bundle','insider','sniper') "
                             "AND created_at>? ORDER BY created_at", (time.time() - days * 86400,))
    by_chain: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_chain[r["chain"]][r["token"]].append(r["address"])
    total = 0
    for chain, sets in by_chain.items():
        gangs = await asyncio.to_thread(find_gangs, sets)
        await db.execute("DELETE FROM gangs WHERE chain=?", (chain,))
        await db.executemany("INSERT INTO gangs(chain,address,gang_id,size,tokens,updated_at) VALUES(?,?,?,?,?,?)",
                             [(chain, a, g, n, t, time.time()) for a, (g, n, t) in gangs.items()])
        total += len({g for g, _, _ in gangs.values()})
    return total


async def hits(app: "App", chain: str, rep: dict) -> tuple[int, int, int]:
    """这个币的早期可疑买家里，同一个团伙最多有几个人。返回 (人数, 团伙编号, 团伙一起出现过的币数)。"""
    addrs = [w.get("wallet") if isinstance(w, dict) else w
             for cls in ("bundle", "insider", "sniper") for w in rep.get(f"{cls}_wallets") or []]
    addrs = [a for a in addrs if a]
    if not addrs:
        return 0, 0, 0
    marks = ",".join("?" * len(addrs))
    rows = await app.db.fetchall(f"SELECT gang_id, tokens, COUNT(*) n FROM gangs WHERE chain=? AND address IN ({marks}) "
                                 f"GROUP BY gang_id ORDER BY n DESC LIMIT 1", (chain, *addrs))
    if not rows:
        return 0, 0, 0
    return int(rows[0]["n"]), int(rows[0]["gang_id"]), int(rows[0]["tokens"])


class GangWatch:
    def __init__(self, app: "App"):
        self.app = app

    async def run(self) -> None:
        await asyncio.sleep(300)
        while True:
            try:
                n = await rebuild(self.app)
                log.info("阴谋团伙识别：%d 个团伙", n)
            except Exception as e:  # noqa: BLE001
                log.exception("阴谋团伙识别出错: %s", e)
            await asyncio.sleep(2 * 3600)
