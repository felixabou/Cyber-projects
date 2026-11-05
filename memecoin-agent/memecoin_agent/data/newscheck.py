"""NewsCheck (newscheck.app): why a coin is moving, from X, the chain, the coin's site and the news.

Used three ways:
- a gate on every candidate the agent is about to buy: a coin whose rundown says RED FLAG / DEV EXIT, or
  whose legit-or-larp verdict is LARP / SCAM, is not bought;
- the narrative lane's entry signal: a young coin with a real story (viral post, meta, product, KOL push...)
  is bought earlier and at a lower market cap than the momentum filters allow;
- a discovery feed: the newest checks anyone ran (free), narrowed to coins with a strong story.

The free plan reads existing rundowns (30 calls a minute) but cannot run new checks, and a brand new coin
usually has none yet. With a paid plan the agent can also request a check for the best young coins
(narrative.max_new_checks_per_day); the result is read on a later scan, so a tick never waits on one.
Unchecked coins are not blocked (unless narrative.require_rundown); they just don't get the early entry.
Key: NEWSCHECK_API_KEY, or the file narrative.api_key_file. Make one free at newscheck.app/developers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import NarrativeConfig

log = logging.getLogger(__name__)

BASE = "https://newscheck.app/api/v1"
ENV_KEY = "NEWSCHECK_API_KEY"


def load_api_key(cfg: NarrativeConfig) -> str | None:
    key = os.environ.get(ENV_KEY, "").strip()
    if key:
        return key
    if cfg.api_key_file:
        path = Path(cfg.api_key_file).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


class NewsCheckError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"NewsCheck {status} {code}: {message}")
        self.status, self.code = status, code


@dataclass
class Rundown:
    ca: str
    chain: str
    symbol: str
    catalyst: str  # REAL NEWS, PRODUCT, LISTING, VIRAL POST, META, NARRATIVE, KOL PUSH, WHALE, NO NEWS, ...
    confidence: int | None
    summary: str
    larp_verdict: str  # REAL, LIKELY REAL, PARTLY REAL, LIKELY LARP, LARP, SCAM, UNCLEAR ("" if none)
    larp_score: int | None
    checked_at: datetime | None
    url: str

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "Rundown":
        lol = d.get("legit_or_larp") or {}
        narrative = lol.get("narrative") if isinstance(lol.get("narrative"), dict) else {}
        verdict = (lol.get("verdict") or narrative.get("verdict") or "")
        score = lol.get("total", lol.get("score"))
        checked = None
        if d.get("checked_at"):
            try:
                checked = datetime.fromisoformat(str(d["checked_at"]).replace("Z", "+00:00"))
            except ValueError:
                pass
        conf = d.get("confidence")
        return cls(ca=str(d.get("ca") or ""), chain=str(d.get("chain") or ""), symbol=str(d.get("symbol") or "?"),
                   catalyst=str(d.get("catalyst") or "").upper(), confidence=int(conf) if isinstance(conf, (int, float)) else None,
                   summary=str(d.get("summary") or ""), larp_verdict=str(verdict).upper(),
                   larp_score=int(score) if isinstance(score, (int, float)) else None, checked_at=checked,
                   url=str(d.get("url") or ""))


def judge(cfg: NarrativeConfig, r: Rundown, now: datetime | None = None) -> tuple[str, str]:
    """("block" | "strong" | "neutral", why) for a rundown."""
    now = now or datetime.now(timezone.utc)
    if r.catalyst in {c.upper() for c in cfg.block_catalysts}:
        return "block", f"{r.catalyst.lower()}: {r.summary}".strip(": ")
    if r.larp_verdict in {v.upper() for v in cfg.block_verdicts}:
        return "block", f"newscheck {r.larp_verdict} {r.larp_score}/100: {r.summary}"
    if r.larp_score is not None and r.larp_score < cfg.min_legit_score:
        return "block", f"newscheck legit score {r.larp_score}/100 < {cfg.min_legit_score}: {r.summary}"
    fresh = r.checked_at is None or (now - r.checked_at).total_seconds() <= cfg.max_rundown_age_hours * 3600
    if (fresh and r.catalyst in {c.upper() for c in cfg.strong_catalysts}
            and (r.confidence is None or r.confidence >= cfg.min_confidence)
            and (r.larp_score is None or r.larp_score >= cfg.min_legit_score)):
        return "strong", f"{r.catalyst.lower()} ({r.confidence if r.confidence is not None else '?'}% sure, " \
                         f"{r.larp_verdict.lower() or 'unrated'} {r.larp_score if r.larp_score is not None else '?'}/100): {r.summary}"
    return "neutral", f"{r.catalyst.lower() or 'no rundown'}: {r.summary}"


class NewsCheckClient:
    """Cached, rate-limited reads (free plan: 30 calls a minute) plus optional new checks."""

    def __init__(self, cfg: NarrativeConfig, key: str, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.http = http or httpx.Client(timeout=20.0, headers={"Accept": "application/json"})
        self.key = key
        self.clock, self.sleep = clock, sleep
        self._cache: dict[str, tuple[float, Rundown | None]] = {}
        self._calls: list[float] = []
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._requested: dict[str, float] = {}
        self._feed: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self.checks_left: int | None = None  # from /me; None until known

    # -- plumbing ------------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        if self.clock() < self._down_until:
            raise NewsCheckError(429, "backing_off", "waiting out a rate limit")
        with self._lock:  # keep under the plan's calls a minute (a margin below 30)
            now = self.clock()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.cfg.calls_per_minute:
                raise NewsCheckError(429, "local_budget", "this minute's call budget is used")
            self._calls.append(now)
        resp = self.http.request(method, f"{BASE}{path}", headers={"Authorization": f"Bearer {self.key}"}, **kw)
        if resp.status_code == 429:
            after = resp.headers.get("retry-after")
            self._down_until = self.clock() + (float(after) if after and after.replace(".", "").isdigit() else 30.0)
        if resp.status_code >= 400:
            try:
                err = (resp.json() or {}).get("error") or {}
            except ValueError:
                err = {}
            raise NewsCheckError(resp.status_code, str(err.get("code") or resp.status_code), str(err.get("message") or ""))
        return resp.json()

    # -- reads ---------------------------------------------------------------------------------
    def rundown(self, chain: str, ca: str) -> Rundown | None:
        """The latest rundown, or None when nobody has checked the coin. Raises NewsCheckError on failures."""
        key = f"{chain}:{ca}"
        hit = self._cache.get(key)
        if hit and hit[0] > self.clock():
            return hit[1]
        try:
            data = self._request("GET", f"/tokens/{key}")
        except NewsCheckError as exc:
            if exc.code == "not_checked" or exc.status == 404:
                self._cache[key] = (self.clock() + self.cfg.miss_ttl_s, None)
                return None
            raise
        r = Rundown.parse((data or {}).get("report") or {})
        self._cache[key] = (self.clock() + self.cfg.ttl_s, r)
        return r

    def latest(self) -> list[dict[str, Any]]:
        """The newest checks anyone ran (free), cached for a minute."""
        at, items = self._feed
        if at > self.clock():
            return items
        data = self._request("GET", "/checks/latest", params={"limit": self.cfg.feed_limit})
        items = list((data or {}).get("checks") or (data or {}).get("items") or (data or {}).get("tokens") or [])
        self._feed = (self.clock() + 60.0, items)
        return items

    def discover(self, now: datetime | None = None) -> list[str]:
        """Solana mints whose newest check shows a strong story."""
        out: list[str] = []
        for it in self.latest():
            if it.get("chain") != "solana" or not it.get("ca"):
                continue
            r = Rundown.parse(it)
            if judge(self.cfg, r, now)[0] == "strong":
                out.append(r.ca)
        return out

    # -- new checks (paid plans) ---------------------------------------------------------------
    def request_check(self, chain: str, ca: str) -> bool:
        """Ask for a fresh check (it finishes in 30-90 s; a later rundown() call reads it). True if requested."""
        key = f"{chain}:{ca}"
        now = self.clock()
        if key in self._requested and now - self._requested[key] < 3600:
            return False
        day = [t for t in self._requested.values() if now - t < 86400]
        if len(day) >= self.cfg.max_new_checks_per_day:
            return False
        if self.checks_left is None:
            try:
                me = self._request("GET", "/me")
                self.checks_left = int(((me or {}).get("account") or me or {}).get("checks_left") or 0)
            except (NewsCheckError, httpx.HTTPError, ValueError):
                self.checks_left = 0
        if not self.checks_left:
            return False
        try:
            self._request("POST", "/checks", json={"token": ca, "chain": chain, "wait": 0})
        except (NewsCheckError, httpx.HTTPError) as exc:
            log.info("NewsCheck: couldn't request a check for %s: %s", ca, exc)
            return False
        self._requested[key] = now
        self.checks_left -= 1
        self._cache.pop(key, None)
        self._cache[key] = (now + 45.0, None)  # look again once it should be done
        return True
