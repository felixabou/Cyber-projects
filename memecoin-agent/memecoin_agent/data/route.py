"""Honeypot check for paper buys: can it be sold back?

Asks Jupiter to quote the buy, then to quote selling exactly that many tokens straight back to SOL.
Quotes only (no wallet, nothing signed or sent). No sell route, or a round trip that loses more than
execution.max_roundtrip_loss_pct (fees, taxes, a thin pool), means no buy. Live trading does the same
in the Jupiter executor. A quote can still pass on a token whose transfer logic blocks the real sell,
which is what the token-settings check in data/profile.py is for.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from ..config import ExecutionConfig, HoneypotConfig

log = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
CACHE_TTL_S = 60.0


class SellRouteCheck:
    def __init__(self, cfg: HoneypotConfig, ex: ExecutionConfig, sol_price_usd: Callable[[], float],
                 client: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.ex = ex
        self.sol_price_usd = sol_price_usd
        headers = {"x-api-key": ex.jupiter_api_key} if ex.jupiter_api_key else {}
        self.http = client or httpx.Client(timeout=15.0, headers=headers)
        self.base = ex.jupiter_base_url.rstrip("/")
        self.clock = clock
        self.sleep = sleep
        self._cache: dict[str, tuple[float, str | None]] = {}

    def _quote(self, input_mint: str, output_mint: str, amount: int) -> dict[str, Any] | None:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount),
                  "slippageBps": str(self.ex.slippage_bps), "restrictIntermediateTokens": "true"}
        resp = self.http.get(f"{self.base}/quote", params=params)
        if resp.status_code == 429:  # the free API's rate limit: one short wait, then one retry
            self.sleep(1.5)
            resp = self.http.get(f"{self.base}/quote", params=params)
        if resp.status_code in (400, 404):  # Jupiter answers "no route" with a 4xx
            return None
        resp.raise_for_status()
        q = resp.json()
        return q if isinstance(q, dict) and q.get("outAmount") else None

    def check(self, mint: str, usd: float) -> str | None:
        """None if it can be sold back within the loss limit, else why not."""
        hit = self._cache.get(mint)
        if hit and hit[0] > self.clock():
            return hit[1]
        try:
            lamports = int(usd / self.sol_price_usd() * LAMPORTS_PER_SOL)
            buy = self._quote(WSOL_MINT, mint, lamports)
            if buy is None:
                why: str | None = "honeypot check: no Jupiter buy route"
            else:
                sell = self._quote(mint, WSOL_MINT, int(buy["outAmount"]))
                if sell is None:
                    why = "honeypot check: Jupiter finds no route to sell it back"
                else:
                    loss = (1 - int(sell["outAmount"]) / lamports) * 100 if lamports else 100.0
                    limit = self.ex.max_roundtrip_loss_pct
                    why = (f"honeypot check: buying and selling straight back loses {loss:.1f}% (limit {limit:g}%)"
                           if loss > limit else None)
        except (httpx.HTTPError, ValueError, RuntimeError, KeyError) as exc:
            log.warning("Jupiter quote failed for %s: %s", mint, type(exc).__name__)
            return "honeypot check: Jupiter unreachable" if self.cfg.fail_closed else None
        self._cache[mint] = (self.clock() + CACHE_TTL_S, why)
        return why
