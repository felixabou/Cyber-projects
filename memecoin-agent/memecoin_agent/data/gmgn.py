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
import threading
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
        return self.error in ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED", "RATE_LIMIT_BANNED_MAX")

    @property
    def plan_upgrade(self) -> bool:
        """The route weighs more than the plan's bucket holds: it can never succeed on this plan."""
        return self.error == "PLAN_UPGRADE_REQUIRED"


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


# Rate-limit weight of each route (GMGN's gmgn-skills docs): the free plan refills 5 a second,
# holding at most 5. Unlisted routes are assumed to weigh 2.
WEIGHTS = {"/v1/market/rank": 3, "/v1/user/wallet_activity": 3, "/v1/user/wallet_stats": 3,
           "/v1/market/token_top_traders": 5, "/v1/market/token_top_holders": 5, "/v1/market/token_kline": 2,
           "/v1/user/smartmoney": 1, "/v1/user/kol": 1, "/v1/token/info": 1, "/v1/token/security": 1,
           "/v1/token/pool_info": 1, "/v1/user/created_tokens": 2}


class GmgnClient:
    """Thread-safe: the agent, the copy tracker and the long-term scanner share one client and one
    rate budget (a leaky bucket, so bursts wait instead of earning a GMGN ban)."""

    def __init__(self, api_key: str, host: str = "https://openapi.gmgn.ai", client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, rate_per_s: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.host = host.rstrip("/")
        self.http = client or httpx.Client(timeout=20.0)
        self.headers = {"X-APIKEY": api_key, "Content-Type": "application/json", "User-Agent": "memecoin-agent"}
        self.clock = clock
        self.sleep = sleep
        self.blocked_until = 0.0  # set when GMGN reports a rate limit
        self.off_plan: set[str] = set()  # routes GMGN answered PLAN_UPGRADE_REQUIRED for
        self.rate = max(rate_per_s, 0.1)
        self._lock = threading.Lock()
        self._free_at = 0.0  # when the bucket has room for the next request

    def _wait_turn(self, path: str) -> None:
        weight = min(WEIGHTS.get(path, 2), self.rate)
        with self._lock:  # _free_at: when the bucket (capacity = rate) would be empty
            now = self.clock()
            start = max(now, self._free_at - (self.rate - weight) / self.rate)
            self._free_at = max(self._free_at, start) + weight / self.rate
        if start > now:
            self.sleep(start - now)

    def get(self, path: str, params: dict[str, Any], body: dict[str, Any] | None = None) -> Any:
        # No request at all while banned: GMGN extends a ban by 5 s for every call made during it, up to
        # 5 minutes (RATE_LIMIT_BANNED_MAX), and any call before reset_at restarts the 5 minutes.
        if self.clock() < self.blocked_until:
            raise GmgnError(f"GMGN rate limited for another {self.blocked_until - self.clock():.0f}s",
                            error="RATE_LIMIT_EXCEEDED")
        if path in self.off_plan:
            raise GmgnError(f"GET {path}: not available on this GMGN plan", error="PLAN_UPGRADE_REQUIRED")
        self._wait_turn(path)
        query = {**params, "timestamp": int(self.clock()), "client_id": str(uuid.uuid4())}
        if body is None:
            resp = self.http.get(f"{self.host}{path}", params=query, headers=self.headers)
        else:
            resp = self.http.post(f"{self.host}{path}", params=query, headers=self.headers, json=body)
        try:
            body = resp.json()
        except ValueError:
            raise GmgnError(f"GET {path}: HTTP {resp.status_code}, non-JSON response") from None
        if not isinstance(body, dict) or body.get("code") != 0:
            body = body if isinstance(body, dict) else {}
            # A ban carries its end in the body (reset_at); the header has the bucket's next refill.
            reset = _f(body.get("reset_at")) or _f(resp.headers.get("x-ratelimit-reset"))
            err = GmgnError(f"GET {path}: {body.get('error') or resp.status_code}: {body.get('message', '')}".strip(),
                            code=body.get("code"), error=body.get("error"), reset_at=reset)
            if err.rate_limited:
                self.blocked_until = reset if reset and reset > self.clock() else self.clock() + 60
                log.warning("GMGN %s: pausing every GMGN call until %s", err.error,
                            time.strftime("%H:%M:%S", time.localtime(self.blocked_until)))
            elif err.plan_upgrade:
                self.off_plan.add(path)
                log.warning("GMGN %s needs a bigger plan; not calling it again this run", path)
            raise err
        return body.get("data")

    def trending(self, interval: str, extra: dict[str, Any], chain: str = CHAIN) -> list[dict[str, Any]]:
        data = self.get("/v1/market/rank", {"chain": chain, "interval": interval, **extra})
        rows = data.get("rank") if isinstance(data, dict) else data
        return [r for r in rows or [] if isinstance(r, dict)]

    def security(self, mint: str, chain: str = CHAIN) -> dict[str, Any]:
        data = self.get("/v1/token/security", {"chain": chain, "address": mint})
        return data if isinstance(data, dict) else {}

    def info(self, mint: str, chain: str = CHAIN) -> dict[str, Any]:
        data = self.get("/v1/token/info", {"chain": chain, "address": mint})
        return data if isinstance(data, dict) else {}

    # -- wallets (copy lane) and history (long-term lane) ----------------------------------------
    @staticmethod
    def _rows(data: Any, *keys: str) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            for k in keys:
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def trades_feed(self, kind: str, chain: str, limit: int = 100) -> list[dict[str, Any]]:
        """Latest trades by GMGN's tagged wallets: kind "smartmoney" or "kol"."""
        return self._rows(self.get(f"/v1/user/{kind}", {"chain": chain, "limit": limit}), "list")

    def wallet_stats(self, chain: str, wallet: str, period: str = "30d") -> dict[str, Any]:
        data = self.get("/v1/user/wallet_stats", {"chain": chain, "wallet_address": wallet, "period": period})
        rows = self._rows(data, "list")
        return rows[0] if rows else (data if isinstance(data, dict) else {})

    def wallet_activity(self, chain: str, wallet: str, limit: int = 20, types: tuple[str, ...] = ("buy",)) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"chain": chain, "wallet_address": wallet, "limit": limit}
        if types:
            params["type"] = list(types)
        return self._rows(self.get("/v1/user/wallet_activity", params), "activities", "list")

    def top_traders(self, chain: str, address: str, limit: int = 50) -> list[dict[str, Any]]:
        data = self.get("/v1/market/token_top_traders", {"chain": chain, "address": address, "limit": limit,
                                                          "order_by": "profit", "direction": "desc"})
        return self._rows(data, "list", "holders", "traders")

    def top_holders(self, chain: str, address: str, limit: int = 50) -> list[dict[str, Any]]:
        data = self.get("/v1/market/token_top_holders", {"chain": chain, "address": address, "limit": limit,
                                                          "order_by": "amount_percentage", "direction": "desc"})
        return self._rows(data, "list", "holders")

    def kline(self, chain: str, address: str, resolution: str, start: int, end: int) -> list[dict[str, Any]]:
        data = self.get("/v1/market/token_kline", {"chain": chain, "address": address, "resolution": resolution,
                                                    "from": start, "to": end})
        return self._rows(data, "list", "klines", "candles")


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
        warnings = self.warnings(sec, info) if c.max_warnings is not None else []
        if len(warnings) > (c.max_warnings or 0):
            reasons.append(f"gmgn {len(warnings)} warnings: " + "; ".join(warnings))
        return SafetyReport(ok=not reasons, score=_f(sec.get("rug_ratio")), reasons=reasons)

    def warnings(self, sec: dict[str, Any], info: dict[str, Any]) -> list[str]:
        """GMGN's soft checklist. Its trader-profile items (entrapment, bots, bundlers, fresh wallets)
        are what caught coins its holder checks passed. Missing fields raise no warning."""
        c = self.cfg
        stat = info.get("stat") or {}
        tags = info.get("wallet_tags_stat") or {}
        out: list[str] = []

        def over(label: str, value: Any, limit: float) -> None:
            v = _f(value)
            if v is not None and v > limit:
                out.append(f"{label} {v * 100:.0f}%")

        over("top-10 hold", sec.get("top_10_holder_rate", stat.get("top_10_holder_rate")), c.warn_top10_holder_rate)
        over("creator holds", stat.get("creator_hold_rate", sec.get("creator_balance_rate")), c.warn_creator_hold_rate)
        over("insider volume", stat.get("top_rat_trader_percentage", sec.get("rat_trader_amount_rate")),
             c.warn_rat_trader_rate)
        over("entrapment volume", stat.get("top_entrapment_trader_percentage"), c.warn_entrapment_rate)
        over("bot traders", stat.get("top_bot_degen_percentage", stat.get("bot_degen_rate")), c.warn_bot_degen_rate)
        over("fresh wallets", stat.get("fresh_wallet_rate"), c.warn_fresh_wallet_rate)
        holders = _f(info.get("holder_count") or stat.get("holder_count"))
        bundlers = _f(tags.get("bundler_wallets"))
        if holders and bundlers is not None:  # a count of wallets, not a share
            over("bundler wallets", bundlers / holders, c.warn_bundler_wallet_share)
        if c.warn_min_holders and holders is not None and holders < c.warn_min_holders:
            out.append(f"{holders:.0f} holders")
        return out

    def check(self, mint: str) -> SafetyReport:
        hit = self._cache.get(mint)
        if hit and hit[0] > self.clock():
            return hit[1]
        try:
            sec = self.client.security(mint)
            # token/info adds smart money, fresh wallets and the creator's history; only needed for
            # the rules that use it (saves a call per candidate otherwise).
            needs_info = (self.cfg.max_fresh_wallet_rate is not None or self.cfg.max_creator_launches is not None
                          or self.cfg.min_smart_wallets > 0 or self.cfg.max_warnings is not None)
            info = self.client.info(mint) if needs_info else {}
        except (httpx.HTTPError, GmgnError) as exc:
            log.warning("GMGN safety lookup failed for %s: %s", mint, exc)
            return SafetyReport(ok=not self.cfg.fail_closed, score=None,
                                reasons=[UNAVAILABLE] if self.cfg.fail_closed else [])
        report = self.evaluate(sec, info)
        # Holder structure moves fast on young coins: keep verdicts briefly.
        self._cache[mint] = (self.clock() + (900 if not report.ok else 300), report)
        return report
