from __future__ import annotations

import base64
import json
import threading
import time
from datetime import timedelta

import httpx
import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.recording import Recorder, RecordingWriter
from memecoin_agent.simulation import SimClock, SyntheticMarket, SyntheticSafety
from memecoin_agent.ui import redacted_config
from memecoin_agent.ui.runner import AgentRunner, EquityHistory, LogBuffer
from memecoin_agent.ui.server import DashboardServer

from .conftest import NOW, make_snap
from .test_agent import FakeMarket, FakeSafety


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config()
    cfg.agent.tick_seconds = 0.05
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    cfg.recorder.dir = str(tmp_path / "rec")
    market = FakeMarket([make_snap(mint="GOOD"), make_snap(mint="BAD")])
    clock = SimClock(NOW)
    agent = Agent(cfg, market, FakeSafety(unsafe={"BAD"}), PaperExecutor(cfg.execution),
                  Portfolio(cash_usd=1000), clock=clock, persist=False)
    runner = AgentRunner(agent, equity=EquityHistory(tmp_path / "equity.csv"),
                         advance=lambda s: clock.advance(15), speed=1.0)
    server = DashboardServer(runner, LogBuffer(), port=0, config_dict=redacted_config(cfg))
    runner.start()
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", headers={"X-UI-Token": server.token}, timeout=10)
    _wait(lambda: runner.view.get("ticks", 0) >= 1)
    yield server, runner, client, market
    client.close()
    server.shutdown()
    server.server_close()
    runner.stop()


def _wait(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_page_and_auth(dashboard):
    server, _, client, _ = dashboard
    page = client.get("/", headers={"X-UI-Token": ""})
    assert page.status_code == 200 and server.token in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200

    anon = httpx.get(f"http://127.0.0.1:{server.port}/api/view")
    assert anon.status_code == 403  # no token
    rebound = client.get("/api/view", headers={"Host": "attacker.example:80"})
    assert rebound.status_code == 403  # DNS-rebinding style request
    assert client.post("/api/control", json={"action": "pause"}, headers={"X-UI-Token": "wrong"}).status_code == 403
    assert client.get("/static/../server.py").status_code in (403, 404)


def test_view_reflects_agent_and_controls_work(dashboard):
    _, runner, client, market = dashboard
    v = client.get("/api/view").json()
    assert v["mode"] == "paper" and [p["mint"] for p in v["positions"]] == ["GOOD"]
    verdicts = {c["mint"]: c["outcome"] for c in v["scan"]["candidates"]}
    assert verdicts["GOOD"] == "bought" and verdicts["BAD"].startswith("unsafe")
    assert v["trades"][0]["side"] == "buy"

    assert client.post("/api/control", json={"action": "pause"}).json()["ok"]
    assert client.get("/api/view").json()["entries_paused"] is True
    client.post("/api/control", json={"action": "resume"})
    assert client.get("/api/view").json()["entries_paused"] is False

    r = client.post("/api/control", json={"action": "close", "mint": "GOOD"})
    assert r.status_code == 200 and "Closed" in r.json()["message"]
    v = client.get("/api/view").json()
    assert v["positions"] == [] and v["trades"][0]["reason"] == "manual close (dashboard)"

    assert client.post("/api/control", json={"action": "close", "mint": "NOPE"}).status_code == 400
    assert client.post("/api/control", json={"action": "selfdestruct"}).status_code == 400
    assert client.get("/api/equity?range=all").json()["points"]


def test_dashboard_backtest_uses_a_config_copy(dashboard, tmp_path):
    server, runner, client, _ = dashboard
    cfg = Config()
    clock = SimClock()
    market = SyntheticMarket(clock, seed=3)

    def advance(_):
        clock.advance(cfg.recorder.interval_s)
        market.step(cfg.recorder.interval_s)

    Recorder(cfg, market, SyntheticSafety(market), RecordingWriter(tmp_path / "rec"), clock=clock, sleep=advance).run(hours=2)

    bad = client.post("/api/backtest", json={"overrides": ["filters.nope=1"]})
    assert bad.status_code == 400 and "filters.nope" in bad.json()["error"]

    kill_before = runner.agent.cfg.agent.kill_switch_file
    r = client.post("/api/backtest", json={"dir": str(tmp_path / "rec"), "overrides": ["filters.min_score=0.6"]})
    assert r.status_code == 200
    _wait(lambda: client.get("/api/backtest").json()["status"] != "running", timeout=60)
    res = client.get("/api/backtest").json()
    assert res["status"] == "done", res
    assert res["stats"]["round_trips"] == len(res["closed"]) and res["equity"]
    # run_backtest rewrites mode/kill switch on its config; the live agent's must be untouched.
    assert runner.agent.cfg.agent.kill_switch_file == kill_before
    assert runner.agent.cfg.filters.min_score == Config().filters.min_score


def test_remote_binding_requires_password(tmp_path):
    cfg = Config()
    agent = Agent(cfg, FakeMarket([]), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  persist=False)
    runner = AgentRunner(agent)
    with pytest.raises(ValueError, match="password"):
        DashboardServer(runner, LogBuffer(), host="0.0.0.0", port=0)

    server = DashboardServer(runner, LogBuffer(), host="0.0.0.0", port=0, password="s3cret")
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.port}/"
        assert httpx.get(url).status_code == 401
        auth = "Basic " + base64.b64encode(b"user:s3cret").decode()
        assert httpx.get(url, headers={"Authorization": auth}).status_code == 200
        wrong = "Basic " + base64.b64encode(b"user:nope").decode()
        assert httpx.get(url, headers={"Authorization": wrong}).status_code == 401
    finally:
        server.shutdown()
        server.server_close()


def test_config_redaction_and_equity_history(tmp_path):
    cfg = Config()
    cfg.execution.rpc_url = "https://mainnet.helius-rpc.com/?api-key=abc123"
    cfg.execution.jupiter_api_key = "jup-secret"
    d = redacted_config(cfg)
    assert "abc123" not in json.dumps(d) and "jup-secret" not in json.dumps(d)
    assert d["execution"]["rpc_url"].startswith("https://mainnet.helius-rpc.com")

    path = tmp_path / "equity.csv"
    eq = EquityHistory(path)
    for i in range(10):
        eq.add(NOW + timedelta(seconds=30 * i), 1000 + i)
    assert len(eq.series()) == 10
    reloaded = EquityHistory(path)  # persisted once a minute
    assert [p[1] for p in reloaded.series()] == [1000.0, 1002.0, 1004.0, 1006.0, 1008.0]
    assert len(eq.series(max_points=3)) == 3 and eq.series(max_points=3)[-1][1] == 1009


def test_log_buffer_incremental():
    import logging

    buf = LogBuffer(maxlen=3)
    logger = logging.getLogger("memecoin_agent.test_ui")
    logger.addHandler(buf)
    logger.setLevel(logging.INFO)
    try:
        for i in range(5):
            logger.info("msg %d", i)
    finally:
        logger.removeHandler(buf)
    assert [e["msg"] for e in buf.since(0)] == ["msg 2", "msg 3", "msg 4"]
    assert [e["msg"] for e in buf.since(buf.seq - 1)] == ["msg 4"]
