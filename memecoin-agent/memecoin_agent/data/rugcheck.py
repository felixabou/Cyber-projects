"""RugCheck.xyz token report client.

RugCheck flags the usual Solana rug vectors: live mint/freeze authority,
unlocked LP, concentrated holders, etc. Reports are cached because token
risk changes slowly relative to the scan interval.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from ..models import SafetyReport

log = logging.getLogger(__name__)

BASE_URL = "https://api.rugcheck.xyz/v1"
UNAVAILABLE = "rugcheck unavailable"


class RugCheckClient:
    def __init__(
        self,
        max_score: float = 50.0,
        reject_danger: bool = True,
        min_lp_locked_pct: float | None = None,
        fail_closed: bool = True,
        cache_ttl_s: float = 1800.0,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_score = max_score
        self.reject_danger = reject_danger
        self.min_lp_locked_pct = min_lp_locked_pct
        self.fail_closed = fail_closed
        self.cache_ttl_s = cache_ttl_s
        self.http = client or httpx.Client(timeout=15.0)
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self._cache: dict[str, tuple[float, SafetyReport]] = {}

    def evaluate(self, report: dict[str, Any]) -> SafetyReport:
        reasons: list[str] = []
        score = report.get("score_normalised")
        if score is not None and float(score) > self.max_score:
            reasons.append(f"rugcheck score {float(score):.0f} > {self.max_score:.0f}")
        if self.reject_danger:
            for risk in report.get("risks") or []:
                if str(risk.get("level", "")).lower() == "danger":
                    reasons.append(f"rugcheck danger: {risk.get('name', 'unknown')}")
        if self.min_lp_locked_pct is not None:
            locked = report.get("lpLockedPct")
            if locked is None or float(locked) < self.min_lp_locked_pct:
                reasons.append(f"LP locked {locked}% < {self.min_lp_locked_pct}%")
        return SafetyReport(ok=not reasons, score=float(score) if score is not None else None, reasons=reasons)

    def check(self, mint: str) -> SafetyReport:
        cached = self._cache.get(mint)
        if cached and self.clock() - cached[0] < self.cache_ttl_s:
            return cached[1]
        try:
            resp = self.http.get(f"{self.base_url}/tokens/{mint}/report/summary")
            resp.raise_for_status()
            result = self.evaluate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("RugCheck lookup failed for %s: %s", mint, exc)
            # Don't cache failures, so the next scan retries.
            return SafetyReport(ok=not self.fail_closed, score=None, reasons=[UNAVAILABLE])
        self._cache[mint] = (self.clock(), result)
        return result


class NoSafetyCheck:
    """Used when `filters.require_rugcheck` is false."""

    def check(self, mint: str) -> SafetyReport:
        return SafetyReport(ok=True, score=None, reasons=[])
