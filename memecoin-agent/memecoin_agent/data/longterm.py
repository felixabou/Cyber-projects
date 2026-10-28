"""Long-term lane: coins over a year old that wore down gradually, still trade every day and still
have big holders in. Bought with their own budget and slots, held without a time stop.

Candidates: longterm.watchlist, plus (with a GMGN key) each chain's tokens over min_age_days old,
ranked by 24h volume. Each is judged on:
- DexScreener: liquidity, market cap, 24h volume and buys;
- daily candles (GMGN, or GeckoTerminal for the main pool): far below its high, lower than one to
  three months ago, no single-day crash in the last month, not still sliding this week, trading most days;
- holders (GMGN, else GeckoTerminal's holder distribution, GoPlus on EVM chains or RugCheck on
  Solana): holder count and how many wallets hold at least longterm.large_holder_usd.
Runs on its own thread every longterm.rescan_hours; the agent buys from `qualified()`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..config import LongTermConfig
from .chain_safety import GOPLUS, GOPLUS_CHAIN_IDS
from .dexscreener import DEX_TO_GMGN, DexScreenerClient, norm_address
from .gmgn import GmgnClient, GmgnError, _f

log = logging.getLogger(__name__)

GECKO = "https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/ohlcv/day"
GECKO_TOKEN = "https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{address}/info"
GECKO_NETWORKS = {"solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base", "arbitrum": "arbitrum",
                  "robinhood": "robinhood", "hyperevm": "hyperevm"}
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
DAY = 86400


@dataclass
class Candle:
    t: float  # day start, unix seconds
    close: float
    high: float
    volume_usd: float


@dataclass
class LongTermVerdict:
    chain: str  # DexScreener chain id
    address: str
    symbol: str = ""
    ok: bool = False
    reasons: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    checked_at: float = 0.0
    watchlist: bool = False


def candle_reasons(cfg: LongTermConfig, candles: list[Candle], now: float) -> tuple[list[str], dict[str, Any]]:
    """Why the daily chart doesn't fit (empty: it does), and what it showed."""
    if len(candles) < 30:
        return [f"only {len(candles)} days of price history"], {}
    candles = sorted(candles, key=lambda c: c.t)
    last = candles[-1].close
    high = max(c.high for c in candles)
    drawdown = (1 - last / high) * 100 if high > 0 else 0.0
    month = [c for c in candles if c.t >= now - 31 * DAY]
    drops = [(1 - b.close / a.close) * 100 for a, b in zip(month, month[1:]) if a.close > 0]
    worst = max(drops, default=0.0)
    # Wearing down: below where it traded one to three months ago (its first week left out: launch
    # prices would make almost anything look like it rose).
    settled = [c for c in candles if c.t >= candles[0].t + 7 * DAY]
    ref_window = [c.close for c in settled if now - 90 * DAY <= c.t <= now - 30 * DAY] or [c.close for c in settled[:-30]]
    ref = sorted(ref_window)[len(ref_window) // 2] if ref_window else 0.0
    change_ref = (last / ref - 1) * 100 if ref > 0 else 0.0
    week = [c for c in candles if c.t >= now - 8 * DAY]
    drop_7d = (1 - last / week[0].close) * 100 if week and week[0].close > 0 else 0.0
    active = sum(1 for c in candles if c.t >= now - 30 * DAY and c.volume_usd > 0)
    facts = {"below_high_pct": round(drawdown), "worst_day_pct": round(worst), "vs_1_3_months_pct": round(change_ref),
             "drop_7d_pct": round(drop_7d), "active_days_30d": active, "history_days": len(candles)}
    why = []
    if drawdown < cfg.min_drawdown_from_high_pct:
        why.append(f"only {drawdown:.0f}% below its high (wants {cfg.min_drawdown_from_high_pct:g}%+)")
    if worst > cfg.max_daily_drop_pct:
        why.append(f"fell {worst:.0f}% in one day this month (not gradual)")
    if ref > 0 and change_ref >= 0:
        why.append(f"{change_ref:+.0f}% vs 1-3 months ago (not wearing down)")
    if drop_7d > cfg.max_drop_7d_pct:
        why.append(f"still sliding: {drop_7d:.0f}% down this week")
    if active < cfg.min_active_days:
        why.append(f"traded on {active} of the last 30 days (min {cfg.min_active_days})")
    return why, facts


def large_from_distribution(dist: dict[str, Any], market_cap: float, big: float) -> int:
    """Wallets holding at least `big`, counted conservatively from GeckoTerminal's holder buckets
    (% of supply held by holders ranked 1-10, 11-30, 31-50): a bucket counts in full only when
    the next one's average holder still clears the bar (everyone ranked above holds at least as much)."""
    def avg(key: str, n: int) -> float:
        return (_f(dist.get(key)) or 0.0) / 100 * market_cap / n

    top, mid, low = avg("top_10", 10), avg("11_30", 20), avg("31_50", 20)
    count = 10 if mid >= big else (1 if top >= big else 0)
    count += 20 if low >= big else 0
    return count


class LongTermScanner:
    def __init__(self, cfg: LongTermConfig, dex: DexScreenerClient, gmgn: GmgnClient | None = None,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.dex = dex
        self.gmgn = gmgn
        self.http = http or httpx.Client(timeout=20.0, headers={"Accept": "application/json"})
        self.clock = clock
        self.verdicts: dict[tuple[str, str], LongTermVerdict] = {}
        self.last_scan = 0.0
        self.scanning = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="longterm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.clock() - self.last_scan >= self.cfg.rescan_hours * 3600:
                try:
                    self.scan()
                except Exception as exc:  # never let the scanner thread die
                    log.warning("Long-term scan failed: %s", exc)
                    self.last_scan = self.clock()
            self._stop.wait(60)

    # -- candidates --------------------------------------------------------------------------------
    def watchlist(self) -> list[tuple[str, str]]:
        out = []
        for entry in self.cfg.watchlist:
            chain, sep, address = entry.partition(":")
            chain, address = (chain.strip(), address.strip()) if sep else ("solana", chain.strip())
            out.append((chain, norm_address(chain, address)))
        return out

    def discovered(self) -> list[tuple[str, str]]:
        if self.gmgn is None or not self.cfg.discovery:
            return []
        c = self.cfg
        out = []
        for chain in c.chains:
            gchain = DEX_TO_GMGN.get(chain)
            if not gchain:
                continue
            try:
                rows = self.gmgn.trending("24h", {"limit": 50, "order_by": "volume", "direction": "desc",
                                                  "min_created": f"{int(c.min_age_days)}d",
                                                  "min_liquidity": c.min_liquidity_usd, "min_marketcap": c.min_market_cap_usd,
                                                  "max_marketcap": c.max_market_cap_usd}, chain=gchain)
            except (httpx.HTTPError, GmgnError) as exc:
                log.info("Long-term discovery on %s unavailable: %s", chain, exc)
                continue
            out += [(chain, norm_address(chain, str(r["address"]))) for r in rows if r.get("address")]
        return out

    def scan(self) -> None:
        self.scanning = True
        try:
            watch = set(self.watchlist())
            todo = list(dict.fromkeys([*watch, *self.discovered()]))
            for chain, address in todo:
                if self._stop.is_set():
                    break
                v = self.evaluate(chain, address, (chain, address) in watch)
                with self._lock:
                    self.verdicts[(chain, address)] = v
            self.last_scan = self.clock()
            ok = [v for v in self.verdicts.values() if v.ok]
            log.info("Long-term scan: %d candidate(s), %d qualify%s", len(todo), len(ok),
                     ": " + ", ".join(f"{v.symbol} ({v.chain})" for v in ok) if ok else "")
        finally:
            self.scanning = False

    # -- judging one token ---------------------------------------------------------------------------
    def evaluate(self, chain: str, address: str, from_watchlist: bool = False) -> LongTermVerdict:
        c, now = self.cfg, self.clock()
        v = LongTermVerdict(chain, address, checked_at=now, watchlist=from_watchlist)
        snap = self.dex.snapshots_on(chain, [address]).get(address)
        if snap is None:
            v.reasons = ["no DexScreener pair"]
            return v
        v.symbol = snap.symbol
        v.facts.update({"price_usd": snap.price_usd, "liquidity_usd": snap.liquidity_usd,
                        "market_cap_usd": snap.market_cap_usd, "volume_h24_usd": snap.volume_h24,
                        "buys_h24": snap.buys_h24, "url": snap.url})
        why = v.reasons
        if snap.liquidity_usd < c.min_liquidity_usd:
            why.append(f"liquidity ${snap.liquidity_usd:,.0f} < ${c.min_liquidity_usd:,.0f}")
        if not c.min_market_cap_usd <= snap.market_cap_usd <= c.max_market_cap_usd:
            why.append(f"market cap ${snap.market_cap_usd:,.0f} outside ${c.min_market_cap_usd:,.0f}-${c.max_market_cap_usd:,.0f}")
        if snap.volume_h24 < c.min_volume_h24_usd:
            why.append(f"24h volume ${snap.volume_h24:,.0f} < ${c.min_volume_h24_usd:,.0f}")
        if snap.buys_h24 < c.min_buys_h24:
            why.append(f"{snap.buys_h24} buys in 24h (min {c.min_buys_h24})")
        if why:  # cheap checks first: skip the history and holder lookups
            return v
        candles = self.daily_candles(chain, address, snap.pair_address)
        age_days = (now - candles[0].t) / DAY if candles else 0.0
        if snap.pair_created_at is not None:
            age_days = max(age_days, (now - snap.pair_created_at.timestamp()) / DAY)
        v.facts["age_days"] = round(age_days)
        if age_days < c.min_age_days and not from_watchlist:
            why.append(f"{age_days:.0f} days old (min {c.min_age_days:g})")
        cw, facts = candle_reasons(c, candles, now)
        why += cw
        v.facts.update(facts)
        holders, large = self.holders(chain, address, snap.market_cap_usd)
        v.facts.update({"holders": holders, "large_holders": large})
        if holders is None:
            why.append("holder data unavailable")
        else:
            if holders < c.min_holders:
                why.append(f"{holders} holders (min {c.min_holders})")
            if large < c.min_large_holders:
                why.append(f"{large} holders with ${c.large_holder_usd:,.0f}+ (min {c.min_large_holders})")
        v.ok = not why
        return v

    def daily_candles(self, chain: str, address: str, pool: str) -> list[Candle]:
        now = int(self.clock())
        gchain = DEX_TO_GMGN.get(chain)
        if self.gmgn is not None and gchain:
            try:
                rows = self.gmgn.kline(gchain, address, "1d", now - 400 * DAY, now)
                out = [Candle(_f(r.get("time")) or 0, _f(r.get("close")) or 0, _f(r.get("high")) or 0,
                              max(_f(r.get("amount")) or 0, _f(r.get("volume")) or 0)) for r in rows]
                if out:
                    return sorted(out, key=lambda x: x.t)
            except (httpx.HTTPError, GmgnError) as exc:
                log.info("GMGN daily candles for %s unavailable: %s", address, exc)
        net = GECKO_NETWORKS.get(chain)
        if not net or not pool:
            return []
        try:
            resp = self.http.get(GECKO.format(net=net, pool=pool),
                                 params={"aggregate": 1, "limit": 400, "currency": "usd", "token": address})
            resp.raise_for_status()
            rows = resp.json()["data"]["attributes"]["ohlcv_list"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.info("GeckoTerminal daily candles for %s unavailable: %s", address, exc)
            return []
        return sorted((Candle(float(r[0]), float(r[4]), float(r[2]), float(r[5])) for r in rows), key=lambda x: x.t)

    def holders(self, chain: str, address: str, market_cap: float) -> tuple[int | None, int]:
        """(holder count, holders with at least large_holder_usd, pools left out)."""
        big = self.cfg.large_holder_usd
        gchain = DEX_TO_GMGN.get(chain)
        if self.gmgn is not None and gchain:
            try:
                info = self.gmgn.info(address, gchain)
                count = int(_f(info.get("holder_count") or (info.get("stat") or {}).get("holder_count")) or 0) or None
                rows = self.gmgn.top_holders(gchain, address, 100)
                large = sum(1 for r in rows if str(r.get("addr_type")) != "2" and (_f(r.get("usd_value")) or 0) >= big)
                if count is not None:
                    return count, large
            except (httpx.HTTPError, GmgnError) as exc:
                log.info("GMGN holders for %s unavailable: %s", address, exc)
        net = GECKO_NETWORKS.get(chain)
        if net:
            try:
                resp = self.http.get(GECKO_TOKEN.format(net=net, address=address))
                resp.raise_for_status()
                h = (resp.json()["data"]["attributes"].get("holders") or {})
                if h.get("count"):
                    return int(h["count"]), large_from_distribution(h.get("distribution_percentage") or {}, market_cap, big)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.info("GeckoTerminal holders for %s unavailable: %s", address, exc)
        try:
            if chain in GOPLUS_CHAIN_IDS:
                resp = self.http.get(GOPLUS.format(chain_id=GOPLUS_CHAIN_IDS[chain]), params={"contract_addresses": address})
                resp.raise_for_status()
                d = (resp.json().get("result") or {}).get(address.lower()) or {}
                if d:
                    rows = [h for h in d.get("holders") or [] if str(h.get("is_contract")) != "1" and not h.get("tag")]
                    large = sum(1 for h in rows if (_f(h.get("percent")) or 0) * market_cap >= big)
                    return int(_f(d.get("holder_count")) or 0), large
            elif chain == "solana":
                resp = self.http.get(RUGCHECK_REPORT.format(mint=address))
                resp.raise_for_status()
                d = resp.json()
                known = d.get("knownAccounts") or {}
                rows = [h for h in d.get("topHolders") or [] if h.get("owner") not in known and h.get("address") not in known]
                large = sum(1 for h in rows if (_f(h.get("pct")) or 0) / 100 * market_cap >= big)
                return (int(d["totalHolders"]) if isinstance(d.get("totalHolders"), int) else None), large
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.info("Holder data for %s unavailable: %s", address, exc)
        return None, 0

    # -- for the agent and the dashboard -----------------------------------------------------------
    def qualified(self) -> list[LongTermVerdict]:
        with self._lock:
            return [v for v in self.verdicts.values() if v.ok]

    def view(self) -> dict[str, Any]:
        with self._lock:
            rows = sorted(self.verdicts.values(), key=lambda v: (not v.ok, not v.watchlist, v.symbol))
            return {"last_scan": self.last_scan, "scanning": self.scanning,
                    "tokens": [{"chain": v.chain, "address": v.address, "symbol": v.symbol, "ok": v.ok,
                                "watchlist": v.watchlist, "reasons": v.reasons, "facts": v.facts} for v in rows[:60]]}
