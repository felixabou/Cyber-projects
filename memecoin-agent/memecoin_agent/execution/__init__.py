"""Order execution backends."""

from __future__ import annotations

from typing import Protocol

from ..models import Fill, Position, TokenSnapshot, is_unchecked
from ..portfolio import Portfolio


class ExecutionError(RuntimeError):
    pass


class UncheckedLiveError(ExecutionError):
    """A live executor was asked to buy a coin no safety check covers. Never allowed."""


def refuse_unchecked_live(snap: TokenSnapshot) -> None:
    """Every live buy goes through this, inside the executor, whatever the caller or the config says: unchecked coins
    and anything off Solana (the only chain with live safety checks and execution) are refused. Sells never are."""
    if is_unchecked(snap):
        raise UncheckedLiveError(f"refused: {snap.symbol} on {snap.chain} is UNCHECKED (no safety check); "
                                 "unchecked coins are paper-only")
    if str(snap.chain).lower() != "solana":
        raise UncheckedLiveError(f"refused: live trading is Solana-only ({snap.symbol} is on {snap.chain})")


class Executor(Protocol):
    def preflight(self, snap: TokenSnapshot, usd: float) -> str | None:
        """Return a reason to skip this buy, or None if it looks executable."""

    def available_usd(self, pf: Portfolio) -> float: ...

    def sync_cash(self, pf: Portfolio) -> None:
        """Refresh the ledger's cash from the real wallet (live mode only)."""

    def buy(self, snap: TokenSnapshot, usd: float) -> Fill: ...

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill: ...
