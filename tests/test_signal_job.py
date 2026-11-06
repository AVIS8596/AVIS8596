import glob
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from integrations.quantdinger import mock_runtime
from quantbot.data import generate_synthetic, load_csv
from quantbot.notify import load_env_file
from quantbot.signal_job import PaperState, SignalEngine, load_state, replay, run_signal_job
from quantbot.strategies import TrendVolTarget

ROOT = Path(__file__).resolve().parents[1]
QD_SRC = (ROOT / "integrations/quantdinger/trend_vol_target_v2.py").read_text()


def _datasets():
    sets = []
    for seed in (42, 7, 123):
        sets.append((f"synthetic{seed}", generate_synthetic(n=1500, seed=seed)))
    cached = sorted(glob.glob(str(ROOT / "data/binance_BTCUSDT_1d_*.csv")))
    if cached:
        sets.append(("real BTCUSDT", load_csv(cached[-1])))
    return sets


@pytest.mark.parametrize("name,df", _datasets())
def test_quantbot_engine_matches_quantdinger_fill_for_fill(name, df):
    """quantbot 每日信号引擎与 QuantDinger 版策略逐笔一致 (时间、方向、数量、价格)。"""
    targets = TrendVolTarget().generate_signals(df)
    st = replay(df, targets, SignalEngine(commission=0.001, slippage=0.0005, threshold=0.1), capital=10_000)
    qd = mock_runtime.run(QD_SRC, df, initial_cash=10_000, commission=0.001, slippage=0.0005)
    ours = [(f["bar"], round(f["qty"], 8), round(f["price"], 6)) for f in st.fills]
    theirs = [(str(f["time"].date()), round(f["qty"], 8), round(f["price"], 6)) for f in qd["fills"]]
    assert ours == theirs, name
    final = st.cash + st.qty * float(df["close"].iloc[-1])
    assert final == pytest.approx(qd["equity"].iloc[-1], rel=1e-9)


def _fake_fetcher(df):
    def fetch(symbol, interval="1d", start=None):
        return df
    return fetch


def test_job_first_run_resume_and_idempotent(tmp_path):
    full = generate_synthetic(n=400, seed=3)
    state = str(tmp_path / "state.json")
    sent = []

    def sender(token, chat, text):
        sent.append(text)

    # 第一次: 只处理最后一根
    txt = run_signal_job(state_path=state, env_path=None, fetcher=_fake_fetcher(full.iloc[:300]), sender=sender)
    assert txt and len(sent) == 1
    st = load_state(state)
    assert st.last_bar == str(full.index[299].date())
    # 没有新日线: 不推送
    assert run_signal_job(state_path=state, env_path=None, fetcher=_fake_fetcher(full.iloc[:300]), sender=sender) is None
    assert len(sent) == 1
    # 漏跑 3 天后: 按顺序补算, 推送一条
    txt = run_signal_job(state_path=state, env_path=None, fetcher=_fake_fetcher(full.iloc[:303]), sender=sender)
    assert len(sent) == 2 and "补算" in txt
    assert load_state(state).last_bar == str(full.index[302].date())


def test_job_does_not_save_state_when_push_fails(tmp_path):
    df = generate_synthetic(n=300, seed=5)
    state = str(tmp_path / "state.json")

    def failing(token, chat, text):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        run_signal_job(state_path=state, env_path=None, fetcher=_fake_fetcher(df), sender=failing)
    assert not os.path.exists(state)


def test_job_message_mentions_action_and_paper_account(tmp_path):
    df = generate_synthetic(n=400, seed=11)
    # 找一根会触发入场的日线作为 "最新收盘"
    targets = TrendVolTarget().generate_signals(df)
    idx = next(i for i in range(200, 400) if targets.iloc[i] > 0.1)
    txt = run_signal_job(state_path=str(tmp_path / "s.json"), env_path=None,
                         fetcher=_fake_fetcher(df.iloc[: idx + 1]), sender=lambda *a: None)
    assert "买入至" in txt and "模拟账户" in txt and "不会下单" in txt


def test_load_env_file(tmp_path):
    p = tmp_path / "t.env"
    p.write_text("# comment\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID='42'\n")
    env = load_env_file(str(p))
    assert env["TELEGRAM_BOT_TOKEN"] == "123:abc" and env["TELEGRAM_CHAT_ID"] == "42"
