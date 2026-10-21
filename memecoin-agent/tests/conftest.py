from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memecoin_agent.models import TokenSnapshot

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def make_snap(**overrides) -> TokenSnapshot:
    """A snapshot that passes the default entry filters."""
    base = dict(
        mint="MINT1", symbol="DOGE2", name="Doge Two", pair_address="PAIR1", dex_id="raydium",
        quote_symbol="SOL", price_usd=0.001, liquidity_usd=80_000, market_cap_usd=600_000,
        volume_m5=8_000, volume_h1=90_000, volume_h24=500_000,
        buys_m5=60, sells_m5=30, buys_h1=600, sells_h1=350,
        price_change_m5=4.0, price_change_h1=45.0, price_change_h24=120.0,
        pair_created_at=NOW - timedelta(hours=3), url="https://dexscreener.com/solana/pair1", fetched_at=NOW,
    )
    base.update(overrides)
    return TokenSnapshot(**base)


@pytest.fixture
def snap() -> TokenSnapshot:
    return make_snap()
