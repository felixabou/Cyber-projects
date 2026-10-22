"""Portfolio-level risk limits: sizing, exposure caps, daily loss halt, cooldowns."""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import RiskConfig
from .portfolio import Portfolio


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    def start_day(self, pf: Portfolio, now: datetime) -> None:
        day = now.date().isoformat()
        if day not in pf.day_start_equity:
            pf.day_start_equity[day] = pf.equity_usd()

    def daily_loss_halted(self, pf: Portfolio, now: datetime) -> bool:
        day = now.date().isoformat()
        start = pf.day_start_equity.get(day) or pf.equity_usd()
        realized = pf.realized_pnl_by_day.get(day, 0.0)
        # Count open losers too, so a pile of underwater bags also stops new entries.
        unrealized = sum(min(0.0, p.market_value_usd() - p.cost_basis_usd) for p in pf.positions.values())
        return start > 0 and (realized + unrealized) <= -start * self.cfg.max_daily_loss_pct / 100

    def entry_block_reason(self, pf: Portfolio, now: datetime) -> str | None:
        if len(pf.active_positions()) >= self.cfg.max_open_positions:
            return f"max open positions ({self.cfg.max_open_positions})"
        if self.daily_loss_halted(pf, now):
            return f"daily loss limit ({self.cfg.max_daily_loss_pct:.0f}%) hit"
        hour_ago = now - timedelta(hours=1)
        recent = sum(1 for t in pf.entry_times if datetime.fromisoformat(t) >= hour_ago)
        if recent >= self.cfg.max_entries_per_hour:
            return f"max entries per hour ({self.cfg.max_entries_per_hour})"
        return None

    def token_blocked(self, pf: Portfolio, mint: str, now: datetime) -> bool:
        if mint in pf.positions:
            return True
        until = pf.cooldowns.get(mint)
        return until is not None and datetime.fromisoformat(until) > now

    def cooldown_until(self, now: datetime) -> datetime:
        return now + timedelta(minutes=self.cfg.token_cooldown_minutes)

    def position_size_usd(self, pf: Portfolio, available_usd: float) -> float:
        """Size = % of equity, capped by max size and available cash. 0 means skip."""
        size = min(pf.equity_usd() * self.cfg.position_size_pct / 100, self.cfg.max_position_usd, available_usd)
        return round(size, 2) if size >= self.cfg.min_position_usd else 0.0

    def prune(self, pf: Portfolio, now: datetime) -> None:
        pf.cooldowns = {m: t for m, t in pf.cooldowns.items() if datetime.fromisoformat(t) > now}
        day_ago = now - timedelta(days=1)
        pf.entry_times = [t for t in pf.entry_times if datetime.fromisoformat(t) >= day_ago]
