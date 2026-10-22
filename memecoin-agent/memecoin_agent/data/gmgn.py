"""GMGN OpenAPI (gmgn.ai): trending-token discovery and a pre-buy safety check.

Only GMGN's read-only data endpoints are used. They authenticate with the API
key alone (an `X-APIKEY` header plus a timestamp and a one-time client_id in
the query); GMGN's request signing with the private key is only needed for
trading and wallet endpoints, which this module never calls.

- Discovery: `/v1/market/rank` (trending), pre-filtered server-side to the
  agent's liquidity / market-cap / age window. Prices still come from
  DexScreener, so the filters, recordings and backtests see one data source.
- Safety: `/v1/token/security` (rug ratio, wash trading, holder concentration,
  bundler/insider volume, tax) and `/v1/token/info` (fresh wallets, smart money,
  the creator's launch history). Runs after RugCheck and before LarpCheck, so a
  GMGN reject spends no LarpCheck credit.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from ..config import FilterConfig, GmgnConfig
from ..models import SafetyReport, TokenSnapshot

log = logging.getLogger(__name__)

ENV_KEY = "GMGN_API_KEY"
UNAVAILABLE = "gmgn unavailable"
CHAIN = "sol"


class GmgnError(RuntimeError):
    def __init__(self, message: str, code: Any = None, error: str | None = None, reset_at: float | None = None):
        super().__init__(message)
        self.code, self.error, self.reset_at = code, error, reset_at

    @property
    def rate_limited(self) -> bool:
        return self.error in ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED")


def load_api_key(cfg: GmgnConfig) -> str | None:
    key = os.environ.get(ENV_KEY, "").strip()
    if key:
        return key
    if cfg.api_key_file:
        path = Path(cfg.api_key_file).expanduser()
        if path.exists():
            for line in path.read_text().splitlines():  # dotenv-style file from `gmgn-cli config --apply`
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == ENV_KEY:
                    return value.strip().strip('"').strip("'") or None
    return None


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, (int, float)) and v == 1) or str(v).strip().lower() in ("true", "yes", "1")


class GmgnClient:
    def __init__(self, api_key: str, host: str = "https://openapi.gmgn.ai", client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.host = host.rstrip("/")
        self.http = client or httpx.Client(timeout=20.0)
        self.headers = {"X-APIKEY": api_key, "Content-Type": "application/json", "User-Agent": "memecoin-agent"}
        self.clock = clock
        self.blocked_until = 0.0  # set when GMGN reports a rate limit

    def get(self, path: str, params: dict[str, Any]) -> Any:
        if self.clock() < self.blocked_until:
            raise GmgnError(f"GMGN rate limited for another {self.blocked_until - self.clock():.0f}s",
                            error="RATE_LIMIT_EXCEEDED")
        query = {**params, "timestamp": int(self.clock()), "client_id": str(uuid.uuid4())}
        resp = self.http.get(f"{self.host}{path}", params=query, headers=self.headers)
        try:
            body = resp.json()
        except ValueError:
            raise GmgnError(f"GET {path}: HTTP {resp.status_code}, non-JSON response") from None
        if not isinstance(body, dict) or body.get("code") != 0:
            body = body if isinstance(body, dict) else {}
            reset = _f(resp.headers.get("x-ratelimit-reset"))
            err = GmgnError(f"GET {path}: {body.get('error') or resp.status_code}: {body.get('message', '')}".strip(),
                            code=body.get("code"), error=body.get("error"), reset_at=reset)
            if err.rate_limited:
                self.blocked_until = reset if reset and reset > self.clock() else self.clock() + 60
            raise err
        return body.get("data")

    def trending(self, interval: str, extra: dict[str, Any]) -> list[dict[str, Any]]:
        data = self.get("/v1/market/rank", {"chain": CHAIN, "interval": interval, **extra})
        rows = data.get("rank") if isinstance(data, dict) else data
        return [r for r in rows or [] if isinstance(r, dict)]

    def security(self, mint: str) -> dict[str, Any]:
        data = self.get("/v1/token/security", {"chain": CHAIN, "address": mint})
        return data if isinstance(data, dict) else {}

    def info(self, mint: str) -> dict[str, Any]:
        data = self.get("/v1/token/info", {"chain": CHAIN, "address": mint})
        return data if isinstance(data, dict) else {}


class GmgnDiscovery:
    """Trending Solana tokens, pre-filtered server-side to the agent's own entry window."""

    def __init__(self, cfg: GmgnConfig, filters: FilterConfig, client: GmgnClient,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.client = client
        self.clock = clock
        self._next = 0.0
        self._last: list[str] = []
        self.params: dict[str, Any] = {
            "limit": cfg.trending_limit,
            "order_by": cfg.trending_order_by,
            "direction": "desc",
            "filters": list(cfg.trending_filters),
            "min_liquidity": filters.min_liquidity_usd,
            "min_marketcap": filters.min_market_cap_usd,
            "max_marketcap": filters.max_market_cap_usd,
            "min_created": f"{max(1, int(filters.min_age_minutes))}m",
            "max_created": f"{max(1, int(filters.max_age_hours))}h",
        }
        if filters.max_liquidity_usd is not None:
            self.params["max_liquidity"] = filters.max_liquidity_usd

    def discover(self) -> list[str]:
        now = self.clock()
        if now < self._next:
            return list(self._last)  # between refreshes, keep offering the last trending set
        self._next = now + self.cfg.discover_every_s
        try:
            rows = self.client.trending(self.cfg.trending_interval, self.params)
        except (httpx.HTTPError, GmgnError) as exc:
            log.warning("GMGN trending failed: %s", exc)
            return list(self._last)
        self._last = list(dict.fromkeys(str(r["address"]) for r in rows if r.get("address")))
        return list(self._last)


class ExtraDiscoveryMarket:
    """MarketData wrapper: extra discovery sources ahead of the base's own; prices from the base."""

    def __init__(self, base: Any, *sources: Any) -> None:
        self.base = base
        self.sources = sources

    def discover(self) -> list[str]:
        found: list[str] = []
        for source in self.sources:
            try:
                found += source.discover()
            except Exception as exc:  # a discovery source must never break the scan
                log.warning("Discovery source %s failed: %s", type(source).__name__, exc)
        try:
            found += self.base.discover()
        except Exception as exc:
            log.warning("Base discovery failed: %s", exc)
        return list(dict.fromkeys(found))

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        return self.base.snapshots(mints)

    def sol_price_usd(self) -> float:
        return self.base.sol_price_usd()


class GmgnSafety:
    """SafetyChecker over GMGN's token security + info data."""

    def __init__(self, cfg: GmgnConfig, client: GmgnClient, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.client = client
        self.clock = clock
        self._cache: dict[str, tuple[float, SafetyReport]] = {}

    def evaluate(self, sec: dict[str, Any], info: dict[str, Any]) -> SafetyReport:
        c = self.cfg
        stat = info.get("stat") or {}
        tags = info.get("wallet_tags_stat") or {}
        dev = info.get("dev") or {}
        reasons: list[str] = []

        def over(label: str, value: Any, limit: float | None, pct: bool = True) -> None:
            v = _f(value)
            if limit is not None and v is not None and v > limit:
                reasons.append(f"gmgn {label} {v * 100:.0f}% > {limit * 100:.0f}%" if pct
                               else f"gmgn {label} {v:g} > {limit:g}")

        over("rug ratio", sec.get("rug_ratio"), c.max_rug_ratio)
        if c.reject_wash_trading and _truthy(sec.get("is_wash_trading")):
            reasons.append("gmgn wash trading detected")
        over("top-10 holders", sec.get("top_10_holder_rate", stat.get("top_10_holder_rate")), c.max_top10_holder_rate)
        over("dev team holds", sec.get("dev_team_hold_rate", stat.get("dev_team_hold_rate")), c.max_dev_team_hold_rate)
        over("creator holds", sec.get("creator_balance_rate", stat.get("creator_hold_rate")), c.max_creator_hold_rate)
        over("insiders hold", sec.get("suspected_insider_hold_rate"), c.max_insider_hold_rate)
        over("bundler volume", sec.get("bundler_trader_amount_rate", stat.get("top_bundler_trader_percentage")),
             c.max_bundler_rate)
        over("insider volume", sec.get("rat_trader_amount_rate", stat.get("top_rat_trader_percentage")),
             c.max_rat_trader_rate)
        over("sell tax", sec.get("sell_tax"), c.max_sell_tax)
        over("fresh wallets", stat.get("fresh_wallet_rate"), c.max_fresh_wallet_rate)
        over("creator's past launches", dev.get("creator_open_count"), c.max_creator_launches, pct=False)
        smart = _f(tags.get("smart_wallets"))
        if c.min_smart_wallets > 0 and (smart or 0) < c.min_smart_wallets:
            reasons.append(f"gmgn smart-money holders {int(smart or 0)} < {c.min_smart_wallets}")
        return SafetyReport(ok=not reasons, score=_f(sec.get("rug_ratio")), reasons=reasons)

    def check(self, mint: str) -> SafetyReport:
        hit = self._cache.get(mint)
        if hit and hit[0] > self.clock():
            return hit[1]
        try:
            sec = self.client.security(mint)
            # token/info adds smart money, fresh wallets and the creator's history; only needed for
            # the rules that use it (saves a call per candidate otherwise).
            needs_info = (self.cfg.max_fresh_wallet_rate is not None or self.cfg.max_creator_launches is not None
                          or self.cfg.min_smart_wallets > 0)
            info = self.client.info(mint) if needs_info else {}
        except (httpx.HTTPError, GmgnError) as exc:
            log.warning("GMGN safety lookup failed for %s: %s", mint, exc)
            return SafetyReport(ok=not self.cfg.fail_closed, score=None,
                                reasons=[UNAVAILABLE] if self.cfg.fail_closed else [])
        report = self.evaluate(sec, info)
        # Holder structure moves fast on young coins: keep verdicts briefly.
        self._cache[mint] = (self.clock() + (900 if not report.ok else 300), report)
        return report
