"""无人值守的安全：数据异地备份、恢复、心跳、实盘利润提醒、真实成本校准的定时任务。

- 每天备份数据库发到你的 Telegram（压缩后的精简版：去掉 K 线缓存和 3 天前的信号流水；结果档案、交易、钱包、
  模型、学到的参数都在）。服务器坏了，在新服务器部署好后，把备份文件发给机器人、配上说明文字 /restore 就能恢复。
- 心跳：在 .env 里填 HEALTHCHECK_URL（healthchecks.io 免费），机器人每 5 分钟报一次平安，
  超过时间没报到 healthchecks 会通过 Telegram / 邮件提醒你——服务器宕机、断网、程序卡死都能发现。
- 实盘利润提醒：GMGN 钱包余额超过设定值，每天提醒一次把多出来的转走（钱包里只留交易需要的）。
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("memebot.safety")

TG_LIMIT = 48 * 1024 * 1024     # Telegram 机器人发文件上限 50MB


def db_path(app: "App") -> Path:
    return Path(app.db.path)


async def send_document(app: "App", path: Path, caption: str) -> bool:
    tg = app.tg
    url = f"{tg.base}/sendDocument"
    form = aiohttp.FormData()
    form.add_field("chat_id", str(tg.chat))
    form.add_field("caption", caption[:1000])
    form.add_field("document", path.read_bytes(), filename=path.name, content_type="application/gzip")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            async with s.post(url, data=form) as r:
                ok = r.status == 200
                if not ok:
                    log.warning("发送备份失败：%s %s", r.status, (await r.text())[:200])
                return ok
    except Exception as e:  # noqa: BLE001
        log.warning("发送备份失败：%s", e)
        return False


def make_slim(src: Path, out_gz: Path) -> int:
    """在线程里跑：复制一份精简数据库并压缩，返回压缩后大小。"""
    tmp = out_gz.with_suffix("")          # .db
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(src))
    try:
        con.execute(f"VACUUM INTO '{tmp}'")
    finally:
        con.close()
    con = sqlite3.connect(str(tmp))
    try:
        cut = time.time() - 3 * 86400
        for sql in ("DELETE FROM kline_cache", f"DELETE FROM signals WHERE ts<{cut}",
                    f"DELETE FROM skipped WHERE ts<{cut}", f"DELETE FROM strat_log WHERE ts<{cut}"):
            try:
                con.execute(sql)
            except sqlite3.Error:
                pass
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    with open(tmp, "rb") as fi, gzip.open(out_gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    tmp.unlink()
    if out_gz.stat().st_size > TG_LIMIT:      # 还太大：早期快照只留 2 天
        with gzip.open(out_gz, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        con = sqlite3.connect(str(tmp))
        try:
            con.execute(f"DELETE FROM early_obs WHERE ts<{time.time() - 2 * 86400}")
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()
        with open(tmp, "rb") as fi, gzip.open(out_gz, "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo)
        tmp.unlink()
    return out_gz.stat().st_size


async def backup_to_tg(app: "App") -> str:
    if not bool(app.cfg.get("safety.tg_backup", True)):
        return ""
    bdir = db_path(app).parent / "backup"
    bdir.mkdir(parents=True, exist_ok=True)
    out = bdir / f"memebot-{time.strftime('%Y%m%d')}-slim.db.gz"
    size = await asyncio.to_thread(make_slim, db_path(app), out)
    if size > TG_LIMIT:
        return f"备份压缩后 {size / 1e6:.0f}MB，超过 Telegram 50MB 上限，只保存在服务器 {out}"
    ok = await send_document(app, out, f"🗄 每日数据备份（{size / 1e6:.1f}MB）：交易、结果档案、钱包、模型、学到的参数都在。"
                                       f"服务器坏了：新服务器部署好后，把这个文件发给机器人，说明文字写 /restore")
    for old in sorted(bdir.glob("memebot-*-slim.db.gz"))[:-2]:
        old.unlink()
    return "" if ok else "备份发送失败（已保存在服务器 data/backup/）"


# ---------------- 恢复
async def restore_receive(app: "App", msg: dict) -> str:
    doc = msg.get("document") or {}
    name = str(doc.get("file_name") or "")
    if not name.endswith((".db.gz", ".db")):
        return "请发机器人每天发给你的备份文件（memebot-日期-slim.db.gz）"
    if int(doc.get("file_size") or 0) > 20 * 1024 * 1024:
        return ("文件超过 20MB，Telegram 不允许机器人下载这么大的文件。"
                "请在 Termius 里把文件放到服务器的 ~/memebot/data/restore.db.gz，再发 /restore confirm")
    tg = app.tg
    r = await app.http.get("tg", f"{tg.base}/getFile", params={"file_id": doc["file_id"]})
    fp = ((r or {}).get("result") or {}).get("file_path")
    if not fp:
        return "下载备份失败，稍后再试"
    dst = db_path(app).parent / ("restore.db.gz" if name.endswith(".gz") else "restore.db")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        async with s.get(f"https://api.telegram.org/file/bot{tg.token}/{fp}") as resp:
            dst.write_bytes(await resp.read())
    info = await asyncio.to_thread(check_backup, dst)
    if info.startswith("❌"):
        dst.unlink(missing_ok=True)
        return info
    return info + "\n确认要用它替换当前数据库就发 /restore confirm（当前数据库会先另存一份，机器人会自动重启）"


def check_backup(p: Path) -> str:
    raw = p.with_suffix("") if p.suffix == ".gz" else p
    try:
        if p.suffix == ".gz":
            with gzip.open(p, "rb") as fi, open(raw, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        con = sqlite3.connect(str(raw))
        try:
            n_pos = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            n_out = con.execute("SELECT COUNT(*) FROM token_outcomes").fetchone()[0]
            n_w = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
            last = con.execute("SELECT MAX(opened_at) FROM positions").fetchone()[0]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return f"❌ 这不是有效的备份：{e}"
    when = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "无"
    return f"✅ 备份可用：持仓记录 {n_pos} 条（最后一笔 {when}）、结果档案 {n_out} 条、钱包 {n_w} 个"


async def restore_confirm(app: "App") -> str:
    d = db_path(app).parent
    raw = d / "restore.db"
    gz = d / "restore.db.gz"
    if gz.exists() and not raw.exists():
        info = await asyncio.to_thread(check_backup, gz)
        if info.startswith("❌"):
            return info
    if not raw.exists():
        return "没有待恢复的备份：先把备份文件发给机器人（说明文字写 /restore）"
    keep = d / "backup" / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
    keep.parent.mkdir(parents=True, exist_ok=True)
    await app.db.execute(f"VACUUM INTO '{keep}'")
    await app.tg.send(f"♻️ 正在恢复：当前数据库已另存为 {keep.name}，机器人 10 秒后自动重启")
    await asyncio.sleep(3)
    await app.db.close()
    shutil.move(str(raw), str(db_path(app)))
    for ext in ("-wal", "-shm"):
        Path(str(db_path(app)) + ext).unlink(missing_ok=True)
    gz.unlink(missing_ok=True)
    os._exit(0)          # systemd 会在 10 秒后重新启动
    return ""


# ---------------- 定时任务
class Safety:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.hc = os.environ.get("HEALTHCHECK_URL", "").strip()
        self.last = {"cost": 0.0, "withdraw": 0.0}

    async def ping(self, fail: bool = False) -> None:
        if not self.hc:
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                await s.get(self.hc + ("/fail" if fail else ""))
        except Exception as e:  # noqa: BLE001
            log.debug("心跳失败: %s", e)

    async def withdraw_check(self) -> None:
        if self.cfg.dry_run:
            return
        for chain, ex in (self.app.trader.exec or {}).items():
            limit = float(self.cfg.per_chain("safety.withdraw_above_native", chain, 0) or 0)
            if limit <= 0:
                continue
            try:
                bal = float(await ex.native_balance())
            except Exception:  # noqa: BLE001
                continue
            if bal > limit:
                await self.app.tg.send(f"💰 {chain} 交易钱包余额 {bal:.4f}，超过设定的 {limit:g}。"
                                       f"建议把多出来的 {bal - limit:.4f} 转到你自己的钱包（GMGN 钱包里只留交易需要的）")

    async def run(self) -> None:
        from .trade.costs import MIN_N, calibrate
        db = self.app.db
        self.app.live_costs = await db.kv_get("live_costs", None)
        # 实盘期间的起点（和模拟盘对比用）：切回模拟盘就清掉，下次上实盘重新算
        if self.cfg.dry_run:
            await db.kv_set("live_since", 0)
        elif not await db.kv_get("live_since", 0):
            await db.kv_set("live_since", time.time())
        while True:
            try:
                await self.ping()
                now = time.time()
                if now - self.last["cost"] > 6 * 3600:
                    self.last["cost"] = now
                    self.app.live_costs = c = await calibrate(self.app)
                    a = (c or {}).get("all") or {}
                    if not await db.kv_get("cost_applied_at", 0) and \
                            max(a.get("buy", {}).get("n", 0), a.get("sell", {}).get("n", 0)) >= MIN_N:
                        await db.kv_set("cost_applied_at", now)      # 从这一刻起模拟盘按实测成本成交
                if now - self.last["withdraw"] > 24 * 3600:
                    self.last["withdraw"] = now
                    await self.withdraw_check()
            except Exception as e:  # noqa: BLE001
                log.exception("安全任务出错: %s", e)
            await asyncio.sleep(300)
