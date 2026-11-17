"""larpcheck.paper_policy / live_policy: every way LarpCheck can fail to give a verdict, under strict and fallback."""

from __future__ import annotations

import json

import httpx
import pytest

from memecoin_agent.config import Config, LarpCheckConfig, load_config
from memecoin_agent.data.larpcheck import (
    BAD_VERDICT,
    FALLBACK,
    NO_CREDITS,
    NO_KEY,
    NO_VERDICT,
    OUTAGE,
    PENDING_CHECK,
    STRICT,
    TIMEOUT,
    CompositeSafety,
    MissingLarpCheck,
    policy_for,
)

from .test_larpcheck import FAIR, RIGGED, Clock, FakeLarpCheck, make_gate

LARP = {**FAIR, "address": "LARP1", "latest": {**FAIR["latest"], "verdict": "LARP", "score": 9}}


class NoCredits(FakeLarpCheck):
    def __call__(self, req):
        if json.loads(req.content)["params"]["name"] == "start_check":
            return httpx.Response(402, text="Out of credits: upgrade to Pro")
        return super().__call__(req)


class PlanRefused(FakeLarpCheck):
    def __call__(self, req):
        return httpx.Response(403, text="This tool needs a premium plan")


def counts(gate):
    return {k.removeprefix("larpcheck_"): v for k, v in gate.counters.items()}


# -- 1. explicit bad verdict: never bought, under either policy -----------------------------------------------

@pytest.mark.parametrize("policy", [STRICT, FALLBACK])
def test_bad_verdict_always_rejected(policy):
    gate = make_gate(FakeLarpCheck({"RIGGED1": RIGGED, "LARP1": LARP}), policy=policy)
    for mint in ("RIGGED1", "LARP1"):
        rep = gate.check(mint)
        assert not rep.ok and rep.category == BAD_VERDICT
    assert counts(gate) == {"rejected": 2, "unavailable": 0, "fallback_allowed": 0, "pending": 0}


def test_a_bad_verdict_seen_earlier_still_blocks_during_an_outage_under_fallback():
    clock = Clock()
    fake = FakeLarpCheck({"RIGGED1": RIGGED})
    gate = make_gate(fake, clock=clock, policy=FALLBACK)
    assert not gate.check("RIGGED1").ok
    fake.down = True
    clock.t += 4000  # past the cache
    rep = gate.check("RIGGED1")
    assert not rep.ok and rep.category == BAD_VERDICT


# -- 2. no verdict yet ------------------------------------------------------------------------------------------

def test_no_verdict_strict_rejects_fallback_allows():
    strict = make_gate(FakeLarpCheck({}), policy=STRICT, on_unchecked="allow")
    rep = strict.check("NEW1")
    assert not rep.ok and rep.category == NO_VERDICT and rep.reasons[0].startswith("larpcheck no verdict: not checked yet")
    fb = make_gate(FakeLarpCheck({}), policy=FALLBACK, on_unchecked="allow")
    rep = fb.check("NEW1")
    assert rep.ok and rep.category == NO_VERDICT
    assert counts(fb) == {"rejected": 0, "unavailable": 1, "fallback_allowed": 1, "pending": 0}
    assert counts(strict) == {"rejected": 1, "unavailable": 1, "fallback_allowed": 0, "pending": 0}


def test_on_unchecked_reject_is_explicit_under_both_policies():
    for policy in (STRICT, FALLBACK):
        rep = make_gate(FakeLarpCheck({}), policy=policy, on_unchecked="reject").check("X9")
        assert not rep.ok and rep.category == NO_VERDICT


def test_meme_without_a_verdict_strict_rejects_fallback_allows():
    for policy, ok in ((STRICT, False), (FALLBACK, True)):
        gate = make_gate(FakeLarpCheck({}), policy=policy)
        gate.kinds["MEME"] = "meme"
        rep = gate.check("MEME")
        assert rep.ok is ok and rep.category == NO_VERDICT
    off = make_gate(FakeLarpCheck({}), policy=STRICT, memes="off")
    off.kinds["MEME"] = "meme"
    assert off.check("MEME").ok  # memes: off leaves memes out of LarpCheck entirely


# -- 3. API outage ----------------------------------------------------------------------------------------------

def test_outage_strict_rejects_fallback_allows():
    for policy, ok in ((STRICT, False), (FALLBACK, True)):
        fake = FakeLarpCheck({})
        fake.down = True
        rep = make_gate(fake, policy=policy).check("X1")
        assert rep.ok is ok and rep.category == OUTAGE
        if not ok:
            assert rep.reasons[0] == "larpcheck outage: HTTP 503 (strict policy)"


# -- 4. no API key ----------------------------------------------------------------------------------------------

def test_no_key_strict_rejects_fallback_allows():
    strict, fb = MissingLarpCheck(STRICT), MissingLarpCheck(FALLBACK)
    rep = strict.check("ANY")
    assert not rep.ok and rep.category == NO_KEY and "strict policy" in rep.reasons[0]
    assert fb.check("ANY").ok and fb.check("ANY").category == NO_KEY
    assert fb.counters["larpcheck_fallback_allowed"] == 2 and strict.counters["larpcheck_rejected"] == 1


# -- 5. exhausted credits ---------------------------------------------------------------------------------------

def test_credits_used_up_locally_or_by_the_server():
    for policy, ok in ((STRICT, False), (FALLBACK, True)):
        gate = make_gate(FakeLarpCheck({}), policy=policy, max_checks_per_day=1)
        assert gate.check("A1").category == PENDING_CHECK  # the day's one check
        rep = gate.check("B2")
        assert rep.ok is ok and rep.category == NO_CREDITS

        broke = NoCredits({})
        g = make_gate(broke, policy=policy, max_checks_per_day=4)
        rep = g.check("C3")
        assert rep.ok is ok and rep.category == NO_CREDITS and g.checks_left_today() == 0 and not broke.started

        plan = make_gate(PlanRefused({}), policy=policy).check("E5")  # lookups refused for lack of plan
        assert plan.ok is ok and plan.category == NO_CREDITS


# -- 6. pending check and timeout -------------------------------------------------------------------------------

def test_pending_holds_under_both_and_the_timeout_follows_the_policy():
    for policy, ok in ((STRICT, False), (FALLBACK, True)):
        clock = Clock()
        fake = FakeLarpCheck({})
        gate = make_gate(fake, clock=clock, policy=policy, pending_max_minutes=5)
        held = gate.check("A1")
        assert not held.ok and held.category == PENDING_CHECK and fake.started == ["A1"]
        clock.t += 60
        assert gate.check("A1").category == PENDING_CHECK  # still waiting, no second credit
        clock.t += 5 * 60
        rep = gate.check("A1")
        assert rep.ok is ok and rep.category == TIMEOUT and fake.started == ["A1"]
        assert gate.counters["larpcheck_pending"] == 2
        fake.coins["A1"] = {**FAIR, "address": "A1"}  # the check finishes later: its verdict is used
        clock.t += 200
        assert gate.check("A1").ok and gate.check("A1").category == ""


# -- the policy in force, and old config files ------------------------------------------------------------------

def test_defaults_paper_fallback_live_strict():
    cfg = LarpCheckConfig()
    assert (cfg.paper_policy, cfg.live_policy) == (FALLBACK, STRICT)
    assert policy_for(cfg, "paper") == FALLBACK and policy_for(cfg, "live") == STRICT
    with pytest.raises(ValueError, match="paper_policy"):
        Config(larpcheck=LarpCheckConfig(paper_policy="loose")).validate()


def test_live_mode_wiring_is_strict_by_default(monkeypatch, tmp_path):
    from memecoin_agent import cli
    from memecoin_agent.data.larpcheck import LarpCheckGate

    cfg = Config()
    cfg.filters.require_rugcheck = False
    cfg.jupiter.enabled = False
    cfg.larpcheck.api_key_file = str(tmp_path / "none")
    monkeypatch.delenv("LARPCHECK_API_KEY", raising=False)
    cfg.execution.mode = "live"
    assert not cli.build_safety(cfg).check("ANY").ok  # live, no key: refused
    cfg.execution.mode = "paper"
    assert cli.build_safety(cfg).check("ANY").ok  # paper, no key: the other checks decide
    monkeypatch.setenv("LARPCHECK_API_KEY", "lc_test")
    cfg.execution.mode = "live"
    assert cli.build_safety(cfg).find(LarpCheckGate).policy == STRICT
    cfg.larpcheck.live_policy = FALLBACK
    assert cli.build_safety(cfg).find(LarpCheckGate).policy == FALLBACK


def test_old_settings_still_load_and_only_tighten(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("larpcheck:\n  require_verdict: true\n  fail_closed: false\n  on_no_credits: allow\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.larpcheck.paper_policy == FALLBACK and cfg.larpcheck.live_policy == STRICT
    assert "replaced by paper_policy / live_policy" in caplog.text
    path.write_text("larpcheck:\n  on_unavailable: reject\n", encoding="utf-8")
    assert load_config(path).larpcheck.paper_policy == STRICT
    path.write_text("larpcheck:\n  fail_closed: true\n  paper_policy: fallback\n", encoding="utf-8")
    assert load_config(path).larpcheck.paper_policy == FALLBACK  # an explicit new key wins


def test_counters_count_decisions_not_cache_hits():
    gate = make_gate(FakeLarpCheck({"RIGGED1": RIGGED}), policy=STRICT)
    for _ in range(3):
        gate.check("RIGGED1")
    assert gate.counters["larpcheck_rejected"] == 1


def test_composite_reports_the_larpcheck_category():
    gate = make_gate(FakeLarpCheck({}), policy=STRICT, on_unchecked="allow")
    assert CompositeSafety(gate).check("NEW1").category == NO_VERDICT
