"""MEME OS (memeos.world) developer API: X-Ray reports as a buy check, bundle clusters for Discord calls.

- X-Ray (5 credits): verdict, score 0-100, mint/freeze authorities, top holders, creator history and flags. Run only
  on coins that already passed every free check, just before a buy; a verdict in memeos.reject_verdicts (or a score
  under memeos.min_score, when set) skips the coin. X-Ray caps new, thin coins at CAUTION (a missing red flag is not
  proof of safety), so CAUTION does not block.
- Bundle (35 credits): wallets funded from the same source among the top holders, and the share of supply they
  hold. Read only for Discord calls, within memeos.max_bundle_per_day.

Credits are capped per day here, and when MEME OS answers 402 (out of credits) the other safety checks decide.
The key comes from the MEMEOS_API_KEY environment variable (or memeos.api_key_file), never from the config.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import MemeosConfig
from ..models import SafetyReport

log = logging.getLogger(__name__)

XRAY_CREDITS, BUNDLE_CREDITS = 5, 35


def load_api_key(cfg: MemeosConfig) -> str | None:
    key = os.environ.get(cfg.api_key_env, "").strip()
    if key:
        return key
    if cfg.api_key_file:
        try:
            return Path(cfg.api_key_file).expanduser().read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


class MemeosError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"MEME OS HTTP {status}: {message}")
        self.status = status


@dataclass
class Xray:
    verdict: str
    score: float | None
    flags: list[str] = field(default_factory=list)
    mint_authority: str | None = None
    freeze_authority: str | None = None
    url: str = ""

    def describe(self) -> str:
        score = f" {self.score:g}/100" if self.score is not None else ""
        flags = f" ({', '.join(f.replace('_', ' ') for f in self.flags[:4])})" if self.flags else ""
        return f"MEME OS X-Ray {self.verdict}{score}{flags}"


@dataclass
class Bundle:
    linked_supply_pct: float | None
    clusters: int
    biggest_wallets: int | None
    biggest_pct: float | None


class MemeosClient:
    def __init__(self, api_key: str, base_url: str = "https://memeos.world", http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0, headers={"Accept": "application/json"})
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.credits_left: float | None = None

    def get(self, path: str) -> dict[str, Any]:
        try:
            resp = self.http.get(f"{self.base_url}{path}", headers=self.headers)
        except httpx.HTTPError as exc:  # re-raised without request details: they carry the key header
            raise MemeosError(0, f"request failed ({type(exc).__name__})") from None
        left = resp.headers.get("x-memeos-credits-remaining")
        if left is not None:
            try:
                self.credits_left = float(left)
            except ValueError:
                pass
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            err = (body.get("error") or {}) if isinstance(body, dict) else {}
            raise MemeosError(resp.status_code, str(err.get("message") or resp.reason_phrase)[:200])
        return (body.get("data") or {}) if isinstance(body, dict) else {}

    def account(self) -> dict[str, Any]:
        return self.get("/api/public/v1/account")

    def xray(self, address: str) -> dict[str, Any]:
        return self.get(f"/api/public/v1/xray/{address}")

    def bundle(self, address: str) -> dict[str, Any]:
        return self.get(f"/api/public/v1/bundle/{address}")


def parse_xray(address: str, d: dict[str, Any]) -> Xray | None:
    verdict = d.get("verdict")
    if not verdict:
        return None
    auth = d.get("authorities") if isinstance(d.get("authorities"), dict) else {}
    try:
        score = float(d["score"]) if d.get("score") is not None else None
    except (TypeError, ValueError):
        score = None
    flags = [str(f) for f in d.get("flags") or [] if f]
    return Xray(str(verdict).upper(), score, flags, auth.get("mint"), auth.get("freeze"),
                f"https://memeos.world/xray/{address}")


def parse_bundle(d: dict[str, Any]) -> Bundle:
    clusters = [c for c in d.get("clusters") or [] if isinstance(c, dict)]
    big = max(clusters, key=lambda c: float(c.get("supplyPct") or 0), default=None)

    def num(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return Bundle(num(d.get("linkedSupplyPct")), len(clusters), int(big["wallets"]) if big and big.get("wallets") else None,
                  num(big.get("supplyPct")) if big else None)


class MemeosGate:
    """X-Ray before a buy, within a daily credit budget; bundle reads for calls. Never raises."""

    def __init__(self, cfg: MemeosConfig, client: MemeosClient, clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], str] = lambda: datetime.now(timezone.utc).date().isoformat()) -> None:
        self.cfg, self.client, self.clock, self.today = cfg, client, clock, today
        self.reports: dict[str, Xray] = {}  # address -> last X-Ray (the Discord call reads it, no extra credits)
        self._expires: dict[str, float] = {}
        self._used: dict[tuple[str, str], int] = {}  # (day, "xray" / "bundle") -> calls
        self._off: dict[str, str] = {}  # "all" / "xray" / "bundle" -> the day it was switched off
        self._warned: set[str] = set()

    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def _budget(self, kind: str, limit: int) -> bool:
        day = self.today()
        if day in (self._off.get("all"), self._off.get(kind)):
            return False
        used = self._used.get((day, kind), 0)
        if used >= limit:
            self._warn_once(f"{day}{kind}", "MEME OS %s: today's cap of %d reached; the other checks decide", kind, limit)
            return False
        self._used[(day, kind)] = used + 1
        return True

    def _failed(self, exc: MemeosError, kind: str) -> None:
        day = self.today()
        if exc.status in (401, 402):
            self._off["all"] = day
            why = "the key was refused" if exc.status == 401 else "out of credits (or the key's monthly cap)"
            self._warn_once(f"{day}all", "MEME OS off for today: %s; the other safety checks decide", why)
        elif exc.status == 403:
            self._off[kind] = day
            self._warn_once(f"{day}{kind}403", "MEME OS %s off for today: the key lacks that permission", kind)
        elif exc.status == 400:
            log.debug("MEME OS %s: address not supported (%s)", kind, exc)  # e.g. a chain X-Ray doesn't read
        else:
            log.warning("MEME OS %s failed: %s", kind, exc)

    def xray(self, address: str) -> Xray | None:
        if not self.cfg.xray:
            return None
        if address in self.reports and self._expires.get(address, 0) > self.clock():
            return self.reports[address]
        if not self._budget("xray", self.cfg.max_xray_per_day):
            return None
        try:
            report = parse_xray(address, self.client.xray(address))
        except MemeosError as exc:
            self._failed(exc, "xray")
            return None
        self._check_balance()
        if report is not None:
            self.reports[address] = report
            self._expires[address] = self.clock() + self.cfg.cache_minutes * 60
            if len(self.reports) > 2000:
                self.reports = dict(list(self.reports.items())[-1000:])
        return report

    def _check_balance(self) -> None:
        """Switch X-Ray off for the day once the balance can't pay for another (the free checks are used up)."""
        left = self.client.credits_left
        if left is not None and left < XRAY_CREDITS:
            self._off["all"] = self.today()
            self._warn_once(f"{self.today()}empty", "MEME OS credits used up (%g left): the other safety checks "
                            "decide each coin", left)

    def start(self) -> float | None:
        """Read the balance (free) once at startup; None if MEME OS can't be reached."""
        try:
            balance = float(self.client.account().get("balance"))
        except (MemeosError, TypeError, ValueError) as exc:
            log.warning("MEME OS balance check failed (%s); the other safety checks decide when X-Ray can't answer", exc)
            return None
        self.client.credits_left = balance
        self._check_balance()
        return balance

    def check(self, address: str) -> SafetyReport:
        """SafetyChecker: a bad X-Ray verdict rejects; no verdict (out of credits, down, unsupported) passes it on
        to the other checks."""
        why = self.block_reason(address)
        return SafetyReport(ok=why is None, score=None, reasons=[why[len("unsafe: "):]] if why else [])

    def block_reason(self, address: str) -> str | None:
        """Why X-Ray says not to buy (None: buy, or X-Ray had nothing to say)."""
        x = self.xray(address)
        if x is None:
            return None
        c = self.cfg
        if x.verdict in {v.upper() for v in c.reject_verdicts}:
            return f"unsafe: {x.describe()} ({x.url})"
        if c.min_score is not None and x.score is not None and x.score < c.min_score:
            return f"unsafe: {x.describe()}: score under {c.min_score:g} ({x.url})"
        bad = sorted(set(x.flags) & set(c.reject_flags))
        if bad:
            return f"unsafe: MEME OS X-Ray flags {', '.join(bad)} ({x.url})"
        return None

    def bundle(self, address: str) -> Bundle | None:
        left = self.client.credits_left
        if not self.cfg.bundle_on_calls or (left is not None and left < max(self.cfg.bundle_min_credits, BUNDLE_CREDITS)):
            return None  # credits are kept for the buy checks
        if not self._budget("bundle", self.cfg.max_bundle_per_day):
            return None
        try:
            return parse_bundle(self.client.bundle(address))
        except MemeosError as exc:
            self._failed(exc, "bundle")
            return None
