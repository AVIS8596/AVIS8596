"""SQLite 存储。单连接 + WAL 模式，避免多任务并发写导致的 database is locked。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS wallets (
  chain TEXT NOT NULL, address TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',      -- active / inactive / blacklist / manual
  source TEXT, label TEXT,
  realized_usd REAL DEFAULT 0, win_rate REAL DEFAULT 0, trades INTEGER DEFAULT 0,
  score REAL DEFAULT 0,
  follow_wins INTEGER DEFAULT 0, follow_losses INTEGER DEFAULT 0,
  evaluated_at REAL DEFAULT 0, created_at REAL,
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_wallets_status ON wallets(chain, status, score);

CREATE TABLE IF NOT EXISTS candidates (       -- 已评估过的候选地址（避免重复花接口费）
  chain TEXT NOT NULL, address TEXT NOT NULL, checked_at REAL, passed INTEGER,
  PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS address_tags (
  chain TEXT NOT NULL, address TEXT NOT NULL, tag TEXT NOT NULL,  -- bundle / insider / sniper / dev
  token TEXT NOT NULL, detail TEXT, created_at REAL,
  PRIMARY KEY (chain, address, tag, token)
);
CREATE INDEX IF NOT EXISTS idx_tags_addr ON address_tags(chain, address);

CREATE TABLE IF NOT EXISTS tokens (
  chain TEXT NOT NULL, address TEXT NOT NULL,
  symbol TEXT, name TEXT, creator TEXT, created_at REAL, source TEXT,
  score REAL, bundle_pct REAL, insider_pct REAL, safety_ok INTEGER,
  report TEXT, analyzed_at REAL, first_seen REAL,
  PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, chain TEXT, wallet TEXT, token TEXT, side TEXT,
  native_amount REAL, token_amount REAL, tx TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_signals_token ON signals(chain, token, ts);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT, token TEXT, symbol TEXT, status TEXT,   -- open / closed / stuck
  dry_run INTEGER, reason TEXT, source_wallets TEXT,
  entry_price REAL, cost_native REAL, cost_usd REAL,
  amount_raw TEXT, decimals INTEGER, remaining_raw TEXT,
  high_price REAL, tp_stage INTEGER DEFAULT 0, sell_attempts INTEGER DEFAULT 0,
  realized_usd REAL DEFAULT 0, opened_at REAL, closed_at REAL, close_reason TEXT,
  tier TEXT,           -- best / good / normal；空 = 分档上线前的老持仓
  entry_snap TEXT, prev_snap TEXT, health INTEGER, health_note TEXT, health_streak INTEGER DEFAULT 0,
  health_emergency TEXT, health_reduced INTEGER DEFAULT 0, health_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, chain TEXT, token TEXT, side TEXT, position_id INTEGER,
  native_amount REAL, token_raw TEXT, price_usd REAL, usd_value REAL,
  tx TEXT, dry_run INTEGER, ok INTEGER, error TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

-- 观察模式：同一个持仓分别按旧规则(v1)和新规则(v2)虚拟卖出，用来对比谁卖得好
CREATE TABLE IF NOT EXISTS shadow (
  pid INTEGER, variant TEXT, frac REAL, tp_stage INTEGER DEFAULT 0, realized REAL DEFAULT 0,
  high REAL, reduced INTEGER DEFAULT 0, closed INTEGER DEFAULT 0, closed_at REAL, last_price REAL,
  log TEXT DEFAULT '[]', created_at REAL, PRIMARY KEY (pid, variant)
);
"""

# 老数据库需要补的列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
POSITION_COLUMNS = [
    ("tier", "TEXT"), ("entry_snap", "TEXT"), ("prev_snap", "TEXT"), ("health", "INTEGER"),
    ("health_note", "TEXT"), ("health_streak", "INTEGER DEFAULT 0"), ("health_emergency", "TEXT"),
    ("health_reduced", "INTEGER DEFAULT 0"), ("health_at", "REAL DEFAULT 0"),
]


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        cur = await self.conn.execute("PRAGMA table_info(positions)")
        have = {r[1] for r in await cur.fetchall()}
        for col, typ in POSITION_COLUMNS:
            if col not in have:
                await self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
        if "tier" not in have:
            # 分档买入上线后、本列加上之前买的持仓：从买入原因里认出档位
            await self.conn.execute(
                "UPDATE positions SET tier = CASE WHEN reason LIKE '%| 🔥优质%' THEN 'best' "
                "WHEN reason LIKE '%| 👍较好%' THEN 'good' WHEN reason LIKE '%| 一般' THEN 'normal' END "
                "WHERE tier IS NULL")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def execute(self, sql: str, params: tuple | list = ()) -> int:
        assert self.conn
        async with self.lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid or 0

    async def execute_rowcount(self, sql: str, params: tuple | list = ()) -> int:
        """返回受影响行数。INSERT OR IGNORE 被忽略时返回 0（lastrowid 在这种情况下不可靠）。"""
        assert self.conn
        async with self.lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.rowcount

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        assert self.conn
        if not rows:
            return
        async with self.lock:
            await self.conn.executemany(sql, rows)
            await self.conn.commit()

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        assert self.conn
        async with self.lock:
            cur = await self.conn.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    # ---------------- kv ----------------
    async def kv_get(self, k: str, default: Any = None) -> Any:
        r = await self.fetchone("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    async def kv_set(self, k: str, v: Any) -> None:
        await self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                           (k, json.dumps(v)))

    # ---------------- wallets ----------------
    async def active_wallets(self, chain: str, limit: int) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM wallets WHERE chain=? AND status IN ('active','manual') "
            "ORDER BY (status='manual') DESC, score DESC LIMIT ?", (chain, limit))

    async def wallet(self, chain: str, address: str) -> dict | None:
        return await self.fetchone("SELECT * FROM wallets WHERE chain=? AND address=?", (chain, address))

    async def upsert_wallet(self, chain: str, address: str, *, status: str, source: str,
                            realized_usd: float, win_rate: float, trades: int, score: float) -> None:
        t = time.time()
        await self.execute(
            """INSERT INTO wallets(chain,address,status,source,realized_usd,win_rate,trades,score,evaluated_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chain,address) DO UPDATE SET
                 status=CASE WHEN wallets.status IN ('manual','blacklist') THEN wallets.status ELSE excluded.status END,
                 realized_usd=excluded.realized_usd, win_rate=excluded.win_rate, trades=excluded.trades,
                 score=excluded.score, evaluated_at=excluded.evaluated_at""",
            (chain, address, status, source, realized_usd, win_rate, trades, score, t, t))

    async def tags_for(self, chain: str, address: str) -> list[dict]:
        return await self.fetchall("SELECT tag, token FROM address_tags WHERE chain=? AND address=?",
                                   (chain, address))

    # ---------------- positions ----------------
    async def open_positions(self) -> list[dict]:
        return await self.fetchall("SELECT * FROM positions WHERE status='open'")

    async def has_open_position(self, chain: str, token: str) -> bool:
        return bool(await self.fetchone(
            "SELECT id FROM positions WHERE chain=? AND token=? AND status='open'", (chain, token)))

    async def last_closed_at(self, chain: str, token: str) -> float:
        r = await self.fetchone("SELECT MAX(closed_at) m FROM positions WHERE chain=? AND token=?", (chain, token))
        return float(r["m"]) if r and r["m"] else 0.0

    async def realized_today_usd(self, day_start: float) -> float:
        r = await self.fetchone(
            "SELECT COALESCE(SUM(realized_usd - cost_usd),0) s FROM positions "
            "WHERE status!='open' AND closed_at>=?", (day_start,))
        return float(r["s"]) if r else 0.0
