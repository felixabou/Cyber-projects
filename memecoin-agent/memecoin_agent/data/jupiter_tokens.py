"""Jupiter's token data (lite-api.jup.ag/tokens/v2, free, no key, up to 100 mints a call).

One lookup carries what several paid sources sell separately: an organic-trading score, the
creator's launch count, holders, liquidity and 24h volume. The pre-buy check here rejects:
- coin factories: a creator who has minted `max_dev_mints` coins or more (the 🖖 coin's had 179,773),
  on a coin that hasn't found a crowd yet (fewer than `factory_max_holders` holders);
- bot volume: an organic score under `min_organic_score` on a coin with real volume. Jupiter leaves
  fresh coins at 0 until it has scored them, so coins younger than `organic_min_age_hours` are
  never judged on it;
- shells: a market cap over `shell_min_market_cap_usd` held by fewer than `shell_max_holders` wallets;
- wash trading: 24h volume far above what the pool size explains, among few holders (GMGN's bands:
  turnover over 15x below $100k of liquidity, 12x below $1M, 30x above, with under 150 holders).
Unreachable or unknown to Jupiter: the other checks decide.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable

import httpx

from ..config import JupiterConfig
from ..models import SafetyReport

log = logging.getLogger(__name__)

SEARCH = "https://lite-api.jup.ag/tokens/v2/search"
BATCH = 100
MAX_RETRY_WAIT_S = 5.0  # the free tier's window is a few seconds: wait it out once, never longer


def _f(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def age_hours(token: dict[str, Any], now: float) -> float | None:
    raw = token.get("createdAt") or (token.get("firstPool") or {}).get("createdAt")
    if not raw:
        return None
    try:
        return (now - datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()) / 3600
    except ValueError:
        return None


def wash_turnover_limit(liquidity_usd: float) -> float:
    """Most 24h volume / liquidity a real market of this size shows (GMGN's bands)."""
    if liquidity_usd < 100_000:
        return 15.0
    return 12.0 if liquidity_usd < 1_000_000 else 30.0


def reasons_for(cfg: JupiterConfig, t: dict[str, Any], now: float) -> list[str]:
    """Why Jupiter's data rules a coin out (empty: it doesn't)."""
    why: list[str] = []
    audit = t.get("audit") or {}
    holders, mcap, liq = _f(t.get("holderCount")), _f(t.get("mcap")), _f(t.get("liquidity")) or 0.0
    mints = _f(audit.get("devMints"))
    # A spray launcher doesn't care what happens to any one coin, but some of its coins find a real
    # crowd anyway (live: DJT, 71k holders, from a 1,146-coin creator): only coins that haven't yet
    # are ruled out.
    if (cfg.max_dev_mints is not None and mints is not None and mints >= cfg.max_dev_mints
            and (holders is None or holders < cfg.factory_max_holders)):
        why.append(f"creator has minted {mints:,.0f} coins (coin factory)")
    s1h, s24 = t.get("stats1h") or {}, t.get("stats24h") or {}
    vol_1h = (_f(s1h.get("buyVolume")) or 0) + (_f(s1h.get("sellVolume")) or 0)
    organic, age = _f(t.get("organicScore")), age_hours(t, now)
    if (cfg.min_organic_score is not None and organic is not None and organic < cfg.min_organic_score
            and age is not None and age >= cfg.organic_min_age_hours and vol_1h >= cfg.organic_min_volume_1h_usd):
        why.append(f"organic score {organic:.0f}/100 on ${vol_1h:,.0f} of 1h volume: mostly bot trading")
    if (cfg.shell_max_holders and holders is not None and mcap and mcap >= cfg.shell_min_market_cap_usd
            and holders < cfg.shell_max_holders):
        why.append(f"${mcap:,.0f} market cap held by {holders:.0f} wallets (shell)")
    vol_24 = (_f(s24.get("buyVolume")) or 0) + (_f(s24.get("sellVolume")) or 0)
    if (cfg.wash_max_holders and holders is not None and holders < cfg.wash_max_holders and liq > 0
            and (turnover := vol_24 / liq) > wash_turnover_limit(liq)):
        shown = f"{turnover:.0f}x" if turnover < 1000 else "over 1,000x"  # a near-empty pool divides to nonsense
        why.append(f"24h volume {shown} its pool among {holders:.0f} holders: wash trading")
    return why


class JupiterTokens:
    """Batched, cached lookups (Jupiter's free tier allows a handful of calls per window)."""

    def __init__(self, http: httpx.Client | None = None, ttl_s: float = 120.0, gap_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.http = http or httpx.Client(timeout=15.0, headers={"Accept": "application/json"})
        self.ttl_s, self.gap_s, self.clock, self.sleep = ttl_s, gap_s, clock, sleep
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._next_call = 0.0
        self._lock = threading.Lock()

    def fetch(self, mints: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Jupiter's record per mint it knows. Raises httpx.HTTPError when Jupiter can't be reached."""
        now = self.clock()
        want = list(dict.fromkeys(mints))
        fresh = {m: hit[1] for m in want if (hit := self._cache.get(m)) and hit[0] > now}
        todo = [m for m in want if m not in fresh]
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            with self._lock:
                wait = self._next_call - self.clock()
                if wait > 0:
                    self.sleep(wait)
                self._next_call = self.clock() + self.gap_s
            resp = self.http.get(SEARCH, params={"query": ",".join(chunk)})
            if resp.status_code == 429 and (wait := self._retry_wait(resp)) is not None:
                self.sleep(wait)
                resp = self.http.get(SEARCH, params={"query": ",".join(chunk)})
            resp.raise_for_status()
            rows = {str(r.get("id")): r for r in resp.json() or [] if isinstance(r, dict)}
            for m in chunk:
                self._cache[m] = (self.clock() + self.ttl_s, rows.get(m))  # unknown mints cached too
                fresh[m] = rows.get(m)
        return {m: t for m, t in fresh.items() if t}

    @staticmethod
    def _retry_wait(resp: httpx.Response) -> float | None:
        """Seconds until a rate-limited call may go again (Retry-After, else the window's reset time),
        or None when that's too long to hold up a buy decision."""
        after = _f(resp.headers.get("retry-after"))
        if after is None and (reset := _f(resp.headers.get("x-ratelimit-reset"))):
            after = reset - time.time() + 0.2
        return max(0.0, after) if after is not None and after <= MAX_RETRY_WAIT_S else None


class JupiterTokenSafety:
    """SafetyChecker over Jupiter's token data (Solana mints)."""

    def __init__(self, cfg: JupiterConfig, tokens: JupiterTokens, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.tokens = tokens
        self.clock = clock

    def check(self, mint: str) -> SafetyReport:
        try:
            t = self.tokens.fetch([mint]).get(mint)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("Jupiter token data unavailable for %s: %s", mint, exc)
            return SafetyReport(ok=True, score=None, reasons=[])
        if t is None:
            return SafetyReport(ok=True, score=None, reasons=[])
        why = reasons_for(self.cfg, t, self.clock())
        return SafetyReport(ok=not why, score=_f(t.get("organicScore")), reasons=[f"jupiter: {w}" for w in why])
