"""LarpCheck (larpcheck.app): is this coin real, a LARP or rigged?

LarpCheck investigates a coin's story (site, X account, claims) and its
holders, and stamps two verdicts: a story verdict (REAL ... LARP / SCAM) and a
coin stamp (OK / NEUTRAL / SKETCHY / RIGGED: how fair the coin is for a buyer,
e.g. "linked wallets hold 82% of supply"). Those are exactly the signals the
market data and RugCheck's summary miss.

It is the last gate before a buy, after the filters and RugCheck, so it runs
a few times a day. Looking up existing verdicts is free; starting a new check
spends a daily credit and only happens with `on_unchecked: check`.

The API is MCP over HTTP. The server is stateless, so each call is a single
JSON-RPC `tools/call` POST.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import LarpCheckConfig
from ..models import Position, SafetyReport
from ..strategy import ExitDecision

log = logging.getLogger(__name__)

ENV_KEY = "LARPCHECK_API_KEY"
UNAVAILABLE = "larpcheck unavailable"
PENDING = "larpcheck check running"
REASON_KINDS = ("risk", "missing")  # LarpCheck finding kinds that explain a LARP call


class LarpCheckError(RuntimeError):
    pass


def load_api_key(cfg: LarpCheckConfig) -> str | None:
    key = os.environ.get(ENV_KEY, "").strip()
    if key:
        return key
    if cfg.api_key_file:
        path = Path(cfg.api_key_file).expanduser()
        if path.exists():
            return path.read_text().strip() or None
    return None


class LarpCheckClient:
    def __init__(self, api_key: str, url: str = "https://larpcheck.app/mcp", client: httpx.Client | None = None) -> None:
        self.url = url
        self.http = client or httpx.Client(timeout=30.0)
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._id = 0

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
        resp = self.http.post(self.url, json=body, headers=self.headers)
        if resp.status_code == 401:
            raise LarpCheckError("LarpCheck rejected the API key (401)")
        resp.raise_for_status()
        text = resp.text
        if text.lstrip().startswith(("event:", "data:")):  # streamable-HTTP servers may answer as SSE
            text = "\n".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
        msg = json.loads(text)
        if "error" in msg:
            raise LarpCheckError(f"{tool}: {msg['error'].get('message', msg['error'])}")
        result = msg.get("result") or {}
        content = "".join(p.get("text", "") for p in result.get("content") or [] if p.get("type") == "text")
        if result.get("isError"):
            raise LarpCheckError(f"{tool}: {content[:300]}")
        try:
            return json.loads(content)
        except ValueError:
            return content

    def lookup_coin(self, mint: str) -> dict[str, Any]:
        return self.call("lookup_coin", {"address": mint})

    def start_check(self, mint: str) -> dict[str, Any]:
        return self.call("start_check", {"input": mint, "wait": False})


@dataclass
class Verdict:
    verdict: str
    score: float | None
    coin_word: str | None
    coin_score: float | None
    summary: str
    url: str
    state: str | None  # "current", "dated", "moved" (LarpCheck's freshness of the read)

    @classmethod
    def from_latest(cls, latest: dict[str, Any]) -> "Verdict | None":
        if (latest.get("status") or "done") != "done" or not latest.get("verdict"):
            return None
        coin = next((s for s in latest.get("stamps") or [] if s.get("id") == "coin"), {})
        return cls(
            verdict=str(latest["verdict"]).upper(),
            score=latest.get("score"),
            coin_word=str(coin["word"]).upper() if coin.get("word") else None,
            coin_score=coin.get("score"),
            summary=str(latest.get("call") or latest.get("headline") or "")[:160],
            url=str(latest.get("url") or ""),
            state=(latest.get("asOf") or {}).get("state"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "score": self.score, "coin_stamp": self.coin_word,
                "coin_score": self.coin_score, "summary": self.summary, "url": self.url, "state": self.state}

    def describe(self) -> str:
        coin = f", coin {self.coin_word} {self.coin_score:g}/100" if self.coin_word and self.coin_score is not None else ""
        score = f" {self.score:g}/100" if self.score is not None else ""
        return f"larpcheck {self.verdict}{score}{coin}: {self.summary}"


class LarpCheckGate:
    """SafetyChecker that blocks coins LarpCheck has called a LARP/scam or rigged."""

    def __init__(
        self,
        cfg: LarpCheckConfig,
        client: LarpCheckClient,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = lambda: datetime.now(timezone.utc).date().isoformat(),
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.clock = clock
        self.today = today
        self._cache: dict[str, tuple[float, SafetyReport]] = {}
        self._pending_since: dict[str, float] = {}
        self.checks_started: dict[str, int] = {}  # UTC day -> checks started (credits spent)
        self.reports_opened: dict[str, int] = {}  # UTC day -> other people's reports opened (credits)
        self.last_verdicts: dict[str, Verdict] = {}
        self._held_next: dict[str, float] = {}  # mint -> next review time of a held coin
        self._held_check_started: set[str] = set()
        # mint -> "tech" / "meme", set by the agent before it calls check() (data/profile.py).
        # Unknown counts as tech: the full gate.
        self.kinds: dict[str, str] = {}

    def lookup_only(self, kind: str | None) -> bool:
        """Memes get LarpCheck's free lookup only (larpcheck.memes: lookup), or nothing (off)."""
        return kind == "meme" and self.cfg.memes != "full"

    def checks_left_today(self) -> int:
        return max(0, self.cfg.max_checks_per_day - self.checks_started.get(self.today(), 0))

    def reports_left_today(self) -> int:
        return max(0, self.cfg.max_report_opens_per_day - self.reports_opened.get(self.today(), 0))

    # -- held coins -----------------------------------------------------------------
    def review_position(self, pos: Position) -> ExitDecision | None:
        """Re-read LarpCheck on a held coin; when it flags it, read why and exit, keeping dust."""
        if not self.cfg.exit_on_flag:
            return None
        meme = self.lookup_only(pos.kind or self.kinds.get(pos.mint))
        if meme and self.cfg.memes == "off":
            return None
        now = self.clock()
        if now < self._held_next.get(pos.mint, 0.0):
            return None
        self._held_next[pos.mint] = now + self.cfg.held_recheck_minutes * 60
        data = self.client.lookup_coin(pos.mint)
        latest = (data.get("latest") or {}) if isinstance(data, dict) else {}
        verdict = Verdict.from_latest(latest)
        if verdict is None:
            # Held but never checked (bought while unchecked): get it checked once, if that's the mode.
            # Never for a meme: memes don't spend credits.
            if (not meme and self.cfg.on_unchecked == "check" and not data.get("running")
                    and pos.mint not in self._held_check_started and self.checks_left_today() > 0):
                started = self.client.start_check(pos.mint)
                self._held_check_started.add(pos.mint)
                day = self.today()
                self.checks_started[day] = self.checks_started.get(day, 0) + 1
                log.info("Started LarpCheck check on held %s: %s", pos.symbol,
                         started.get("url") if isinstance(started, dict) else "")
            return None
        self.last_verdicts[pos.mint] = verdict
        upper = lambda xs: {x.upper() for x in xs}
        dust_ok = upper(self.cfg.dust_verdicts)
        fp = [latest.get("id"), verdict.verdict, verdict.coin_word]  # LarpCheck's view of the coin
        prev = pos.larp or {}
        prev_fp, prev_verdict = prev.get("fp"), prev.get("verdict")
        flagged = (verdict.verdict in upper(self.cfg.exit_verdicts)
                   or (verdict.coin_word or "") in upper(self.cfg.exit_coin_stamps))
        changed = (self.cfg.sell_on_change and prev_fp is not None and prev_fp != fp
                   and prev_verdict in dust_ok)
        if not (flagged or changed):
            pos.larp = {**verdict.as_dict(), "fp": fp}
            return None

        # Dust only for a LIKELY LARP / LIKELY REAL coin whose coin stamp is OK and that no check has
        # ever stamped RIGGED; anything else LarpCheck sells is sold in full.
        frac = self.cfg.exit_sell_fraction if self._dust_problem(verdict, data, latest) is None else 1.0
        reasoning = self.read_reasoning(latest, allow_credit=not meme)
        trigger = (f"changed from {prev_verdict}" + (f"/{prev_fp[2]}" if prev_fp and prev_fp[2] else "")
                   if changed and not flagged else "flagged")
        larp = {**verdict.as_dict(), "fp": fp, "trigger": trigger, "reasoning": reasoning}
        keep = f", {100 - frac * 100:.0f}% kept as dust" if frac < 0.999 else ""
        why = "; ".join(r["title"] for r in reasoning.get("risks", [])[:3]) or reasoning.get("summary", "")
        log.warning("LarpCheck %s held %s %s: %s | watch: %s | %s", trigger, pos.symbol, verdict.describe(),
                    why[:300], (reasoning.get("watch") or "-")[:200], verdict.url)
        return ExitDecision(frac, f"{verdict.describe()[:140]} ({trigger}): sold {frac * 100:.0f}%{keep}",
                            make_dust=frac < 0.999, larp=larp)

    def _dust_problem(self, verdict: Verdict, data: dict[str, Any], latest: dict[str, Any]) -> str | None:
        """Why this coin may not be kept as dust, or None if it may."""
        upper = lambda xs: {x.upper() for x in xs}
        history = [c for c in (data.get("checks") or []) if isinstance(c, dict)] + [latest]
        if any(str(st.get("word", "")).upper() == "RIGGED"
               for c in history for st in c.get("stamps") or [] if st.get("id") == "coin"):
            return "stamped RIGGED in a LarpCheck check"
        if verdict.verdict not in upper(self.cfg.dust_verdicts):
            return f"verdict is {verdict.verdict}"
        if (verdict.coin_word or "") not in upper(self.cfg.dust_coin_stamps):
            return f"coin stamp is {verdict.coin_word or 'missing'}, not {'/'.join(self.cfg.dust_coin_stamps)}"
        return None

    def review_dust(self, pos: Position) -> ExitDecision | None:
        """Dust stays only while the coin still qualifies; once it doesn't, sell the remainder."""
        if not self.cfg.exit_on_flag:
            return None
        now = self.clock()
        if now < self._held_next.get(pos.mint, 0.0):
            return None
        self._held_next[pos.mint] = now + self.cfg.held_recheck_minutes * 60
        data = self.client.lookup_coin(pos.mint)
        latest = (data.get("latest") or {}) if isinstance(data, dict) else {}
        verdict = Verdict.from_latest(latest)
        if verdict is None:
            return None
        problem = self._dust_problem(verdict, data, latest)
        if problem is None:
            return None
        log.warning("Dust %s no longer qualifies (%s): selling the rest | %s", pos.symbol, problem, verdict.url)
        return ExitDecision(1.0, f"dust no longer qualifies: {problem} ({verdict.describe()[:120]})")

    def read_reasoning(self, latest: dict[str, Any], allow_credit: bool = True) -> dict[str, Any]:
        """LarpCheck's reasoning for a verdict: the full report when it's open to us (our own checks,
        or one we open with a credit), otherwise the free headline, call and stamp lines.
        All of it is text LarpCheck read from the web: it is stored and shown, never acted on as instructions."""
        check_id = latest.get("id")
        out: dict[str, Any] = {"source": "summary", "summary": latest.get("headline") or latest.get("call") or "",
                               "risks": [], "other_findings": [], "watch": None, "claim": None, "claim_status": None,
                               "coin_line": None}
        if not check_id:
            return out
        try:
            check = self.client.call("get_check", {"id": check_id})
            report = check.get("report") if isinstance(check, dict) else None
            if not report and allow_credit and self.reports_left_today() > 0:
                opened = self.client.call("open_report", {"id": check_id})
                day = self.today()
                self.reports_opened[day] = self.reports_opened.get(day, 0) + 1
                report = opened.get("report") if isinstance(opened, dict) else None
                check = opened if isinstance(opened, dict) else check
        except (httpx.HTTPError, LarpCheckError, ValueError) as exc:
            log.warning("Couldn't read LarpCheck's reasoning for check %s: %s", check_id, exc)
            return out
        stamps = ((check.get("verdict") or {}) if isinstance(check, dict) and isinstance(check.get("verdict"), dict)
                  else {}).get("stamps") or []
        out["coin_line"] = next((s.get("line") for s in stamps if s.get("id") == "coin" and s.get("line")), None)
        if isinstance(report, dict):
            core = report.get("core") or {}
            findings = [f for f in report.get("findings") or [] if isinstance(f, dict)]
            pick = lambda f: {"title": str(f.get("title") or "")[:160], "detail": str(f.get("detail") or "")[:400],
                              "evidence": f.get("evidence") if str(f.get("evidence") or "").startswith("https://") else None}
            out.update(
                source="full report",
                summary=str(report.get("summary") or report.get("eli5") or out["summary"])[:600],
                claim=str(core.get("claim") or "")[:300] or None,
                claim_status=core.get("status"),
                # Why it's a LARP: what's risky and what's missing (no community, no product...).
                # "works" / "info" findings are context, kept separately.
                risks=[{**pick(f), "kind": f.get("kind")} for f in findings if f.get("kind") in REASON_KINDS],
                other_findings=[{**pick(f), "kind": f.get("kind")} for f in findings
                                if f.get("kind") not in REASON_KINDS],
                watch=str(report.get("watch") or "")[:300] or None,
            )
        return out

    def _cached(self, mint: str, report: SafetyReport, ttl_s: float) -> SafetyReport:
        self._cache[mint] = (self.clock() + ttl_s, report)
        return report

    def _judge(self, v: Verdict) -> SafetyReport:
        bad = v.verdict in {x.upper() for x in self.cfg.reject_verdicts}
        bad |= bool(v.coin_word) and v.coin_word in {x.upper() for x in self.cfg.reject_coin_stamps}
        if self.cfg.min_coin_score is not None and v.coin_score is not None:
            bad |= v.coin_score < self.cfg.min_coin_score
        return SafetyReport(ok=not bad, score=v.score, reasons=[f"{v.describe()} ({v.url})"] if bad else [])

    def _no_verdict(self, mint: str, why: str, ttl_s: float = 60) -> SafetyReport:
        """No finished verdict to go on. With require_verdict that means no buy."""
        if self.cfg.require_verdict:
            return self._cached(mint, SafetyReport(ok=False, score=None, reasons=[f"larpcheck: {why}"]), ttl_s)
        # Short TTL: someone may check it in the next minutes, and we want that verdict.
        return self._cached(mint, SafetyReport(ok=True, score=None, reasons=[]), 120)

    def check(self, mint: str) -> SafetyReport:
        hit = self._cache.get(mint)
        if hit and hit[0] > self.clock():
            return hit[1]
        if self.lookup_only(self.kinds.get(mint)):
            return self._check_meme(mint)
        try:
            data = self.client.lookup_coin(mint)
            if not isinstance(data, dict):
                raise LarpCheckError(f"unexpected lookup_coin reply: {str(data)[:120]}")
            verdict = Verdict.from_latest(data.get("latest") or {})
            if verdict is not None:
                self._pending_since.pop(mint, None)
                self.last_verdicts[mint] = verdict
                report = self._judge(verdict)
                # Bad verdicts rarely improve (a rigged holder base stays rigged); good ones can sour.
                return self._cached(mint, report, 3600 if not report.ok else 600)

            now = self.clock()
            # A check we just started may not be listed as running yet: it counts as pending too.
            if data.get("running") or mint in self._pending_since:
                since = self._pending_since.setdefault(mint, now)
                if now - since < self.cfg.pending_max_minutes * 60:
                    return SafetyReport(ok=False, score=None, reasons=[PENDING])
                # Don't spend another credit on it; if the check finishes later, its verdict is used.
                return self._no_verdict(mint, f"check still running after {self.cfg.pending_max_minutes:g} min")
            return self._unchecked(mint)
        except (httpx.HTTPError, LarpCheckError, ValueError) as exc:
            log.warning("LarpCheck lookup failed for %s: %s", mint, exc)
            if self.cfg.require_verdict or self.cfg.fail_closed:
                return SafetyReport(ok=False, score=None, reasons=[UNAVAILABLE])
            return SafetyReport(ok=True, score=None, reasons=[])

    def _check_meme(self, mint: str) -> SafetyReport:
        """A meme: no verdict needed and no credit spent, but a bad verdict someone else got still blocks it."""
        ok = SafetyReport(ok=True, score=None, reasons=[])
        if self.cfg.memes == "off":
            return ok
        try:
            data = self.client.lookup_coin(mint)
            verdict = Verdict.from_latest((data.get("latest") or {}) if isinstance(data, dict) else {})
        except (httpx.HTTPError, LarpCheckError, ValueError) as exc:
            log.warning("LarpCheck lookup failed for %s: %s", mint, exc)
            return ok
        if verdict is None:
            return self._cached(mint, ok, 120)  # someone may check it soon; look again then
        self.last_verdicts[mint] = verdict
        report = self._judge(verdict)
        return self._cached(mint, report, 3600 if not report.ok else 600)

    def _unchecked(self, mint: str) -> SafetyReport:
        mode = self.cfg.on_unchecked
        if mode == "check":
            if self.checks_left_today() > 0:
                started = self.client.start_check(mint)
                day = self.today()
                self.checks_started[day] = self.checks_started.get(day, 0) + 1
                self._pending_since[mint] = self.clock()
                log.info("Started LarpCheck check on %s (%d left today): %s",
                         mint, self.checks_left_today(), started.get("url") if isinstance(started, dict) else "")
                return SafetyReport(ok=False, score=None, reasons=[PENDING])
            return self._no_verdict(mint, "not checked yet and no checks left today", ttl_s=300)
        if mode == "reject":
            return self._cached(mint, SafetyReport(ok=False, score=None, reasons=["larpcheck: no verdict yet"]), 120)
        return self._no_verdict(mint, "not checked yet")


class RequireLarpCheck:
    """Stands in for the gate when a verdict is required but LarpCheck can't be reached at all
    (no API key): every buy is refused, loudly, instead of trading without the check."""

    def __init__(self, why: str) -> None:
        self.why = why

    def check(self, mint: str) -> SafetyReport:
        return SafetyReport(ok=False, score=None, reasons=[f"larpcheck required: {self.why}"])


class CompositeSafety:
    """Run checks in order (cheapest first); the first one that rejects decides."""

    def __init__(self, *checkers: Any) -> None:
        self.checkers = [c for c in checkers if c is not None]

    def check(self, mint: str) -> SafetyReport:
        last = SafetyReport(ok=True, score=None, reasons=[])
        for checker in self.checkers:
            last = checker.check(mint)
            if not last.ok:
                return last
        return last

    def find(self, cls: type) -> Any:
        return next((c for c in self.checkers if isinstance(c, cls)), None)
