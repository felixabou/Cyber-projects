from __future__ import annotations

from datetime import timedelta

import pytest

from memecoin_agent.config import ExitConfig, FilterConfig, RiskConfig, load_config
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.config import ExecutionConfig
from memecoin_agent.models import Fill
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.risk import RiskManager
from memecoin_agent.strategy import evaluate_entry, evaluate_exit, momentum_score

from .conftest import NOW, make_snap


# -- entry ---------------------------------------------------------------------------
def test_good_token_passes(snap):
    ev = evaluate_entry(snap, FilterConfig(), NOW)
    assert ev.passed, ev.reasons
    assert 0.55 <= ev.score <= 1


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"liquidity_usd": 5_000}, "liquidity"),
        ({"pair_created_at": NOW - timedelta(minutes=3)}, "age"),
        ({"pair_created_at": None}, "unknown pair age"),
        ({"price_change_h1": 400.0}, "too extended"),
        ({"sells_h1": 900}, "b/s1h"),
        ({"quote_symbol": "BONK"}, "quote"),
        ({"market_cap_usd": 50_000_000}, "mcap"),
    ],
)
def test_filters_reject(overrides, expected):
    ev = evaluate_entry(make_snap(**overrides), FilterConfig(), NOW)
    assert not ev.passed
    assert any(expected in r for r in ev.reasons), ev.reasons


def test_blacklist(snap):
    assert not evaluate_entry(snap, FilterConfig(blacklist=[snap.mint]), NOW).passed


def test_score_tapers_for_parabolic_moves():
    assert momentum_score(make_snap(price_change_h1=60)) > momentum_score(make_snap(price_change_h1=240))


# -- exits ---------------------------------------------------------------------------
def _position(pf: Portfolio, snap, price=0.001):
    fill = Fill(token_amount=100_000, usd_value=100.0, price_usd=price)
    return pf.open(snap, fill, NOW, 0.8)


def test_exit_rules(snap):
    e = ExitConfig(style="classic")
    pos = _position(Portfolio(cash_usd=1000), snap)
    later = NOW + timedelta(minutes=10)

    assert evaluate_exit(pos, make_snap(price_usd=0.00105), e, later) is None
    assert "stop loss" in evaluate_exit(pos, make_snap(price_usd=0.0007), e, later).reason
    assert "liquidity pulled" in evaluate_exit(pos, make_snap(liquidity_usd=20_000), e, later).reason

    tp = evaluate_exit(pos, make_snap(price_usd=0.00155), e, later)
    assert tp.tp_level == 0 and tp.fraction == pytest.approx(0.4)
    pos.tp_levels_hit.append(0)
    assert evaluate_exit(pos, make_snap(price_usd=0.00155), e, later) is None

    pos.peak_price_usd = 0.002  # +100% peak, now 25% off it
    trail = evaluate_exit(pos, make_snap(price_usd=0.0015), e, later)
    assert "trailing" in trail.reason and trail.fraction == 1.0

    pos.peak_price_usd = 0.0011
    assert evaluate_exit(pos, make_snap(price_usd=0.0011), e, NOW + timedelta(hours=5)) is None  # 12h hold
    assert "time stop" in evaluate_exit(pos, make_snap(price_usd=0.0011), e, NOW + timedelta(hours=12)).reason
    # Without market data only the time stop can fire.
    assert evaluate_exit(pos, None, e, later) is None
    assert evaluate_exit(pos, None, e, NOW + timedelta(hours=12)).fraction == 1.0


# -- portfolio & risk -------------------------------------------------------------------
def test_partial_then_full_sell_accounting(snap):
    pf = Portfolio(cash_usd=1000)
    _position(pf, snap)
    assert pf.cash_usd == 900
    r1 = pf.reduce(snap.mint, 0.5, Fill(50_000, 80.0, 0.0016), NOW, "tp")
    assert r1 == pytest.approx(30.0)
    assert pf.positions[snap.mint].cost_basis_usd == pytest.approx(50.0)
    r2 = pf.reduce(snap.mint, 1.0, Fill(50_000, 40.0, 0.0008), NOW, "stop", cooldown_until=NOW + timedelta(hours=1))
    assert r2 == pytest.approx(-10.0)
    assert snap.mint not in pf.positions
    assert pf.cash_usd == pytest.approx(1020.0)
    assert pf.total_realized_pnl() == pytest.approx(20.0)
    assert snap.mint in pf.cooldowns


def test_state_roundtrip(tmp_path, snap):
    pf = Portfolio(cash_usd=1000)
    _position(pf, snap)
    pf.save(tmp_path / "s.json")
    pf.flush_trades(tmp_path / "t.csv")
    loaded = Portfolio.load(tmp_path / "s.json", 0)
    assert loaded.cash_usd == 900
    assert loaded.positions[snap.mint].opened_at == NOW
    assert "buy" in (tmp_path / "t.csv").read_text()


def test_risk_limits(snap):
    rm = RiskManager(RiskConfig(max_open_positions=1, max_daily_loss_pct=10))
    pf = Portfolio(cash_usd=1000)
    rm.start_day(pf, NOW)
    assert rm.entry_block_reason(pf, NOW) is None
    assert rm.position_size_usd(pf, pf.cash_usd) == 50.0  # 5% of equity
    assert rm.position_size_usd(pf, 5.0) == 0.0  # below min size

    _position(pf, snap)
    assert "max open" in rm.entry_block_reason(pf, NOW)
    assert rm.token_blocked(pf, snap.mint, NOW)

    pf.reduce(snap.mint, 1.0, Fill(100_000, 0.0, 0.0), NOW, "rug", cooldown_until=rm.cooldown_until(NOW))
    assert "daily loss" in rm.entry_block_reason(pf, NOW)
    assert rm.token_blocked(pf, snap.mint, NOW + timedelta(minutes=10))
    assert not rm.token_blocked(pf, snap.mint, NOW + timedelta(days=1))


def test_paper_fills_cost_money(snap):
    ex = PaperExecutor(ExecutionConfig(paper_fee_pct=1.0))
    fill = ex.buy(snap, 100.0)
    assert fill.price_usd > snap.price_usd  # fee + impact make the effective price worse
    pf = Portfolio(cash_usd=1000)
    pos = pf.open(snap, fill, NOW, 0.8)
    out = ex.sell(pos, 1.0, snap)
    assert out.usd_value < 100.0  # immediate round trip loses money


# -- config -------------------------------------------------------------------------
def test_config_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  max_open_positions: 2\nexits:\n  take_profit_levels:\n    - {at_pct: 30, sell_fraction: 1.0}\n")
    cfg = load_config(p)
    assert cfg.risk.max_open_positions == 2
    assert cfg.exits.take_profit_levels[0].at_pct == 30
    assert cfg.filters.min_liquidity_usd == FilterConfig().min_liquidity_usd

    p.write_text("risk:\n  max_open_postions: 2\n")
    with pytest.raises(ValueError, match="max_open_postions"):
        load_config(p)
    p.write_text("execution:\n  mode: yolo\n")
    with pytest.raises(ValueError, match="mode"):
        load_config(p)


def test_example_config_loads():
    from pathlib import Path

    load_config(Path(__file__).parent.parent / "config.example.yaml")
