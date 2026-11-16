from datetime import datetime, timezone

import httpx
import pytest

from memecoin_agent.watch.alerts import AlertSink
from memecoin_agent.watch.approvals import (
    ApprovalTarget,
    ApprovalWatcher,
    decode_bool,
    encode_address_call,
    rpc_call,
    selector,
)

FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
TOKEN_A = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
TOKEN_B = "0x1111111111111111111111111111111111111111"
TRUE = "0x" + "0" * 63 + "1"
FALSE = "0x" + "0" * 64
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class FakeChain:
    def __init__(self, approved=()):
        self.approved = set(a.lower() for a in approved)
        self.fail: set[str] = set()
        self.calls = 0

    def __call__(self, to, data):
        self.calls += 1
        token = "0x" + data[-40:]
        if token.lower() in self.fail:
            raise RuntimeError("RPC HTTP 503")
        return TRUE if token.lower() in self.approved else FALSE


def make(tmp_path, chain, tokens=(TOKEN_A,), state="state.json", alerts="alerts.csv"):
    target = ApprovalTarget("pons-factory", FACTORY, "approvedPairTokens(address)", list(tokens))
    return ApprovalWatcher(chain, [target], AlertSink(tmp_path / alerts), tmp_path / state, clock=lambda: NOW)


def test_selector_matches_known_value():
    assert selector("transfer(address,uint256)") == "0xa9059cbb"


def test_encode_address_call_pads_and_lowercases():
    data = encode_address_call(selector("approvedPairTokens(address)"), TOKEN_A)
    assert len(data) == 2 + 8 + 64
    assert data.endswith("5fc5360d0400a0fd4f2af552add042d716f1d168")
    assert data[10:74] == "0" * 24 + "5fc5360d0400a0fd4f2af552add042d716f1d168"


def test_encode_rejects_bad_address():
    with pytest.raises(ValueError):
        encode_address_call("0x00000000", "0x1234")


def test_decode_bool():
    assert decode_bool(TRUE) is True
    assert decode_bool(FALSE) is False
    with pytest.raises(ValueError):
        decode_bool("")
    with pytest.raises(ValueError):
        decode_bool(None)


def test_first_poll_is_a_baseline_with_no_alert(tmp_path):
    w = make(tmp_path, FakeChain(approved=[TOKEN_A]))
    assert w.poll_once() == []
    assert w.state == {f"{FACTORY.lower()}:{TOKEN_A.lower()}": True}


def test_flip_to_approved_then_revoked_alerts_once_each(tmp_path):
    chain = FakeChain()
    w = make(tmp_path, chain)
    assert w.poll_once() == []
    chain.approved.add(TOKEN_A.lower())
    alerts = w.poll_once()
    assert len(alerts) == 1 and alerts[0].detail.startswith("approved on pons-factory")
    assert w.poll_once() == []
    chain.approved.discard(TOKEN_A.lower())
    alerts = w.poll_once()
    assert len(alerts) == 1 and alerts[0].detail.startswith("revoked")


def test_restart_does_not_repeat_alerts(tmp_path):
    chain = FakeChain()
    w = make(tmp_path, chain)
    w.poll_once()
    chain.approved.add(TOKEN_A.lower())
    assert len(w.poll_once()) == 1
    restarted = make(tmp_path, chain)
    assert restarted.poll_once() == []


def test_alert_is_written_to_csv_with_header(tmp_path):
    chain = FakeChain()
    w = make(tmp_path, chain)
    w.poll_once()
    chain.approved.add(TOKEN_A.lower())
    w.poll_once()
    lines = (tmp_path / "alerts.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "seen_at,source,subject,detail,url"
    assert len(lines) == 2 and TOKEN_A in lines[1] and "approved" in lines[1]


def test_failed_check_is_skipped_and_not_recorded(tmp_path):
    chain = FakeChain()
    chain.fail.add(TOKEN_A.lower())
    w = make(tmp_path, chain, tokens=(TOKEN_A, TOKEN_B))
    w.poll_once()
    assert all(TOKEN_A.lower() not in k for k in w.state)
    assert any(TOKEN_B.lower() in k for k in w.state)


def test_rpc_errors_do_not_leak_the_url(monkeypatch):
    secret_url = "https://robinhood-mainnet.g.alchemy.com/v2/SECRETKEY123"

    def boom(self, url, **kwargs):
        raise httpx.ConnectError(f"failed to reach {url}")

    monkeypatch.setattr(httpx.Client, "post", boom)
    call = rpc_call(secret_url)
    with pytest.raises(RuntimeError) as exc:
        call(FACTORY, "0x")
    assert "SECRETKEY123" not in str(exc.value)
