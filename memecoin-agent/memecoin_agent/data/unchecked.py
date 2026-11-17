"""Discovery for chains DexScreener's profile and boost lists rarely cover (Elysium).

DexScreener's search answers a chain's name with pairs on that chain. Every discover_every_s this searches each
configured chain and keeps the base tokens of pairs quoted in one of unchecked.quote_symbols. The search returns
at most about 30 pairs, so this sees the most active coins, not every launch.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from ..config import UncheckedConfig

log = logging.getLogger(__name__)

SEARCH = "https://api.dexscreener.com/latest/dex/search"


class ChainSearchDiscovery:
    def __init__(self, cfg: UncheckedConfig, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.clock = cfg, clock
        self.http = http or httpx.Client(timeout=12.0, headers={"Accept": "application/json"})
        self._next = 0.0
        self._found: dict[str, str] = {}  # token address -> chain

    def _search(self, chain: str) -> list[dict[str, Any]]:
        resp = self.http.get(SEARCH, params={"q": chain})
        resp.raise_for_status()
        return (resp.json() or {}).get("pairs") or []

    def discover(self) -> list[str]:
        if self.cfg.enabled and self.clock() >= self._next:
            self._next = self.clock() + self.cfg.discover_every_s
            quotes = {q.upper() for q in self.cfg.quote_symbols}
            found: dict[str, str] = {}
            for chain in self.cfg.chains:
                try:
                    pairs = self._search(chain)
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("Discovery search for %s failed: %s", chain, exc)
                    found.update({a: c for a, c in self._found.items() if c == chain})  # keep the last result
                    continue
                for p in pairs:
                    base = (p.get("baseToken") or {}).get("address")
                    quote = str((p.get("quoteToken") or {}).get("symbol") or "").upper()
                    if p.get("chainId") == chain and base and quote in quotes:
                        found[str(base).lower()] = chain
            self._found = found
        return list(self._found)

    def chain_of(self, mint: str) -> str | None:
        return self._found.get(mint)
