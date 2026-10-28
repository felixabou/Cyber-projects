"""Trade downloads: one all-time journal, sliced on request (this session, the last 1-24 hours, all)."""

from __future__ import annotations

import csv
import io
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, load_config
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.export import RANGES, parse_range, select
from memecoin_agent.models import TradeRecord
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.simulation import SimClock
from memecoin_agent.ui.runner import AgentRunner, EquityHistory, LogBuffer
from memecoin_agent.ui.server import DashboardServer

from .test_agent import FakeMarket, FakeSafety

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def trade(t: datetime, side="buy", symbol="AAA", pnl=0.0) -> TradeRecord:
    return TradeRecord(t, side, f"MINT_{symbol}", symbol, 1000.0, 50.0, 0.05, 0.25, "test", pnl, None)


def journal(path, trades):
    Portfolio(cash_usd=0, trades=list(trades)).flush_trades(path)


def symbols(data: bytes) -> list[str]:
    return [r["symbol"] for r in csv.DictReader(io.StringIO(data.decode()))]


def test_parse_range():
    assert RANGES[0] == "session" and RANGES[-1] == "all" and "1h" in RANGES and "24h" in RANGES
    assert [parse_range(x) for x in ("session", "This session", "all", None, "", "6", "6h", "6 hours", "last 12h", "24")] \
        == ["session", "session", "all", "all", "all", "6h", "6h", "6h", "12h", "24h"]
    for bad in ("0", "25", "48h", "week", "-1"):
        with pytest.raises(ValueError):
            parse_range(bad)


def test_slices_of_the_one_journal(tmp_path):
    path = tmp_path / "trades.csv"
    journal(path, [trade(NOW - timedelta(days=3), symbol="OLD"),
                   trade(NOW - timedelta(hours=20), symbol="DAY"),
                   trade(NOW - timedelta(hours=5), symbol="FIVE"),
                   trade(NOW - timedelta(minutes=30), "sell", symbol="NEW", pnl=4.5)])
    with path.open("a") as fh:
        fh.write("2026-10-28T14:59:00+00:00,buy,PARTIAL")  # a row the agent is still writing
    session_start = NOW - timedelta(hours=6)
    get = lambda which: select(path, which, NOW, session_start)  # noqa: E731
    assert symbols(get("all").to_csv()) == ["OLD", "DAY", "FIVE", "NEW"]
    assert symbols(get("session").to_csv()) == ["FIVE", "NEW"]
    assert symbols(get("1h").to_csv()) == ["NEW"]
    assert symbols(get("6").to_csv()) == ["FIVE", "NEW"]
    assert symbols(get("24h").to_csv()) == ["DAY", "FIVE", "NEW"]
    assert get("all").to_csv().splitlines()[0] == path.read_bytes().splitlines()[0]  # same columns as the journal
    assert get("session").filename == "trades_session_2026-10-28_0900-1500_UTC.csv"
    assert get("6h").filename == "trades_last_6h_to_2026-10-28_1500_UTC.csv"
    assert get("all").filename == "trades_all_to_2026-10-28_1500_UTC.csv"
    assert get("1h").summary() == "Trades in the last 1 hour: 1 (0 buys, 1 sells), realized +$4.50"
    empty = select(tmp_path / "missing.csv", "all", NOW, NOW)
    assert empty.rows == [] and empty.to_csv().startswith(b"time,side,mint")


def test_old_config_files_with_an_export_section_still_load(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("export:\n  enabled: true\n  dir: exports\n  every_minutes: 60\ntelegram:\n  send_exports: true\n")
    assert load_config(p).export.dir == "exports"


def test_dashboard_downloads(tmp_path):
    cfg = Config()
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    journal(cfg.agent.trades_csv, [trade(NOW - timedelta(hours=30), symbol="OLD"),
                                   trade(NOW - timedelta(hours=2), symbol="TWO"),
                                   trade(NOW - timedelta(minutes=10), symbol="NEW")])
    clock = SimClock(NOW - timedelta(hours=1))  # this session started an hour ago
    agent = Agent(cfg, FakeMarket([]), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  clock=clock, persist=False)
    runner = AgentRunner(agent, equity=EquityHistory())
    clock.advance(3600)
    server = DashboardServer(runner, LogBuffer(), port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        c = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", headers={"X-UI-Token": server.token})
        for rng, want in (("session", ["NEW"]), ("3h", ["TWO", "NEW"]), ("all", ["OLD", "TWO", "NEW"]), (None, ["OLD", "TWO", "NEW"])):
            r = c.get("/api/trades.csv", params={"range": rng} if rng else None)
            assert r.status_code == 200 and symbols(r.content) == want
            assert r.headers["content-disposition"].startswith('attachment; filename="trades_')
        assert c.get("/api/trades.csv", params={"range": "48h"}).status_code == 400
        assert httpx.get(f"http://127.0.0.1:{server.port}/api/trades.csv").status_code == 403  # no token
        assert runner.view["downloads"]["ranges"][:2] == ["session", "1h"]
    finally:
        server.shutdown()
        server.server_close()
