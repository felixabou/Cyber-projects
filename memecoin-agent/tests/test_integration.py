"""One-process integration: trading, recording, dashboard engine and CLI wiring together."""

from __future__ import annotations

import pytest

from memecoin_agent import cli
from memecoin_agent.backtest import run_backtest
from memecoin_agent.config import Config
from memecoin_agent.recording import read_recordings, summarize_recordings
from memecoin_agent.statelock import StateLock, StateLocked


def write_cfg(tmp_path, **extra):
    state = tmp_path / "state"
    body = f"""
agent:
  tick_seconds: 15
  state_file: {state}/state.json
  trades_csv: {state}/trades.csv
  log_file: null
  kill_switch_file: {tmp_path}/STOP
recorder:
  dir: {tmp_path}/recordings
feeds:
  buffer_file: null
"""
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def test_state_lock_blocks_a_second_trader(tmp_path):
    first = StateLock(tmp_path / "state.json").acquire()
    with pytest.raises(StateLocked, match="already in use"):
        StateLock(tmp_path / "state.json").acquire()
    first.release()
    StateLock(tmp_path / "state.json").acquire().release()


def test_headless_run_trades_and_records_in_one_process(tmp_path):
    cfg_path = write_cfg(tmp_path)
    # 240 ticks x 15s = 1 simulated hour; the recorder samples every 30s on the same thread.
    cli.main(["-c", str(cfg_path), "run", "--synthetic", "--speed", "100000", "--max-ticks", "240", "--record"])
    rec_dir = tmp_path / "recordings-synthetic"  # synthetic data never mixes with real recordings
    summary = summarize_recordings(rec_dir)
    assert summary and summary["files"] >= 1
    assert not (tmp_path / "recordings").exists()
    records = list(read_recordings(rec_dir))
    kinds = {r["type"] for r in records}
    assert {"discover", "snap", "safety"} <= kinds
    snap_times = sorted({r["fetched_at"] for r in records if r["type"] == "snap"})
    assert 100 <= len(snap_times) <= 125  # ~one sample per 30 simulated seconds over the hour

    # What the session recorded is immediately usable by the backtester.
    res = run_backtest(Config(), records)
    assert res.records_read == len(records) and res.trades


def test_runner_recording_toggle(tmp_path):
    from memecoin_agent.recording import Recorder, RecordingWriter
    from memecoin_agent.simulation import SimClock, SyntheticMarket, SyntheticSafety
    from memecoin_agent.agent import Agent
    from memecoin_agent.execution.paper import PaperExecutor
    from memecoin_agent.portfolio import Portfolio
    from memecoin_agent.ui.runner import AgentRunner

    cfg = Config()
    cfg.agent.kill_switch_file = ""
    clock = SimClock()
    market = SyntheticMarket(clock, seed=2)
    agent = Agent(cfg, market, SyntheticSafety(market), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  clock=clock, persist=False)

    def advance(s):
        clock.advance(s)
        market.step(s)

    runner = AgentRunner(agent, advance=advance, speed=10_000, synthetic=True,
                         make_recorder=lambda: Recorder(cfg, market, SyntheticSafety(market),
                                                        RecordingWriter(tmp_path / "rec"), clock=clock))
    assert runner.view["recorder"] == {**runner.view["recorder"], "available": True, "active": False, "on_disk": None}
    runner.start()
    try:
        assert runner.submit("record_start")["ok"]
        runner.done.wait(0.3)
        v = runner.view["recorder"]
        assert v["active"] and v["records_written"] > 0 and v["on_disk"]["files"] >= 1
        runner.submit("record_stop")
        written = sum(1 for _ in read_recordings(tmp_path / "rec"))
        runner.done.wait(0.2)
        assert sum(1 for _ in read_recordings(tmp_path / "rec")) == written  # stopped means stopped
        assert runner.view["recorder"]["active"] is False
    finally:
        runner.stop()


def test_bare_command_opens_the_dashboard(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "cmd_ui", lambda args, cfg: seen.update(cmd=args.command, port=args.port))
    cli.main(["-c", str(write_cfg(tmp_path))])
    assert seen == {"cmd": "ui", "port": 8787}


def test_second_trading_process_is_refused(tmp_path):
    cfg_path = write_cfg(tmp_path)
    held = StateLock(tmp_path / "state" / "state.json").acquire()
    try:
        with pytest.raises(SystemExit, match="already in use"):
            cli.main(["-c", str(cfg_path), "run", "--max-ticks", "1"])
        with pytest.raises(SystemExit, match="Close all button"):
            cli.main(["-c", str(cfg_path), "close-all"])
    finally:
        held.release()
