"""Market data and token-safety sources."""

from __future__ import annotations

from typing import Iterable, Protocol

from ..models import SafetyReport, TokenSnapshot


class MarketData(Protocol):
    def discover(self) -> list[str]:
        """Return candidate token mints worth evaluating."""

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        """Return the best-pair snapshot for each mint that has one."""

    def sol_price_usd(self) -> float:
        """Current SOL/USD price."""


class SafetyChecker(Protocol):
    def check(self, mint: str) -> SafetyReport: ...
