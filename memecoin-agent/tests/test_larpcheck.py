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


def test_allow_mode_only_without_require_verdict():
    clock = Clock()
    fake = FakeLarpCheck({})
    with pytest.raises(ValueError, match="require_verdict"):
        Config(larpcheck=LarpCheckConfig(on_unchecked="allow")).validate()
    gate = make_gate(fake, clock=clock, require_verdict=False, on_unchecked="allow")
    assert gate.check("NEW1").ok and fake.started == []
    fake.coins["NEW1"] = {**RIGGED, "address": "NEW1"}  # someone checks it a few minutes later
    clock.t += 121
    assert not gate.check("NEW1").ok


def test_never_buys_without_a_verdict():
    clock = Clock()
    fake = FakeLarpCheck({})
    gate = make_gate(fake, clock=clock, max_checks_per_day=1, pending_max_minutes=5)
    assert gate.check("A1").reasons == [PENDING] and fake.started == ["A1"]
    b = gate.check("B2")  # no credits left: refused, not waved through
    assert not b.ok and "no checks left today" in b.reasons[0] and fake.started == ["A1"]
    clock.t += 5 * 60 + 1
    a = gate.check("A1")  # our check never finished: refused, and no second credit spent on it
    assert not a.ok and "still running" in a.reasons[0] and fake.started == ["A1"]
    fake.coins["A1"] = {**FAIR, "address": "A1"}  # the check finishes later: its verdict is used
    clock.t += 61
    assert gate.check("A1").ok

    fake2 = FakeLarpCheck({"RUN1": {**UNCHECKED, "address": "RUN1", "running": ["someone"]}})
    assert make_gate(fake2, clock=clock).check("RUN1").reasons == [PENDING]  # someone else's check: wait
    assert not make_gate(FakeLarpCheck({}), on_unchecked="reject").check("X9").ok


def test_outages_refuse_buys_unless_verdicts_are_optional():
    fake = FakeLarpCheck({})
    fake.down = True
    assert make_gate(fake).check("X1").reasons == [UNAVAILABLE]
    open_gate = make_gate(fake, require_verdict=False, on_unchecked="check")
    assert open_gate.check("X1") == SafetyReport(ok=True, score=None, reasons=[])
    assert make_gate(fake, require_verdict=False, on_unchecked="check", fail_closed=True).check("X1").reasons == [UNAVAILABLE]
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
    cfg.filters.require_rugcheck = False  # isolate the LarpCheck wiring (and stay off the network)
    cfg.larpcheck.api_key_file = str(tmp_path / "key")
    monkeypatch.delenv("LARPCHECK_API_KEY", raising=False)
    assert load_api_key(cfg.larpcheck) is None
    no_key = cli.build_safety(cfg)  # verdicts required but no key: every buy refused, loudly
    assert not no_key.check("ANY").ok and "no LarpCheck API key" in no_key.check("ANY").reasons[0]
    cfg.larpcheck.require_verdict = False
    cfg.larpcheck.on_unchecked = "check"
    assert not isinstance(cli.build_safety(cfg), CompositeSafety)  # optional: RugCheck only, with a warning
    cfg.larpcheck.require_verdict = True
    (tmp_path / "key").write_text("lc_file\n")
    assert load_api_key(cfg.larpcheck) == "lc_file"
    monkeypatch.setenv("LARPCHECK_API_KEY", "lc_env")
    assert load_api_key(cfg.larpcheck) == "lc_env"
    assert cli.build_safety(cfg).find(LarpCheckGate) is not None
    assert not isinstance(cli.build_safety(cfg, with_larpcheck=False), CompositeSafety)  # recorder
    cfg.larpcheck.enabled = False
    assert not isinstance(cli.build_safety(cfg), CompositeSafety)


# -- held coins: exits, dust and reasoning ------------------------------------------------------
from datetime import timedelta  # noqa: E402

from memecoin_agent.models import Position  # noqa: E402

REPORT = {  # shape of a real full report (MrBeast check), trimmed
    "eli5": "Someone launched a coin using a celebrity's name.",
    "summary": "The coin claims to be official but nothing connects it to the person.",
    "core": {"claim": "Presented as the celebrity's coin", "status": "contradicted", "how": "No post mentions it."},
    "findings": [
        {"kind": "risk", "title": "Impersonates a real person", "detail": "No official link mentions the coin.",
         "evidence": "https://x.com/someone"},
        {"kind": "risk", "title": "One crew holds 80%", "detail": "611 identical wallets.", "evidence": "javascript:alert(1)"},
        {"kind": "missing", "title": "No community outside the launch", "detail": "Only the coin's own pages.",
         "evidence": "https://example.com/site"},
        {"kind": "works", "title": "LP burned", "detail": "", "evidence": None},
    ],
    "watch": "A post from the person naming this contract would change the picture.",
}


def coin(mint, verdict, stamp, check_id="chk0000001", score=30, history_stamps=()):
    """A lookup_coin reply; history_stamps adds earlier checks with those coin stamps."""
    history = [{"id": f"old{i:07d}", "status": "done", "verdict": "PARTLY REAL",
                "stamps": [{"id": "coin", "word": w, "score": 20}]} for i, w in enumerate(history_stamps)]
    return {"address": mint, "checked": True, "running": [], "checks": history, "latest": {
        "id": check_id, "url": f"https://larpcheck.app/c/{check_id}", "status": "done", "verdict": verdict,
        "score": score, "stamps": [{"id": "story", "word": verdict, "score": score},
                                   {"id": "coin", "word": stamp, "score": 12}],
        "call": f"{verdict} call", "headline": f"{verdict} headline", "asOf": {"state": "current"}}}


class FakeWithReports(FakeLarpCheck):
    def __init__(self, coins, open_reports=("chk0000001",)):
        super().__init__(coins)
        self.open = set(open_reports)  # checks whose full report is open to this account

    def __call__(self, req):
        body = json.loads(req.content)
        name, args = body["params"]["name"], body["params"]["arguments"]
        if name in ("get_check", "open_report"):
            self.calls.append((name, args))
            if name == "open_report":
                self.open.add(args["id"])
            payload = {"id": args["id"], "status": "done",
                       "verdict": {"verdict": "LIKELY LARP", "stamps": [{"id": "coin", "word": "RIGGED", "score": 12,
                                                                         "line": "dev + 611 wallets hold 79.5%"}]},
                       "report": REPORT if args["id"] in self.open else None}
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
            return httpx.Response(200, json=msg)
        return super().__call__(req)


def held(mint="H1", symbol="HELD"):
    from .conftest import NOW
    return Position(mint=mint, symbol=symbol, pair_address="P", opened_at=NOW, entry_price_usd=0.001,
                    token_amount=50_000, cost_basis_usd=50, initial_cost_usd=50, peak_price_usd=0.001,
                    entry_liquidity_usd=50_000, last_price_usd=0.001, last_liquidity_usd=50_000)


def review_gate(fake, clock=None, **cfg):
    client = LarpCheckClient("lc_test", client=httpx.Client(transport=httpx.MockTransport(fake)))
    return LarpCheckGate(LarpCheckConfig(**cfg), client, clock=clock or Clock(), today=lambda: "2026-10-22")


def test_likely_larp_sells_80_keeps_dust_and_parses_reasoning():
    fake = FakeWithReports({"H1": coin("H1", "LIKELY LARP", "OK")})
    d = review_gate(fake).review_position(held())
    assert d.fraction == pytest.approx(0.8) and d.make_dust
    assert "20% kept as dust" in d.reason
    r = d.larp["reasoning"]
    assert r["source"] == "full report" and r["claim_status"] == "contradicted"
    assert [f["title"] for f in r["risks"]] == ["Impersonates a real person", "One crew holds 80%",
                                                "No community outside the launch"]
    assert r["risks"][2]["kind"] == "missing"
    assert r["risks"][1]["evidence"] is None  # only https evidence links are kept
    assert r["other_findings"][0]["title"] == "LP burned"
    assert r["watch"].startswith("A post from") and r["coin_line"] == "dev + 611 wallets hold 79.5%"


@pytest.mark.parametrize("verdict, stamp, history, fraction", [
    ("LIKELY LARP", "OK", (), 0.8),             # the one case that keeps dust
    ("LIKELY LARP", "NEUTRAL", (), 1.0),        # stamp not OK: no dust
    ("LIKELY LARP", "RIGGED", (), 1.0),
    ("LIKELY REAL", "RIGGED", (), 1.0),
    ("LIKELY LARP", "OK", ("RIGGED",), 1.0),    # OK now, but stamped RIGGED in an earlier check
    ("LARP", "OK", (), 1.0),                    # worse than LIKELY: sell everything
    ("SCAM", "SKETCHY", (), 1.0),
    ("PARTLY REAL", "RIGGED", (), 1.0),
])
def test_dust_only_for_likely_verdicts_stamped_ok_and_never_rigged(verdict, stamp, history, fraction):
    d = review_gate(FakeWithReports({"H1": coin("H1", verdict, stamp, history_stamps=history)})).review_position(held())
    assert d.fraction == pytest.approx(fraction) and d.make_dust == (fraction < 1)


def test_likely_real_held_until_anything_changes():
    clock = Clock()
    fake = FakeWithReports({"H1": coin("H1", "LIKELY REAL", "OK", "chkAAAAAAA")})
    gate = review_gate(fake, clock=clock)
    pos = held()
    assert gate.review_position(pos) is None and pos.larp["verdict"] == "LIKELY REAL"  # baseline stored
    clock.t += 60
    assert gate.review_position(pos) is None and sum(c[0] == "lookup_coin" for c in fake.calls) == 1  # throttled
    clock.t += 5 * 60
    assert gate.review_position(pos) is None  # same view: hold
    fake.coins["H1"] = coin("H1", "LIKELY REAL", "OK", "chkBBBBBBB")  # a new check, still LIKELY REAL + OK
    clock.t += 5 * 60
    d = gate.review_position(pos)
    assert d.fraction == pytest.approx(0.8) and d.make_dust and "changed from LIKELY REAL/OK" in d.reason

    pos1 = held("H1b")
    fake.coins["H1b"] = coin("H1b", "LIKELY REAL", "OK", "chkGGGGGGG")
    assert gate.review_position(pos1) is None
    fake.coins["H1b"] = coin("H1b", "LIKELY REAL", "SKETCHY", "chkHHHHHHH")  # stamp left the OK range: all of it
    clock.t += 5 * 60
    assert gate.review_position(pos1).fraction == 1.0

    pos2 = held("H2")
    fake.coins["H2"] = coin("H2", "LIKELY REAL", "OK", "chkCCCCCCC")
    assert gate.review_position(pos2) is None
    fake.coins["H2"] = coin("H2", "LARP", "RIGGED", "chkDDDDDDD")  # changed into something worse: sell it all
    clock.t += 5 * 60
    assert gate.review_position(pos2).fraction == 1.0

    pos3 = held("H3")
    fake.coins["H3"] = coin("H3", "PARTLY REAL", "OK", "chkEEEEEEE")
    assert gate.review_position(pos3) is None
    fake.coins["H3"] = coin("H3", "REAL", "OK", "chkFFFFFFF")  # not a LIKELY coin: changes don't sell it
    clock.t += 5 * 60
    assert gate.review_position(pos3) is None


def test_locked_reports_spend_one_credit_then_fall_back_to_summary():
    fake = FakeWithReports({"H1": coin("H1", "LIKELY LARP", "OK", "chkLOCKED1"),
                            "H2": coin("H2", "LIKELY LARP", "OK", "chkLOCKED2")}, open_reports=())
    gate = review_gate(fake, max_report_opens_per_day=1)
    assert gate.review_position(held("H1")).larp["reasoning"]["source"] == "full report"
    r2 = gate.review_position(held("H2")).larp["reasoning"]
    assert r2["source"] == "summary" and r2["summary"] == "LIKELY LARP headline" and r2["coin_line"]
    assert [c[0] for c in fake.calls].count("open_report") == 1


def test_held_unchecked_coin_gets_one_check_in_check_mode():
    clock = Clock()
    fake = FakeWithReports({})
    gate = review_gate(fake, clock=clock, on_unchecked="check")
    pos = held()
    assert gate.review_position(pos) is None
    clock.t += 5 * 60
    assert gate.review_position(pos) is None
    assert fake.started == ["H1"] and gate.checks_left_today() == 3
    assert review_gate(FakeWithReports({}), exit_on_flag=False).review_position(pos) is None


def test_agent_sells_80_then_leaves_dust_alone(tmp_path):
    from memecoin_agent.agent import Agent
    from memecoin_agent.execution.paper import PaperExecutor
    from memecoin_agent.portfolio import Portfolio

    from .conftest import NOW, make_snap
    from .test_agent import FakeMarket, FakeSafety

    cfg = Config()
    cfg.agent.kill_switch_file = ""
    cfg.risk.max_open_positions = 1
    fake = FakeWithReports({"GOOD": coin("GOOD", "LIKELY LARP", "OK")})
    clock = Clock()
    gate = review_gate(fake, clock=clock)
    market = FakeMarket([make_snap(mint="GOOD")])
    t = {"now": NOW}
    agent = Agent(cfg, market, FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  clock=lambda: t["now"], persist=False, guard=gate)
    agent.tick()  # buys GOOD (the fake safety passes it); then the guard reviews it on the next tick
    t["now"] += timedelta(minutes=1)
    agent.tick()
    pos = agent.pf.positions["GOOD"]
    assert pos.dust and pos.token_amount == pytest.approx(agent.pf.trades[0].token_amount * 0.2)
    assert pos.larp["reasoning"]["risks"]
    sells = [x for x in agent.pf.trades if x.side == "sell"]
    assert len(sells) == 1 and "kept as dust" in sells[0].reason

    # Dust: no strategy exits (even a -90% crash), no slot taken; re-checked only for still qualifying.
    market.snaps["GOOD"] = make_snap(mint="GOOD", price_usd=0.0001)
    clock.t += 3600
    t["now"] += timedelta(minutes=10)
    agent.tick()
    assert "GOOD" in agent.pf.positions and agent.pf.positions["GOOD"].dust
    assert agent.risk.entry_block_reason(agent.pf, t["now"]) is None
    restored = Position.from_dict(pos.to_dict())
    assert restored.dust and restored.larp["verdict"] == "LIKELY LARP"
    assert agent.close_position("GOOD") and "GOOD" not in agent.pf.positions  # manual close takes the dust


def test_dust_that_stops_qualifying_is_sold():
    clock = Clock()
    fake = FakeWithReports({"H1": coin("H1", "LIKELY LARP", "OK", "chkAAAAAAA")})
    gate = review_gate(fake, clock=clock)
    pos = held()
    pos.dust = True
    assert gate.review_dust(pos) is None  # still LIKELY LARP + OK, never rigged: keep
    fake.coins["H1"] = coin("H1", "LIKELY LARP", "OK", "chkBBBBBBB", history_stamps=("RIGGED",))
    clock.t += 60
    assert gate.review_dust(pos) is None  # throttled
    clock.t += 5 * 60
    d = gate.review_dust(pos)
    assert d.fraction == 1.0 and "RIGGED" in d.reason and not d.make_dust
    fake.coins["H2"] = coin("H2", "LIKELY REAL", "SKETCHY")
    assert "coin stamp is SKETCHY" in gate.review_dust(held("H2")).reason
