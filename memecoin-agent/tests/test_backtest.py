from __future__ import annotations

import gzip
from datetime import timedelta

import pytest

from memecoin_agent.backtest import ReplayMarket, ReplaySafety, format_report, run_backtest
from memecoin_agent.config import Config, apply_override
from memecoin_agent.data.wallets import WalletScan
from memecoin_agent.models import SafetyReport
from memecoin_agent.recording import Recorder, RecordingWriter, read_recordings
from memecoin_agent.simulation import SimClock, SyntheticMarket, SyntheticSafety

from .conftest import NOW, make_snap


def snap_rec(t, **kw):
    return {"type": "snap", **make_snap(fetched_at=t, **kw).to_dict()}


def disc(t, mints):
    return {"type": "discover", "t": t.isoformat(), "mints": mints}


def safe(t, mint, ok=True, chain="solana"):
    return {"type": "safety", "t": t.isoformat(), "chain": chain, "mint": mint, "ok": ok,
            "score": 5, "reasons": [] if ok else ["x"]}


# -- storage ----------------------------------------------------------------------------
def test_writer_reader_roundtrip_survives_truncated_tail(tmp_path):
    w = RecordingWriter(tmp_path)
    w.write(NOW, [disc(NOW, ["A"]), snap_rec(NOW, mint="A")])
    later = NOW + timedelta(hours=1)
    w.write(later, [snap_rec(later, mint="A", price_usd=0.002)])
    files = sorted(tmp_path.glob("*.gz"))
    assert len(files) == 2  # hourly rotation

    # Simulate a crash mid-write: append half a gzip member to the last file.
    member = gzip.compress(b'{"type":"snap"}\n')
    with files[-1].open("ab") as fh:
        fh.write(member[: len(member) // 2])
    recs = list(read_recordings(tmp_path))
    assert [r["type"] for r in recs] == ["discover", "snap", "snap"]

    with pytest.raises(FileNotFoundError):
        list(read_recordings(tmp_path / "missing"))


# -- replay semantics ---------------------------------------------------------------------
def test_replay_has_no_lookahead_and_expires_stale_data():
    t1, t2 = NOW + timedelta(seconds=30), NOW + timedelta(seconds=60)
    m = ReplayMarket([disc(NOW, ["A"]), snap_rec(NOW, mint="A"), snap_rec(t1, mint="A", price_usd=0.002),
                      safe(t2, "A")], stale_after_s=90)
    m.advance_to(NOW + timedelta(seconds=10))
    assert m.discover() == ["A"]
    assert m.snapshots(["A"])["A"].price_usd == 0.001  # t1 data not visible yet
    assert ReplaySafety(m).check("A").ok is False  # verdict recorded later; fail closed until then
    assert ReplaySafety(m, assume_safe=True).check("A").ok

    m.advance_to(t2)
    assert m.snapshots(["A"])["A"].price_usd == 0.002
    assert ReplaySafety(m).check("A").ok
    assert m.exhausted

    m.advance_to(t1 + timedelta(seconds=91))
    assert m.snapshots(["A"]) == {}  # token stopped being sampled -> looks unavailable


def test_replay_tracks_pregraduation_marker_until_migration():
    t1, t2 = NOW, NOW + timedelta(seconds=30)
    records = [
        {**snap_rec(t1, mint="A"), "pregraduation": True},
        {**snap_rec(t2, mint="A"), "pregraduation": False},
    ]
    market = ReplayMarket(records)
    market.advance_to(t1)
    assert market.is_pregraduation("A")
    market.advance_to(t2)
    assert not market.is_pregraduation("A")


def test_sniper_replay_requires_a_recorded_wallet_scan(tmp_path):
    cfg = Config()
    cfg.sniper.enabled = True
    cfg.feeds.pumpportal_new_tokens = True
    cfg.agent.tick_seconds = 5
    cfg.agent.scan_every_ticks = 1
    snap = make_snap(mint="CURVE", pair_created_at=NOW - timedelta(minutes=1),
                     liquidity_usd=10_000, market_cap_usd=20_000, volume_h1=5_000,
                     buys_h1=25, sells_h1=10)

    class Market:
        def discover(self):
            return ["CURVE"]

        def snapshots(self, mints):
            return {"CURVE": snap}

        def is_pregraduation(self, mint):
            return mint == "CURVE"

    class Safety:
        def check(self, mint):
            return SafetyReport(True, 1, [])

    class Wallet:
        def check(self, mint):
            return SafetyReport(True, 0, [])

        def result(self, mint):
            return WalletScan(mint=mint, scanned_at=0, bundle_bought_pct=5, top10_pct=20)

    Recorder(cfg, Market(), Safety(), RecordingWriter(tmp_path), clock=SimClock(NOW),
             wallet_scan=Wallet()).tick()
    records = list(read_recordings(tmp_path))
    wallet = next(r for r in records if r["type"] == "wallet")
    assert wallet["mint"] == "CURVE" and wallet["scan"]["top10_pct"] == 20
    assert run_backtest(cfg, records).trades[0].side == "buy"
    assert run_backtest(cfg, [r for r in records if r["type"] != "wallet"]).trades == []
    late_wallet = [{**r, "t": (NOW + timedelta(seconds=5)).isoformat()}
                   if r["type"] == "wallet" else r for r in records]
    assert run_backtest(cfg, late_wallet).trades[0].time == NOW + timedelta(seconds=5)


def test_recorder_captures_lowcap_wallet_scans_for_replay(tmp_path):
    cfg = Config()
    cfg.lowcap.confirm_seconds = 0
    cfg.sniper.enabled = False
    low = make_snap(mint="LOW", market_cap_usd=80_000, liquidity_usd=20_000, volume_h1=30_000,
                    pair_created_at=NOW - timedelta(minutes=45))

    class Market:
        def discover(self):
            return ["LOW"]

        def snapshots(self, mints):
            return {"LOW": low}

        def is_pregraduation(self, mint):
            return False

    class Safety:
        def check(self, mint):
            return SafetyReport(True, 1.0, [])

    class Wallet:
        def check(self, mint):
            return SafetyReport(True, 0.0, [])

        def result(self, mint):
            return WalletScan(mint=mint, scanned_at=0.0, bundle_bought_pct=5.0, top10_pct=20.0)

    Recorder(cfg, Market(), Safety(), RecordingWriter(tmp_path), clock=SimClock(NOW), wallet_scan=Wallet()).tick()
    records = list(read_recordings(tmp_path))
    assert any(r["type"] == "wallet" and r["mint"] == "LOW" for r in records)
    res = run_backtest(cfg, records)
    assert [t.side for t in res.trades] == ["buy", "sell"]


def test_lowcap_bsc_replay_uses_chain_snapshot_and_safety(tmp_path):
    cfg = Config()
    cfg.lowcap.confirm_seconds = 0
    address = "0x" + "ab" * 20
    records = [disc(NOW, [address]), snap_rec(NOW, mint=address, chain="bsc", quote_symbol="WBNB",
                                                market_cap_usd=80_000, liquidity_usd=20_000,
                                                volume_h1=30_000, pair_created_at=NOW - timedelta(minutes=45)),
               safe(NOW, address, chain="bsc")]
    res = run_backtest(cfg, records)
    assert [t.side for t in res.trades] == ["buy", "sell"]
    assert res.closed[0].mint == address and res.closed[0].exit_reason == "end of backtest"


def test_recorder_records_non_solana_safety_for_replay(tmp_path):
    cfg = Config()
    cfg.lowcap.confirm_seconds = 0
    address = "0x" + "ef" * 20
    snap = make_snap(mint=address, chain="bsc", quote_symbol="WBNB", market_cap_usd=80_000,
                     liquidity_usd=20_000, volume_h1=30_000, pair_created_at=NOW - timedelta(minutes=45))

    class Market:
        def discover(self):
            return [address]

        def snapshots(self, mints):
            return {address: snap}

    class SolanaSafety:
        def check(self, mint):
            raise AssertionError("EVM snapshot was sent to Solana safety")

    class ChainSafety:
        def __init__(self):
            self.calls = []

        def check(self, chain, mint):
            self.calls.append((chain, mint))
            return SafetyReport(True, 1.0, [])

    chain_safety = ChainSafety()
    Recorder(cfg, Market(), SolanaSafety(), RecordingWriter(tmp_path), clock=SimClock(NOW),
             chain_safety=chain_safety).tick()
    records = list(read_recordings(tmp_path))
    safety = next(r for r in records if r["type"] == "safety")
    assert safety["chain"] == "bsc" and chain_safety.calls == [("bsc", address)]
    assert [t.side for t in run_backtest(cfg, records).trades] == ["buy", "sell"]


def test_backtest_trades_only_on_information_available_at_the_time():
    cfg = Config()
    cfg.agent.tick_seconds = 15
    drop_at = NOW + timedelta(minutes=10)
    records = [disc(NOW, ["GOOD"]), safe(NOW, "GOOD")]
    t = NOW
    while t < NOW + timedelta(minutes=20):
        price = 0.001 if t < drop_at else 0.0004  # -60%: past the 50% hard stop
        records.append(snap_rec(t, mint="GOOD", price_usd=price))
        t += timedelta(seconds=30)

    res = run_backtest(cfg, records)
    buys = [x for x in res.trades if x.side == "buy"]
    sells = [x for x in res.trades if x.side == "sell"]
    assert len(buys) == 1 and buys[0].time == NOW
    assert len(sells) == 1 and "stop loss" in sells[0].reason
    assert drop_at <= sells[0].time < drop_at + timedelta(seconds=30)
    assert res.stats()["round_trips"] == 1 and res.stats()["win_rate_pct"] == 0
    assert "stop loss: 1" in format_report(res)


def test_unsafe_verdicts_block_entries():
    records = [disc(NOW, ["BAD"]), safe(NOW, "BAD", ok=False), snap_rec(NOW, mint="BAD"),
               snap_rec(NOW + timedelta(minutes=1), mint="BAD")]
    assert run_backtest(Config(), records).trades == []
    # No verdict recorded at all: rejected unless --assume-safe.
    records = [r for r in records if r["type"] != "safety"]
    assert run_backtest(Config(), records).trades == []
    assert run_backtest(Config(), records, assume_safe=True).trades


# -- recorder -----------------------------------------------------------------------------
def test_recorder_tracking_rules(tmp_path):
    class Market:
        def __init__(self):
            self.mints = ["A", "B"]

        def discover(self):
            return list(self.mints)

        def snapshots(self, mints):
            return {m: make_snap(mint=m, liquidity_usd=1_000 if m == "B" else 80_000) for m in mints}

        def is_pregraduation(self, mint):
            return mint == "A"

    class Safety:
        calls = []

        def check(self, mint):
            self.calls.append(mint)
            return SafetyReport(True, 1.0, [])

    cfg = Config()
    cfg.recorder.track_hours = 1
    cfg.recorder.max_tracked = 2
    clock = SimClock(NOW)
    market, safety = Market(), Safety()
    rec = Recorder(cfg, market, safety, RecordingWriter(tmp_path), clock=clock)
    rec.tick()
    assert set(rec.tracked) == {"A", "B"}
    snaps = [r for r in read_recordings(tmp_path) if r["type"] == "snap"]
    assert all(r["pregraduation"] is (r["mint"] == "A") for r in snaps)
    assert safety.calls == ["A"]  # B is far below any plausible liquidity filter

    market.mints = ["C", "A", "B"]
    clock.advance(60)
    rec.tick()
    rec.tick()
    assert "C" in rec.tracked and len(rec.tracked) == 2  # capped, newest kept
    assert safety.calls == ["A", "C"]  # one verdict per token

    clock.advance(2 * 3600)
    rec.tick()
    assert rec.tracked == {}  # expired, and not re-tracked when rediscovered
    types = [r["type"] for r in read_recordings(tmp_path)]
    assert types.count("discover") == 2 and types.count("safety") == 2


def test_synthetic_record_then_backtest_books_balance(tmp_path):
    cfg = Config()
    clock = SimClock()
    market = SyntheticMarket(clock, seed=5)

    def advance(_):
        clock.advance(cfg.recorder.interval_s)
        market.step(cfg.recorder.interval_s)

    Recorder(cfg, market, SyntheticSafety(market), RecordingWriter(tmp_path), clock=clock, sleep=advance).run(hours=4)
    res = run_backtest(Config(), read_recordings(tmp_path))
    pf = res.portfolio
    assert res.closed and not pf.positions
    buys = sum(t.usd_value for t in res.trades if t.side == "buy")
    sells = sum(t.usd_value for t in res.trades if t.side == "sell")
    assert pf.cash_usd == pytest.approx(1000 - buys + sells)
    assert sum(c.pnl_usd for c in res.closed) == pytest.approx(pf.total_realized_pnl())
    assert res.final_equity == pytest.approx(1000 + pf.total_realized_pnl())
    assert 0 <= res.max_drawdown_pct() < 100


def test_apply_override_validation():
    cfg = Config()
    apply_override(cfg, "filters.min_score=0.7")
    apply_override(cfg, "exits.take_profit_levels=[{at_pct: 25, sell_fraction: 1.0}]")
    assert cfg.filters.min_score == 0.7 and cfg.exits.take_profit_levels[0].at_pct == 25
    for bad in ("filters.min_scroe=1", "nosection.x=1", "filters", "exits.stop_loss_pct=150"):
        with pytest.raises(ValueError):
            apply_override(cfg, bad)


