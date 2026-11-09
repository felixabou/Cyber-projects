"""The scan-everything log inside the agent: every scanned coin is recorded, then followed up."""

from __future__ import annotations

from datetime import timedelta

from memecoin_agent.config import Config
from memecoin_agent.scanlog import ScanLog, load_rows

from .conftest import NOW, make_snap
from .test_agent import FakeMarket, FakeSafety, make_agent


def test_every_scanned_coin_is_logged_and_followed(tmp_path):
    good, weak = make_snap(mint="GOOD"), make_snap(mint="WEAK", liquidity_usd=1000)
    market = FakeMarket([good, weak])
    cfg = Config()
    agent, clock = make_agent(market, FakeSafety(), cfg, tmp_path=tmp_path)
    agent.scanlog = ScanLog(str(tmp_path / "scans.csv"))
    agent.tick()
    assert set(agent.scanlog.pending) == {"GOOD", "WEAK"}  # the rejected coin too
    assert agent.scanlog.pending["GOOD"]["bought"] == 1 and agent.scanlog.pending["WEAK"]["passed"] == 0
    assert "liquidity" in agent.scanlog.pending["WEAK"]["reasons"]
    for h in (15 * 60, 3600, 4 * 3600):  # time passes; the follow-up fetches the coins again
        clock.t = NOW + timedelta(seconds=h + 5)
        agent.scanlog.follow_up(clock.t.timestamp(), agent._fetch)
    rows = {r["mint"]: r for r in load_rows(tmp_path / "scans.csv")}
    assert set(rows) == {"GOOD", "WEAK"} and rows["GOOD"]["bought"] == "1" and rows["WEAK"]["ret_1h"] == "0.0"


def test_a_failing_scan_log_never_stops_trading(tmp_path):
    agent, clock = make_agent(FakeMarket([make_snap(mint="GOOD")]), FakeSafety(), tmp_path=tmp_path)

    class Broken:
        def observe(self, *a, **k):
            raise RuntimeError("disk full")

        def follow_up(self, *a, **k):
            raise RuntimeError("disk full")

    agent.scanlog = Broken()
    agent.tick()
    assert "GOOD" in agent.pf.positions
