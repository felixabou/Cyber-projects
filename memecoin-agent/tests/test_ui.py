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
    assert client.get("/share").status_code == 404
    assert client.get("/api/share/view").status_code == 404

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
    # A normal tick: the fixture's 0.05 s one would replay the 2 hours as 144,000 ticks.
    r = client.post("/api/backtest", json={"dir": str(tmp_path / "rec"),
                                           "overrides": ["filters.min_score=0.6", "agent.tick_seconds=10"]})
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


def test_friend_share_is_read_only_and_redacted(dashboard):
    _, runner, _, _ = dashboard
    with pytest.raises(ValueError, match="must differ"):
        DashboardServer(runner, LogBuffer(), port=0, password="same", share_password="same")

    server = DashboardServer(runner, LogBuffer(), port=0, password="owner-pass",
                             share_password="friend-pass")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.port}"
    guest_auth = "Basic " + base64.b64encode(b"friend:friend-pass").decode()
    owner_auth = "Basic " + base64.b64encode(b"owner:owner-pass").decode()
    guest = {"Authorization": guest_auth, "X-Share-Token": server.share_token}
    http = httpx.Client(trust_env=False)
    try:
        assert http.get(root + "/share").status_code == 401
        assert http.get(root + "/share", headers={"Authorization": owner_auth}).status_code == 401
        page = http.get(root + "/share", headers={"Authorization": guest_auth})
        assert page.status_code == 200 and server.share_token in page.text
        assert server.token not in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert http.get(root + "/static/share.js", headers=guest).status_code == 200
        assert http.get(root + "/static/app.js", headers=guest).status_code == 401
        assert http.get(root + "/api/share/view", headers={"Authorization": guest_auth}).status_code == 403
        shared = http.get(root + "/api/share/view", headers=guest).json()
        assert shared["positions"] and "symbol" in shared["positions"][0]
        assert {"mint", "cash", "equity", "config", "scan", "signature", "token"}.isdisjoint(shared)
        assert {"mint", "cost_basis_usd", "initial_cost_usd", "url", "larp"}.isdisjoint(shared["positions"][0])
        assert {"signature", "mint", "token_amount", "usd_value"}.isdisjoint(shared["trades"][0])
        chart = http.get(root + "/api/share/equity", headers=guest).json()
        assert all(len(point) == 2 for point in chart["points"])
        assert http.get(root + "/api/view", headers={**guest, "X-UI-Token": server.token}).status_code == 401
        assert http.get(root + "/api/log", headers=guest).status_code == 401
        assert http.get(root + "/api/config", headers=guest).status_code == 401
        assert http.get(root + "/api/trades.csv", headers=guest).status_code == 401
        assert http.post(root + "/api/control", headers={**guest, "X-UI-Token": server.token},
                          json={"action": "close_all"}).status_code == 401
        assert http.post(root + "/api/share/control", headers=guest,
                          json={"action": "close_all"}).status_code == 401
    finally:
        http.close()
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


def test_manual_trades_from_the_dashboard(tmp_path):
    from .test_lanes import FakeMulti

    cfg = Config()
    cfg.agent.tick_seconds = 0.05
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    rc = make_snap(mint="0x" + "ab" * 20, chain="robinhood", symbol="ROBINCAT")
    clock = SimClock(NOW)
    agent = Agent(cfg, FakeMarket([]), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  clock=clock, persist=False, multichain=FakeMulti([rc]))
    runner = AgentRunner(agent, equity=EquityHistory(tmp_path / "equity.csv"), advance=lambda s: clock.advance(15))
    server = DashboardServer(runner, LogBuffer(), port=0, config_dict=redacted_config(cfg))
    runner.start()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", headers={"X-UI-Token": server.token}, timeout=10)
    try:
        found = client.get("/api/lookup", params={"q": rc.mint}).json()["results"]
        assert found[0]["symbol"] == "ROBINCAT" and found[0]["chain"] == "robinhood"
        over_cap = client.post("/api/control", json={"action": "manual_buy", "chain": "robinhood",
                                                  "address": rc.mint, "usd": 200}).json()
        assert not over_cap["ok"] and "risk.max_position_usd" in over_cap["error"]
        r = client.post("/api/control", json={"action": "manual_buy", "chain": "robinhood", "address": rc.mint,
                                              "usd": 30, "managed": False}).json()
        assert r["ok"] and r["message"].startswith("Bought $30.00 of ROBINCAT")
        pos = client.get("/api/view").json()["positions"][0]
        assert pos["lane"] == "manual" and pos["managed"] is False and pos["stage"] == "manual"
        assert pos["url"].startswith("https://dexscreener.com/robinhood/")
        assert client.post("/api/control", json={"action": "set_managed", "mint": rc.mint, "managed": True}).json()["ok"]
        assert client.post("/api/control", json={"action": "sell", "mint": rc.mint, "fraction": 0.5}).json()["ok"]
        pos = client.get("/api/view").json()["positions"][0]
        assert pos["managed"] is True and abs(pos["value_usd"] - 15) < 1
        bad = client.post("/api/control", json={"action": "manual_buy", "chain": "robinhood", "address": rc.mint, "usd": 5})
        assert bad.status_code == 400 and "Already holding" in bad.json()["error"]
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        runner.stop()

