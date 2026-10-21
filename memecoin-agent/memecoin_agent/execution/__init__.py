"""Order execution backends."""

from __future__ import annotations

from typing import Protocol

from ..models import Fill, Position, TokenSnapshot
from ..portfolio import Portfolio


class ExecutionError(RuntimeError):
    pass


class Executor(Protocol):
    def preflight(self, snap: TokenSnapshot, usd: float) -> str | None:
        """Return a reason to skip this buy, or None if it looks executable."""

    def available_usd(self, pf: Portfolio) -> float: ...

    def sync_cash(self, pf: Portfolio) -> None:
        """Refresh the ledger's cash from the real wallet (live mode only)."""

    def buy(self, snap: TokenSnapshot, usd: float) -> Fill: ...

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill: ...
