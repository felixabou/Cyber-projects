"""Paper-trading executor: simulated fills with fees, constant-product price impact and the drift a
real order suffers while it lands."""

from __future__ import annotations

from ..config import ExecutionConfig
from ..models import Fill, Position, TokenSnapshot
from ..portfolio import Portfolio
from . import ExecutionError

MAX_IMPACT = 0.5
MAX_DRIFT = 0.15


class PaperExecutor:
    def __init__(self, cfg: ExecutionConfig) -> None:
        self.cfg = cfg

    def _impact(self, usd: float, liquidity_usd: float) -> float:
        if not self.cfg.paper_price_impact or liquidity_usd <= 0:
            return 0.0
        # In an x*y=k pool, liquidity_usd is ~2x the quote reserve, so trading `usd`
        # moves price by roughly usd / (liquidity_usd / 2).
        return min(MAX_IMPACT, 2 * usd / liquidity_usd)

    def _drift(self, snap: TokenSnapshot | None) -> float:
        """How far the price moves, against us, during the fill delay: the coin's 5-minute move scaled
        to paper_latency_s."""
        if snap is None or self.cfg.paper_latency_s <= 0:
            return 0.0
        return min(MAX_DRIFT, abs(snap.price_change_m5) / 100 * self.cfg.paper_latency_s / 300)

    def preflight(self, snap: TokenSnapshot, usd: float) -> str | None:
        return None

    def available_usd(self, pf: Portfolio) -> float:
        return max(0.0, pf.cash_usd)

    def sync_cash(self, pf: Portfolio) -> None:
        pass

    def buy(self, snap: TokenSnapshot, usd: float) -> Fill:
        if snap.price_usd <= 0:
            raise ExecutionError(f"No price for {snap.symbol}")
        fee = usd * self.cfg.paper_fee_pct / 100
        price = snap.price_usd * (1 + self._impact(usd, snap.liquidity_usd) + self._drift(snap))
        tokens = (usd - fee) / price
        # Report the effective all-in price so PnL and stops are measured from what we paid.
        return Fill(token_amount=tokens, usd_value=usd, price_usd=usd / tokens, fee_usd=fee)

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill:
        price = snap.price_usd if snap else pos.last_price_usd
        liquidity = snap.liquidity_usd if snap else pos.last_liquidity_usd
        tokens = pos.token_amount if fraction >= 0.999 else pos.token_amount * fraction
        gross = tokens * price
        gross *= max(0.0, 1 - self._impact(gross, liquidity) - self._drift(snap))
        fee = gross * self.cfg.paper_fee_pct / 100
        net = gross - fee
        return Fill(token_amount=tokens, usd_value=net, price_usd=net / tokens if tokens else 0.0, fee_usd=fee)
