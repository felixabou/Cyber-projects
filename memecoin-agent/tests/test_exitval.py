"""Out-of-sample exit validation: deterministic synthetic candles with known outcomes."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from memecoin_agent.config import ExitConfig
from memecoin_agent.exitlab import Entry
from memecoin_agent.exitval import (
    SimParams,
    TradeResult,
    chrono_split,
    default_strategies,
    fixed_stop_take,
    hold,
    metrics,
    simulate,
    validate,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
KEEP = (1 - 0.005) * (1 - 0.01)  # 0.5% fee and 1% slippage on the sale


def entry(t=T0, price=1.0, cost=10.0, mint="M1"):
    return Entry(mint, mint, t, price, cost, "momentum")


def flat(prices, start=T0, volume=1_000_000.0):
    """One flat candle per minute after `start`: [ts, open, high, low, close, volume]."""
    t0 = start.timestamp()
    return [[t0 + 60 * (i + 1), p, p, p, p, volume] for i, p in enumerate(prices)]


def run(prices, factory, params=None, e=None, volume=1_000_000.0):
    e = e or entry()
    return simulate(e, flat(prices, e.time, volume), factory(e), params or SimParams(), random.Random(1))


def test_fixed_take_profit_sells_at_the_trigger_price():
    r = run([1.0] * 5 + [2.0, 3.0], fixed_stop_take(30, 100))
    assert r.ret_pct == pytest.approx((2.0 * KEEP - 1) * 100) and r.exits == 1


def test_fixed_stop_and_buy_and_hold():
    assert run([1.0, 0.8, 0.7, 0.5], fixed_stop_take(30, 100)).ret_pct == pytest.approx((0.7 * KEEP - 1) * 100)
    r = run([1.0, 1.2, 0.9, 1.5], hold())
    assert r.ret_pct == pytest.approx((1.5 * KEEP - 1) * 100) and r.exits == 0
    assert r.hours_held == pytest.approx((4 * 60 + 45) / 3600)


def test_entry_latency_buys_at_the_later_price_with_slippage_and_fee():
    p = SimParams(entry_latency_s=120)
    r = run([1.0, 1.2, 1.2, 1.5], hold(), p)
    tokens = 10 * 0.995 / (1.2 * 1.01)
    assert r.ret_pct == pytest.approx((tokens * 1.5 * KEEP / 10 - 1) * 100)


def test_exit_latency_fills_at_the_price_after_the_delay():
    r = run([1.0, 2.0, 2.4, 2.4], fixed_stop_take(30, 100), SimParams(exit_latency_s=60))
    assert r.ret_pct == pytest.approx((2.4 * KEEP - 1) * 100)


def test_volume_cap_spreads_a_sale_over_candles_and_the_rest_fills_lower():
    # 10 tokens at 2.0 is $20; 20% of a $5 candle is $1 a candle. Ten candles at 2.0 sell 5 tokens ($10),
    # then the price halves and the other 5 tokens sell at 1.0 ($5).
    prices = [1.0, 2.0] + [2.0] * 9 + [1.0] * 10
    r = run(prices, fixed_stop_take(30, 100), SimParams(max_volume_share=0.2), volume=5.0)
    assert r.ret_pct == pytest.approx((15 * KEEP / 10 - 1) * 100)
    assert r.exits == 1  # one decision, many partial fills
    unlimited = run(prices, fixed_stop_take(30, 100))
    assert unlimited.ret_pct > r.ret_pct and unlimited.hours_held < r.hours_held


def test_failed_exits_retry_and_never_fill_at_a_100_percent_failure_rate():
    r = run([1.0, 2.0, 2.0, 1.5], fixed_stop_take(30, 100), SimParams(fail_rate=1.0))
    assert r.exits == 0 and r.failed_exits >= 2
    assert r.ret_pct == pytest.approx((1.5 * KEEP - 1) * 100)  # still held at the end: sold at the last price


def test_missing_entry_fill_is_counted_not_guessed():
    r = run([1.0, 1.1], hold(), SimParams(entry_latency_s=3600))
    assert r.ret_pct is None
    assert metrics([r]) == {"n": 0, "missed": 1}


def test_horizon_closes_what_is_left():
    prices = [1.0] * 30 + [5.0]
    r = run(prices, hold(), SimParams(horizon_hours=0.25))  # 15 minutes: the 5x candle comes later
    assert r.ret_pct == pytest.approx((1.0 * KEEP - 1) * 100)


def test_current_exit_rules_run_through_the_simulator():
    e = entry()
    factory = default_strategies(ExitConfig(), 0.5)["current exits"]
    r = simulate(e, flat([1.0, 0.95, 0.6, 0.5], e.time), factory(e), SimParams(), random.Random(1))
    assert r.exits >= 1 and r.ret_pct < -30  # the 30% stop fired on the way down


def test_chronological_split_with_purge_has_no_overlap():
    es = [entry(T0 + timedelta(hours=i), mint=f"E{i}") for i in range(10)]
    train, val, holdout = chrono_split(es, (0.6, 0.2, 0.2), horizon_hours=2)
    assert [e.mint for e in train.entries] == ["E0", "E1", "E2", "E3", "E4"] and train.purged == 1
    assert [e.mint for e in val.entries] == ["E6"] and val.purged == 1
    assert [e.mint for e in holdout.entries] == ["E8", "E9"]
    assert train.in_sample and not val.in_sample and not holdout.in_sample
    for cur, nxt in ((train, val), (val, holdout)):
        assert all(e.time + timedelta(hours=2) <= nxt.entries[0].time for e in cur.entries)
    with pytest.raises(ValueError):
        chrono_split(es, (1.0,))


def test_metrics_on_known_returns():
    rs = [TradeResult(entry(T0 + timedelta(hours=i)), r, hours_held=2.0) for i, r in enumerate([10, -20, 30, -10])]
    m = metrics(rs)
    assert m["n"] == 4 and m["net_pct"] == pytest.approx(2.5) and m["win_rate"] == 0.5
    assert m["median"] == pytest.approx(0) and m["average"] == pytest.approx(2.5)
    assert m["profit_factor"] == pytest.approx(40 / 30) and m["worst"] == -20
    assert m["max_drawdown"] == pytest.approx(-20)  # cumulative 10, -10, 20, 10: from 10 down to -10
    assert m["exposure_hours"] == 2.0 and m["ci90"][0] <= 2.5 <= m["ci90"][1]


def test_report_labels_periods_warns_on_small_samples_and_picks_on_train_only():
    es, candles = [], {}
    for i in range(24):
        e = entry(T0 + timedelta(hours=3 * i), mint=f"E{i}")
        es.append(e)
        up = i % 2 == 0
        candles[e.mint] = flat([1.0, 1.5, 2.1, 2.5, 2.6, 2.7] if up else [1.0, 0.8, 0.6, 0.4, 0.4, 0.3], e.time)
    text, table = validate(es, lambda e: candles[e.mint], default_strategies(ExitConfig(), 0.5),
                           SimParams(horizon_hours=2), (0.5, 0.25, 0.25))
    assert "(IN-SAMPLE)" in text and "(OUT-OF-SAMPLE)" in text
    assert "[train: IN-SAMPLE: tuning ground, not evidence]" in text and "[validation: OUT-OF-SAMPLE]" in text
    assert "too few to judge" in text or "small sample" in text
    assert "Picked on the training period only" in text and "has shown nothing" in text
    assert set(table) == {"train", "validation", "holdout"}
    assert table["train"]["buy and hold to the horizon"]["n"] == 12


def test_a_shorter_purge_keeps_more_trades_and_says_so():
    es = [entry(T0 + timedelta(hours=i), mint=f"E{i}") for i in range(20)]
    candles = {e.mint: flat([1.0, 1.1, 1.2, 1.3, 1.4, 1.5], e.time) for e in es}
    strict, _ = validate(es, lambda e: candles[e.mint], {"hold": hold()}, SimParams(horizon_hours=6))
    loose, _ = validate(es, lambda e: candles[e.mint], {"hold": hold()}, SimParams(horizon_hours=6), purge_hours=1)
    assert "purged" in strict and "purge window 1h is shorter than the 6h horizon" in loose
