"""看"钓鱼钱包%""市值 / 池子"这两条新拦截会拦掉多少币、被拦的币后来怎样（只读数据库，机器人开着也能跑）。

用法（在机器人目录里）：  python3 deploy/fake_check.py
"""
import json
import sqlite3
import sys
import time

db = sqlite3.connect(f"file:{sys.argv[1] if len(sys.argv) > 1 else 'data/memebot.db'}?mode=ro", uri=True)
rows = db.execute("SELECT features, price, p60, p240, p1440 FROM token_outcomes WHERE ts>? AND chain='solana' AND price>0",
                  (time.time() - 4 * 86400,)).fetchall()


def fv(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def show(name, buckets):
    print(f"\n{name}")
    for label, rs in buckets:
        n = len(rs)
        if not n:
            print(f"  {label}: 0 个")
            continue
        dead = sum(1 for r in rs if r[2])
        p60 = sorted(r[0] for r in rs if r[0] is not None)
        p240 = sorted(r[1] for r in rs if r[1] is not None)
        med = lambda s: f"{s[len(s) // 2] * 100:+.0f}%" if s else "—"
        print(f"  {label}: {n} 个（{n / len(rows) * 100:.0f}%）| 1 小时后中位 {med(p60)} | 4 小时后中位 {med(p240)} | 后来取不到价格（多半归零）{dead / n * 100:.0f}%")


ent, ml, t10 = {}, {}, {}
def ret(px, p):
    if p is None:
        return None
    return -1.0 if p < 0 else (p / px - 1 if p > 0 else None)


for feats, px, p60, p240, p1440 in rows:
    try:
        x = json.loads(feats or "{}") or {}
    except ValueError:
        continue
    gone = any(p is not None and p < 0 for p in (p60, p240, p1440))   # 之后取不到价格 = 多半归零 / 池子被撤
    out = (ret(px, p60), ret(px, p240), gone)
    e = fv(x.get("entrap_pct"))
    ent.setdefault("0~10%" if e < 10 else "10~30%" if e < 30 else "30~50%" if e < 50 else "50~80%" if e < 80 else "≥80%", []).append(out)
    tp = fv(x.get("top10_pct"))
    t10.setdefault("0~20%" if tp < 20 else "20~30%" if tp < 30 else "30~45%" if tp < 45 else "≥45%", []).append(out)
    liq, mc = fv(x.get("liquidity")), fv(x.get("mcap"))
    if liq > 0:
        r = mc / liq
        ml.setdefault("＜10 倍" if r < 10 else "10~30 倍" if r < 30 else "≥30 倍", []).append(out)

print(f"最近 4 天 Solana 分析过的币：{len(rows)} 个")
show("钓鱼钱包%（≥10% 会被拦）", [(k, ent.get(k, [])) for k in ("0~10%", "10~30%", "30~50%", "50~80%", "≥80%")])
show("市值 / 池子（≥30 倍会被拦）", [(k, ml.get(k, [])) for k in ("＜10 倍", "10~30 倍", "≥30 倍")])
show("前10持仓（对照仓 / 买卖力量打法 >20% 会被拦，其他来源 >45%）", [(k, t10.get(k, [])) for k in ("0~20%", "20~30%", "30~45%", "≥45%")])
