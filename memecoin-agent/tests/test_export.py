"""Hourly trade CSV exports: period files, catch-up after restarts, and the runner/dashboard wiring."""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timedelta, timezone

import httpx

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, ExportConfig
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.export import TradeExporter
from memecoin_agent.models import TradeRecord
from memecoin_agent.portfolio import Portfolio, read_trades_csv
from memecoin_agent.simulation import SimClock
from memecoin_agent.ui.runner import AgentRunner, EquityHistory, LogBuffer
from memecoin_agent.ui.server import DashboardServer

from .conftest import make_snap
from .test_agent import FakeMarket, FakeSafety
from .test_ui import _wait

UTC = timezone.utc


def trade(t: datetime, side="buy", symbol="AAA", pnl=0.0) -> TradeRecord:
    return TradeRecord(t, side, f"MINT_{symbol}", symbol, 1000.0, 50.0, 0.05, 0.25, "test", pnl, None)


def journal(path, trades):
    pf = Portfolio(cash_usd=0, trades=list(trades))
    pf.flush_trades(path)


def rows(path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_period_files_skip_empty_and_unfinished_periods(tmp_path):
    csv_path = tmp_path / "trades.csv"
    day = datetime(2026, 10, 22, tzinfo=UTC)
    journal(csv_path, [trade(day + timedelta(hours=13, minutes=5)),
                       trade(day + timedelta(hours=13, minutes=59, seconds=59), "sell", pnl=7.5),
                       trade(day + timedelta(hours=15, minutes=1), symbol="BBB")])
    exp = TradeExporter(ExportConfig(dir=str(tmp_path / "exports")), csv_path, now=day + timedelta(hours=15, minutes=30))
    out = exp.run(day + timedelta(hours=15, minutes=30))
    assert [f.path.name for f in out] == ["trades_2026-10-22_1300-1400_UTC.csv"]  # 14h empty, 15h unfinished
    assert [r["side"] for r in rows(out[0].path)] == ["buy", "sell"]
    assert "2 (1 buys, 1 sells), realized +$7.50" in out[0].summary()
    assert not exp.due(day + timedelta(hours=15, minutes=59)) and exp.run(day + timedelta(hours=15, minutes=59)) == []
    out = exp.run(day + timedelta(hours=16, minutes=0, seconds=3))
    assert [f.path.name for f in out] == ["trades_2026-10-22_1500-1600_UTC.csv"]
    assert [p.name for p in exp.files()] == ["trades_2026-10-22_1500-1600_UTC.csv", "trades_2026-10-22_1300-1400_UTC.csv"]


def test_restart_catches_up_without_rewriting(tmp_path):
    csv_path = tmp_path / "trades.csv"
    day = datetime(2026, 10, 22, tzinfo=UTC)
    journal(csv_path, [trade(day - timedelta(days=3)), trade(day + timedelta(hours=2, minutes=10)),
                       trade(day + timedelta(hours=23, minutes=30))])
    cfg = ExportConfig(dir=str(tmp_path / "exports"))
    first = TradeExporter(cfg, csv_path, now=day + timedelta(hours=3))
    assert [f.path.name for f in first.run(day + timedelta(hours=3))] == ["trades_2026-10-22_0200-0300_UTC.csv"]
    # Agent off until the next day: the 23:00 period is filled in, the 02:00 one isn't written twice,
    # and the 3-day-old trade is beyond the look-back.
    later = TradeExporter(cfg, csv_path, now=day + timedelta(days=1, hours=1))
    assert [f.path.name for f in later.run(day + timedelta(days=1, hours=1))] == ["trades_2026-10-22_2300-2400_UTC.csv"]


def test_other_period_lengths_and_validation(tmp_path):
    csv_path = tmp_path / "trades.csv"
    day = datetime(2026, 10, 22, tzinfo=UTC)
    journal(csv_path, [trade(day + timedelta(hours=9, minutes=40))])
    exp = TradeExporter(ExportConfig(dir=str(tmp_path / "x"), every_minutes=30), csv_path, now=day + timedelta(hours=10))
    assert [f.path.name for f in exp.run(day + timedelta(hours=10))] == ["trades_2026-10-22_0930-1000_UTC.csv"]
    cfg = Config()
    cfg.export.every_minutes = 7
    try:
        cfg.validate()
    except ValueError as exc:
        assert "every_minutes" in str(exc)
    else:
        raise AssertionError("7 doesn't divide a day")


def test_runner_exports_and_tells_listeners(tmp_path):
    cfg = Config()
    cfg.agent.tick_seconds = 0.02
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    cfg.agent.state_file = str(tmp_path / "state.json")
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    cfg.export.dir = str(tmp_path / "exports")
    start = datetime(2026, 10, 22, 13, 50, tzinfo=UTC)
    clock = SimClock(start)
    snap = make_snap(mint="GOOD", pair_created_at=start - timedelta(hours=3), fetched_at=start)
    agent = Agent(cfg, FakeMarket([snap]), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000), clock=clock)

    class Listener:
        def __init__(self):
            self.trades, self.exports = [], []

        def notify(self, trades, exports):
            self.trades += trades
            self.exports += exports

    listener = Listener()
    runner = AgentRunner(agent, equity=EquityHistory(), advance=lambda s: clock.advance(60),
                         exporter=TradeExporter(cfg.export, cfg.agent.trades_csv, clock()))
    runner.listeners.append(listener)
    runner.start()
    try:
        _wait(lambda: listener.exports)
    finally:
        runner.stop()
    assert listener.trades and listener.trades[0].side == "buy" and listener.trades[0].mint == "GOOD"
    exported = listener.exports[0]
    assert exported.path.name == "trades_2026-10-22_1300-1400_UTC.csv"
    assert [r["mint"] for r in rows(exported.path)] == ["GOOD"]
    assert read_trades_csv(cfg.agent.trades_csv)[0].time.tzinfo is not None
    assert runner.view["export"] == {"dir": cfg.export.dir, "every_minutes": 60}


def test_dashboard_lists_and_serves_exports_only(tmp_path):
    cfg = Config()
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    cfg.export.dir = str(tmp_path / "exports")
    day = datetime(2026, 10, 22, tzinfo=UTC)
    journal(cfg.agent.trades_csv, [trade(day + timedelta(hours=1))])
    exporter = TradeExporter(cfg.export, cfg.agent.trades_csv, day + timedelta(hours=2))
    exporter.run(day + timedelta(hours=2))
    (tmp_path / "secret.txt").write_text("not for download")
    agent = Agent(cfg, FakeMarket([]), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  clock=SimClock(day), persist=False)
    runner = AgentRunner(agent, equity=EquityHistory(), exporter=exporter)
    server = DashboardServer(runner, LogBuffer(), port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        c = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", headers={"X-UI-Token": server.token})
        name = "trades_2026-10-22_0100-0200_UTC.csv"
        assert c.get("/api/exports").json() == {"files": [{"name": name, "bytes": (exporter.dir / name).stat().st_size}]}
        r = c.get(f"/api/exports/{name}")
        assert r.status_code == 200 and "MINT_AAA" in r.text and name in r.headers["content-disposition"]
        for bad in ("..%2Fsecret.txt", "../secret.txt", "trades_2026-10-22_0100-0300_UTC.csv"):
            assert c.get(f"/api/exports/{bad}").status_code == 404
        assert httpx.get(f"http://127.0.0.1:{server.port}/api/exports/{name}").status_code == 403  # no token
    finally:
        server.shutdown()
        server.server_close()
