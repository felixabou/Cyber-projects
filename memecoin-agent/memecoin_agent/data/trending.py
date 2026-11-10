"""More coins to look at: Jupiter's and GeckoTerminal's live lists (both free, no key).

DexScreener's discovery lists are coins that paid for a profile or a boost, and the launch feed is pump.fun
graduations: between them the agent sees a few dozen coins. These lists are ranked by what is actually
trading now:

- Jupiter (lite-api.jup.ag/tokens/v2/<list>/<interval>): top trending, top traded and top organic score over
  5 minutes and 1 hour, 100 coins each, with market cap, liquidity, age and the mint's authorities, so coins
  outside the agent's size and age window are dropped here and never use scan budget;
- GeckoTerminal (trending_pools): Solana pools trending over 5 minutes and 1 hour.

Only discovery: every coin found here still goes through the same filters and safety checks as any other.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from ..config import DiscoveryConfig, FilterConfig
from .jupiter_tokens import _f, age_hours

log = logging.getLogger(__name__)

JUPITER = "https://lite-api.jup.ag/tokens/v2"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"


class TrendingDiscovery:
    def __init__(self, cfg: DiscoveryConfig, filters: FilterConfig, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.cfg, self.filters = cfg, filters
        self.http = http or httpx.Client(timeout=12.0, headers={"Accept": "application/json"})
        self.clock, self.wall = clock, wall
        self._next = 0.0
        self._turn = 0
        self._lists: dict[str, list[str]] = {}
        self._known: set[str] = set()
        self._sources = [(f"jupiter {n}", self._jupiter, n) for n in cfg.jupiter_lists] + \
                        [(f"geckoterminal {d}", self._gecko, d) for d in cfg.gecko_durations]
        self._warned: set[str] = set()
        self.counts: dict[str, int] = {}  # list -> coins it offered at the last refresh

    def _get(self, url: str, **params: Any) -> Any:
        resp = self.http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def wanted(self, t: dict[str, Any]) -> bool:
        """A Jupiter row inside the agent's size and age window, with the mint's authorities given up."""
        c, f = self.cfg, self.filters
        mcap, liq = _f(t.get("mcap")), _f(t.get("liquidity"))
        if mcap is None or not c.min_market_cap_usd <= mcap <= f.max_market_cap_usd:
            return False
        if liq is None or liq < c.min_liquidity_usd:
            return False
        age = age_hours(t, self.wall())
        if age is not None and age > f.max_age_hours:
            return False
        audit = t.get("audit") or {}
        if audit.get("mintAuthorityDisabled") is False or audit.get("freezeAuthorityDisabled") is False:
            return False
        return str(t.get("id") or "") not in f.blacklist

    def _jupiter(self, name: str) -> list[str]:
        rows = self._get(f"{JUPITER}/{name}", limit=self.cfg.per_list) or []
        return [str(t["id"]) for t in rows if isinstance(t, dict) and t.get("id") and self.wanted(t)]

    def _gecko(self, duration: str) -> list[str]:
        c, f = self.cfg, self.filters
        out: list[str] = []
        for pool in (self._get(GECKO, duration=duration, page=1) or {}).get("data") or []:
            attrs = pool.get("attributes") or {}
            token = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            mint = str(token.get("id") or "").removeprefix("solana_")
            cap = _f(attrs.get("market_cap_usd")) or _f(attrs.get("fdv_usd"))
            if mint and cap is not None and c.min_market_cap_usd <= cap <= f.max_market_cap_usd \
                    and (_f(attrs.get("reserve_in_usd")) or 0) >= c.min_liquidity_usd and mint not in f.blacklist:
                out.append(mint)
        return out

    def discover(self) -> list[str]:
        """Every coin on the lists. Each call refreshes at most one list, in turn, so a full round takes
        refresh_s and no two calls go out together (Jupiter's free tier allows about one a second)."""
        now = self.clock()
        if self._sources and now >= self._next:
            self._next = now + self.cfg.refresh_s / len(self._sources)
            label, fetch, arg = self._sources[self._turn % len(self._sources)]
            self._turn += 1
            try:
                self._lists[label] = fetch(arg)
                self._warned.discard(label)
                self.counts[label] = len(self._lists[label])
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:  # the list keeps its last result
                if label not in self._warned:  # once per list, not every round it stays down
                    self._warned.add(label)
                    log.warning("Trending list %s unavailable: %s", label, exc)
            self._known = {m for mints in self._lists.values() for m in mints}
        return list(dict.fromkeys(m for label, _, _ in self._sources for m in self._lists.get(label, [])))

    def chain_of(self, mint: str) -> str | None:
        return "solana" if mint in self._known else None
