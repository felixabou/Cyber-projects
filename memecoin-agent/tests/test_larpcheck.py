"""LarpCheck gate, against a mock of its MCP-over-HTTP API (shapes captured from the live service)."""

from __future__ import annotations

import json

import httpx
import pytest

from memecoin_agent.config import Config, LarpCheckConfig
from memecoin_agent.data.larpcheck import (
    PENDING,
    UNAVAILABLE,
    CompositeSafety,
    LarpCheckClient,
    LarpCheckGate,
    load_api_key,
)
from memecoin_agent.models import SafetyReport

RIGGED = {  # trimmed from the real CYBERCOIN lookup
    "address": "RIGGED1", "chain": "solana", "symbol": "CYBERCOIN", "checked": True, "running": [],
    "latest": {
        "id": "r5r0a1gPyt", "url": "https://larpcheck.app/c/r5r0a1gPyt", "status": "done",
        "verdict": "LIKELY LARP", "score": 20,
        "stamps": [{"id": "story", "word": "LIKELY LARP", "score": 20}, {"id": "coin", "word": "RIGGED", "score": 12}],
        "call": "Empty story, rigged coin: linked wallets hold 82% of supply.",
        "asOf": {"state": "current", "coinAgeMinAtRead": 28},
    },
}
FAIR = {"address": "FAIR1", "checked": True, "running": [], "latest": {
    "id": "okokokokok", "url": "https://larpcheck.app/c/okokokokok", "status": "done", "verdict": "PARTLY REAL",
    "score": 61, "stamps": [{"id": "story", "word": "PARTLY REAL", "score": 61}, {"id": "coin", "word": "NEUTRAL", "score": 55}],
    "call": "Real meme, ordinary launch.", "asOf": {"state": "current"}}}
UNCHECKED = {"address": "NEW1", "checked": False, "latest": None, "running": [], "checks": []}


class FakeLarpCheck:
    def __init__(self, coins: dict, sse: bool = False):
        self.coins = coins
        self.sse = sse
        self.calls: list[tuple[str, dict]] = []
        self.started: list[str] = []
        self.down = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer lc_test"
        if self.down:
            return httpx.Response(503)
        body = json.loads(req.content)
        name, args = body["params"]["name"], body["params"]["arguments"]
        self.calls.append((name, args))
        if name == "lookup_coin":
            payload = self.coins.get(args["address"], {**UNCHECKED, "address": args["address"]})
        elif name == "start_check":
            assert args == {"input": args["input"], "wait": False}
            self.started.append(args["input"])
            payload = {"id": "CS5fRaau2A", "url": "https://larpcheck.app/c/CS5fRaau2A", "reused": False}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]}})
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
        if self.sse:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(msg)}\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=msg)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_gate(fake, clock=None, **cfg):
    client = LarpCheckClient("lc_test", client=httpx.Client(transport=httpx.MockTransport(fake)))
    return LarpCheckGate(LarpCheckConfig(**cfg), client, clock=clock or Clock(), today=lambda: "2026-10-22")


@pytest.mark.parametrize("sse", [False, True])
def test_rigged_coin_is_blocked_with_evidence_link_and_cached(sse):
    fake = FakeLarpCheck({"RIGGED1": RIGGED, "FAIR1": FAIR}, sse=sse)
    gate = make_gate(fake)
    rep = gate.check("RIGGED1")
    assert not rep.ok and rep.score == 20
    assert "LIKELY LARP 20/100, coin RIGGED 12/100" in rep.reasons[0]
    assert rep.reasons[0].endswith("(https://larpcheck.app/c/r5r0a1gPyt)")
    gate.check("RIGGED1")
    assert len(fake.calls) == 1  # cached
    assert gate.check("FAIR1").ok


def test_thresholds_are_configurable():
    fake = FakeLarpCheck({"FAIR1": FAIR, "RIGGED1": RIGGED})
    assert not make_gate(fake, min_coin_score=60).check("FAIR1").ok
    assert not make_gate(fake, reject_verdicts=["PARTLY REAL"]).check("FAIR1").ok
    assert make_gate(fake, reject_verdicts=[], reject_coin_stamps=[]).check("RIGGED1").ok


def test_unchecked_allow_is_free_and_rechecked_soon():
    clock = Clock()
    fake = FakeLarpCheck({})
    gate = make_gate(fake, clock=clock)
    assert gate.check("NEW1").ok
    assert fake.started == []  # never spends a credit by default
    fake.coins["NEW1"] = {**RIGGED, "address": "NEW1"}  # someone else checks it a minute later
    clock.t += 60
    assert gate.check("NEW1").ok  # still the short-lived cached "no verdict"
    clock.t += 61
    assert not gate.check("NEW1").ok  # the new verdict is picked up


def test_check_mode_starts_one_check_waits_then_judges():
    clock = Clock()
    fake = FakeLarpCheck({})
    gate = make_gate(fake, clock=clock, on_unchecked="check", max_checks_per_day=2)
    assert gate.check("NEW1").reasons == [PENDING]
    # The lookup doesn't list it as running yet (race): still pending, and no second credit spent.
    clock.t += 15
    assert gate.check("NEW1").reasons == [PENDING]
    assert fake.started == ["NEW1"] and gate.checks_left_today() == 1
    fake.coins["NEW1"] = {**RIGGED, "address": "NEW1"}
    clock.t += 45
    assert not gate.check("NEW1").ok and "RIGGED" in gate.check("NEW1").reasons[0]


def test_check_budget_and_stuck_checks_fall_back_to_allow():
    clock = Clock()
    fake = FakeLarpCheck({})
    gate = make_gate(fake, clock=clock, on_unchecked="check", max_checks_per_day=1, pending_max_minutes=5)
    assert gate.check("A1").reasons == [PENDING]
    assert gate.check("B2").ok and fake.started == ["A1"]  # budget spent: B2 goes ahead on other checks
    clock.t += 5 * 60 + 1
    assert gate.check("A1").ok  # our check never finished: don't block forever, don't re-spend
    assert fake.started == ["A1"]

    fake2 = FakeLarpCheck({"RUN1": {**UNCHECKED, "address": "RUN1", "running": ["someone"]}})
    g2 = make_gate(fake2, clock=clock)
    assert g2.check("RUN1").reasons == [PENDING]  # someone else's check is running: wait for it


def test_outages_fail_open_by_default_closed_on_request():
    fake = FakeLarpCheck({})
    fake.down = True
    assert make_gate(fake).check("X1") == SafetyReport(ok=True, score=None, reasons=[])
    assert make_gate(fake, fail_closed=True).check("X1").reasons == [UNAVAILABLE]
    fake.down = False
    client = LarpCheckClient("lc_test", client=httpx.Client(transport=httpx.MockTransport(fake)))
    with pytest.raises(Exception, match="nope"):
        client.call("mystery_tool", {})  # isError results surface as errors


def test_composite_runs_cheapest_first_and_short_circuits():
    class Rug:
        def __init__(self, ok):
            self.ok = ok

        def check(self, mint):
            return SafetyReport(self.ok, 10.0, [] if self.ok else ["rugcheck danger: Mint Authority"])

    fake = FakeLarpCheck({"RIGGED1": RIGGED})
    gate = make_gate(fake)
    assert CompositeSafety(Rug(False), gate).check("RIGGED1").reasons == ["rugcheck danger: Mint Authority"]
    assert fake.calls == []  # RugCheck already said no: LarpCheck not consulted
    assert "RIGGED" in CompositeSafety(Rug(True), gate).check("RIGGED1").reasons[0]
    assert CompositeSafety(Rug(True), None).check("ANY").ok


def test_key_loading_and_wiring(monkeypatch, tmp_path):
    from memecoin_agent import cli

    cfg = Config()
    cfg.larpcheck.api_key_file = str(tmp_path / "key")
    monkeypatch.delenv("LARPCHECK_API_KEY", raising=False)
    assert load_api_key(cfg.larpcheck) is None
    assert not isinstance(cli.build_safety(cfg), CompositeSafety)  # no key: RugCheck only, with a warning
    (tmp_path / "key").write_text("lc_file\n")
    assert load_api_key(cfg.larpcheck) == "lc_file"
    monkeypatch.setenv("LARPCHECK_API_KEY", "lc_env")
    assert load_api_key(cfg.larpcheck) == "lc_env"
    assert isinstance(cli.build_safety(cfg), CompositeSafety)
    assert not isinstance(cli.build_safety(cfg, with_larpcheck=False), CompositeSafety)  # recorder
    cfg.larpcheck.enabled = False
    assert not isinstance(cli.build_safety(cfg), CompositeSafety)
