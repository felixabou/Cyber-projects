"""Discord caller: every automatic buy is posted as a call with a full report, then followed up.

- A call: the coin, its numbers, the checks it passed (or an UNCHECKED warning), why it was picked, a
  description when one is found, and the paper buy that was made at the same moment.
- Follow-ups at caller.follow_up_minutes after each call: the move since the call and the best seen so far.
- A daily recap of the last 24 hours of calls.

Posts go to a Discord webhook whose URL comes from an environment variable. Failures are logged and never stop
the agent; error messages never include the webhook URL.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import CallerConfig
from .models import SafetyReport, TokenSnapshot

log = logging.getLogger(__name__)

GREEN, ORANGE, BLUE, GREY = 0x2ECC71, 0xE67E22, 0x3498DB, 0x95A5A6
DESCRIPTION_KEYS = ("description", "intro", "introduction", "summary", "narrative", "desc")
DEX_PROFILE_FEEDS = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1")
GMGN_CHAIN = {"solana": "sol", "ethereum": "eth", "bsc": "bsc", "base": "base", "arbitrum": "arb",
              "robinhood": "robinhood", "monad": "monad", "tron": "tron", "hyperevm": "hyperevm"}


def _usd(v: float | None) -> str:
    if v is None:
        return "?"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= size:
            return f"${v / size:.2f}{unit}"
    return f"${v:,.0f}"


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:+.1f}%"


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def describe_from_gmgn(info: Any) -> str | None:
    """A description from a GMGN token-info answer, wherever it keeps one (top level or one level down)."""
    if not isinstance(info, dict):
        return None
    for d in (info, *(v for v in info.values() if isinstance(v, dict))):
        for k in DESCRIPTION_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


class Describer:
    """Token descriptions: GMGN token info first (when a client is given), then DexScreener's profile and boost
    feeds, which carry the description projects write for their DexScreener page."""

    def __init__(self, gmgn: Any = None, http: httpx.Client | None = None, refresh_s: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.gmgn = gmgn
        self.http = http or httpx.Client(timeout=10.0, headers={"Accept": "application/json"})
        self.refresh_s, self.clock = refresh_s, clock
        self._dex: dict[tuple[str, str], str] = {}
        self._next = 0.0

    def _refresh_dex(self) -> None:
        if self.clock() < self._next:
            return
        self._next = self.clock() + self.refresh_s
        for path in DEX_PROFILE_FEEDS:
            try:
                resp = self.http.get(f"https://api.dexscreener.com{path}")
                resp.raise_for_status()
                rows = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("DexScreener %s unavailable: %s", path, exc)
                continue
            for r in rows if isinstance(rows, list) else []:
                desc = r.get("description") if isinstance(r, dict) else None
                if isinstance(desc, str) and desc.strip() and r.get("tokenAddress"):
                    self._dex[(str(r.get("chainId")), str(r["tokenAddress"]).lower())] = desc.strip()
        if len(self._dex) > 5000:
            self._dex = dict(list(self._dex.items())[-2000:])

    def describe(self, snap: TokenSnapshot) -> tuple[str | None, str]:
        """(description, where it came from)."""
        chain = GMGN_CHAIN.get(snap.chain)
        if self.gmgn is not None and chain:
            try:
                if text := describe_from_gmgn(self.gmgn.info(snap.mint, chain=chain)):
                    return text, "GMGN"
            except Exception as exc:  # a description is extra; never let it stop a call
                log.debug("GMGN info for %s failed: %s", snap.symbol, exc)
        self._refresh_dex()
        text = self._dex.get((snap.chain, snap.mint.lower()))
        return (text, "DexScreener") if text else (None, "")


class DiscordWebhook:
    def __init__(self, url: str, username: str = "Memecoin Caller", http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.url, self.username, self.sleep = url, username, sleep
        self.http = http or httpx.Client(timeout=10.0)

    def post(self, content: str = "", embeds: list[dict] | None = None) -> bool:
        payload = {"username": self.username, "content": content[:2000], "embeds": (embeds or [])[:10],
                   "allowed_mentions": {"parse": []}}
        for attempt in range(2):
            try:
                resp = self.http.post(self.url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("Discord post failed (%s)", type(exc).__name__)
                return False
            if resp.status_code == 429 and attempt == 0:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    wait = 1.0
                self.sleep(min(max(wait, 0.5), 5.0))
                continue
            if resp.status_code >= 400:
                log.warning("Discord post rejected: HTTP %s", resp.status_code)
                return False
            return True
        return False


@dataclass
class Call:
    mint: str
    symbol: str
    chain: str
    called_at: str
    entry_price: float
    mcap0: float
    lane: str
    unchecked: bool = False
    done: list[float] = field(default_factory=list)
    moves: dict[str, float] = field(default_factory=dict)  # minutes after the call -> % move
    peak_pct: float = 0.0


class Caller:
    def __init__(self, cfg: CallerConfig, webhook: DiscordWebhook, describer: Describer | None = None,
                 state_path: str | Path | None = None) -> None:
        self.cfg, self.webhook, self.describer = cfg, webhook, describer
        self.state_path = Path(state_path or cfg.state_file)
        self.calls: list[Call] = []
        self.last_recap: str | None = None
        self._load()

    # -- state ------------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.calls = [Call(**c) for c in data.get("calls", [])]
            self.last_recap = data.get("last_recap")
        except (OSError, ValueError, TypeError):
            self.calls = []

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"calls": [asdict(c) for c in self.calls],
                                               "last_recap": self.last_recap}, indent=1), encoding="utf-8")

    # -- the call ---------------------------------------------------------------------------------
    def report(self, snap: TokenSnapshot, now: datetime, lane: str, why: str, size_usd: float, price: float,
               safety: SafetyReport | None, unchecked: bool, description: str | None, source: str) -> dict:
        age = snap.age_minutes(now)
        age_text = "?" if age is None else (f"{age:.0f}m" if age < 120 else f"{age / 60:.1f}h")
        ratio = snap.buy_sell_ratio_h1
        links = [f"[DexScreener]({snap.url})"] if snap.url else []
        if gmgn := GMGN_CHAIN.get(snap.chain):
            links.append(f"[GMGN](https://gmgn.ai/{gmgn}/token/{snap.mint})")
        links += [f"[link]({u})" for u in snap.links[:4]]

        if unchecked:
            checks = (f"**UNCHECKED.** No honeypot or tax check exists for {snap.chain}. This coin may be impossible "
                      f"to sell. Paper buy capped at ${size_usd:.0f}.")
        elif safety is not None:
            checks = f"Passed every safety check the agent runs on {snap.chain}"
            if safety.score is not None:
                checks += f" (score {safety.score:.2f})"
        else:
            checks = "Passed the lane's checks"

        body = []
        if unchecked:
            body.append(f"**WARNING: UNCHECKED CHAIN ({snap.chain.upper()}).** No safety check was run.")
        body.append(_cut(description, 1500) if description else "_No description found._")
        if description and source:
            body.append(f"_Description: {source}_")

        fields = [
            {"name": "Contract", "value": f"`{snap.mint}`", "inline": False},
            {"name": "Chain / DEX", "value": f"{snap.chain} / {snap.dex_id or '?'} ({snap.quote_symbol} pair)", "inline": True},
            {"name": "Market cap", "value": _usd(snap.market_cap_usd), "inline": True},
            {"name": "Liquidity", "value": _usd(snap.liquidity_usd), "inline": True},
            {"name": "Age", "value": age_text, "inline": True},
            {"name": "Volume 5m / 1h", "value": f"{_usd(snap.volume_m5)} / {_usd(snap.volume_h1)}", "inline": True},
            {"name": "Txns 1h", "value": f"{snap.buys_h1} buys / {snap.sells_h1} sells ({ratio:.2f}x)", "inline": True},
            {"name": "Price change", "value": f"5m {_pct(snap.price_change_m5)} | 1h {_pct(snap.price_change_h1)} | "
                                              f"6h {_pct(snap.price_change_h6)}", "inline": False},
            {"name": "Safety", "value": _cut(checks, 1024), "inline": False},
            {"name": "Why it was called", "value": _cut(why or f"{lane} lane", 1024), "inline": False},
            {"name": "Paper buy", "value": f"${size_usd:.2f} at ${price:.10g}", "inline": True},
        ]
        if links:
            fields.append({"name": "Links", "value": _cut(" | ".join(links), 1024), "inline": False})
        title = f"{'UNCHECKED CALL' if unchecked else 'CALL'}: {snap.symbol}" + (f" ({snap.name})" if snap.name else "")
        return {"title": _cut(title, 256), "url": snap.url or None, "description": _cut("\n\n".join(body), 4096),
                "color": ORANGE if unchecked else GREEN, "fields": fields,
                "footer": {"text": "Paper trade. Not financial advice."}, "timestamp": now.isoformat()}

    def call(self, snap: TokenSnapshot, now: datetime, lane: str, why: str, size_usd: float, price: float,
             safety: SafetyReport | None = None, unchecked: bool = False) -> bool:
        if any(c.mint == snap.mint and c.chain == snap.chain for c in self.calls):
            return False  # already called and still being followed
        description, source = (None, "")
        if self.cfg.describe and self.describer is not None:
            description, source = self.describer.describe(snap)
        embed = self.report(snap, now, lane, why, size_usd, price, safety, unchecked, description, source)
        ok = self.webhook.post(embeds=[embed])
        self.calls.append(Call(snap.mint, snap.symbol, snap.chain, now.isoformat(), price, snap.market_cap_usd,
                               lane, unchecked))
        self._save()
        if ok:
            log.info("Called %s (%s) on Discord", snap.symbol, snap.chain)
        return ok

    # -- follow-ups and recap ---------------------------------------------------------------------
    def tick(self, now: datetime, fetch: Callable[[list[str]], dict[str, TokenSnapshot]]) -> None:
        due: list[tuple[Call, float]] = []
        for c in self.calls:
            age = (now - datetime.fromisoformat(c.called_at)).total_seconds() / 60
            for m in map(float, self.cfg.follow_up_minutes):
                if age >= m and m not in c.done:
                    due.append((c, m))
        if due:
            snaps = fetch(list(dict.fromkeys(c.mint for c, _ in due)))
            embeds = []
            for c, m in due:
                c.done.append(m)
                s = snaps.get(c.mint)
                if s is None or s.price_usd <= 0 or c.entry_price <= 0:
                    embeds.append({"title": f"{c.symbol}: {self._label(m)} follow-up", "color": GREY,
                                   "description": "No price found (pool gone or delisted)."})
                    continue
                pct = (s.price_usd / c.entry_price - 1) * 100
                c.moves[str(m)] = round(pct, 2)
                c.peak_pct = max(c.peak_pct, pct)
                embeds.append({
                    "title": f"{c.symbol}: {_pct(pct)} {self._label(m)} after the call", "url": s.url or None,
                    "color": GREEN if pct >= 0 else ORANGE,
                    "description": f"Market cap {_usd(c.mcap0)} -> {_usd(s.market_cap_usd)} | liquidity "
                                   f"{_usd(s.liquidity_usd)} | best of the checks so far {_pct(c.peak_pct)}"
                                   + (" | UNCHECKED chain" if c.unchecked else "")})
            for i in range(0, len(embeds), 10):
                self.webhook.post(embeds=embeds[i : i + 10])
            self._save()
        self._recap(now)
        self._prune(now)

    @staticmethod
    def _label(minutes: float) -> str:
        return f"{minutes:g}m" if minutes < 60 else f"{minutes / 60:g}h"

    def _recap(self, now: datetime) -> None:
        h = self.cfg.recap_hour_utc
        today = now.date().isoformat()
        if h is None or now.hour != h or self.last_recap == today:
            return
        self.last_recap = today
        since = now - timedelta(hours=24)
        day = [c for c in self.calls if datetime.fromisoformat(c.called_at) >= since]
        if not day:
            self.webhook.post(embeds=[{"title": "Daily recap: no calls in the last 24h", "color": BLUE}])
            self._save()
            return
        key = str(60.0) if any("60.0" in c.moves for c in day) else None
        hour = [c.moves[key] for c in day if key and key in c.moves]
        best = max(day, key=lambda c: c.peak_pct)
        lines = [f"Calls: **{len(day)}** ({sum(c.unchecked for c in day)} unchecked)",
                 f"Up 50%+ at a check: **{sum(c.peak_pct >= 50 for c in day)}** | doubled: "
                 f"**{sum(c.peak_pct >= 100 for c in day)}**"]
        if hour:
            lines.append(f"1h after the call: median {_pct(statistics.median(hour))}, "
                         f"{sum(x > 0 for x in hour)} of {len(hour)} up")
        lines.append(f"Best: {best.symbol} ({best.chain}) {_pct(best.peak_pct)}")
        self.webhook.post(embeds=[{"title": "Daily recap: last 24h of calls", "color": BLUE,
                                   "description": "\n".join(lines), "timestamp": now.isoformat()}])
        self._save()

    def _prune(self, now: datetime) -> None:
        keep_for = timedelta(minutes=max(self.cfg.follow_up_minutes, default=0)) + timedelta(hours=26)
        before = len(self.calls)
        self.calls = [c for c in self.calls if now - datetime.fromisoformat(c.called_at) <= keep_for]
        if len(self.calls) != before:
            self._save()

