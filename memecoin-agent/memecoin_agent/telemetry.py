"""Why the agent isn't buying: every blocked candidate, by stage and reason, per scan and per lane.

Stages, in the order a candidate meets them:

    discovery   no candidates at all this scan
    filters     the entry filters (market cap, liquidity, age, volume...) and pre-checks (dumped, KOLs sold)
    safety      RugCheck, Jupiter, GMGN, MEME OS, the wallet scan, the honeypot round trip, chain checks
    larpcheck   a bad LarpCheck verdict, or no verdict under the strict policy
    regime      the market-regime gate (market falling)
    history     the scan-log track record (group rugs too often / returns too little)
    risk        entries paused or limited (kill switch, daily loss, max positions, balance, lane cap)
    execution   paper-only chains in live mode, preflight refusals, failed buys

Each blocked candidate is a row in state/rejections.jsonl (lane, chain, mint, rejection_stage, rejection_reason,
regime_state, history_verdict, score, intended_position_size...). The same coin blocked for the same reason again
within `dedupe_minutes` adds no row, so the file holds decisions, not one line per scan. Totals since the last
reset survive restarts (state/telemetry.json); the current scan's counters are kept in memory for the dashboard.
Only the buy path counts a buy: a blocked candidate is never a trade.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

STAGES = ("discovery", "filters", "safety", "larpcheck", "regime", "history", "risk", "execution")
ROTATE_BYTES = 20 * 1024 * 1024


def slug(text: str, fallback: str = "other") -> str:
    """A short machine-readable reason from a human one: 'liquidity $5,000 < $15,000' -> 'liquidity'."""
    head = re.split(r"[:(;$\d<>=]", (text or "").strip().lower(), maxsplit=1)[0]
    words = re.findall(r"[a-z][a-z/\-]*", head)[:3]
    return "_".join(w.replace("/", "_").replace("-", "_") for w in words) or fallback


def key(text: str, fallback: str = "other") -> str:
    """A filter reason (scanlog.reason_key) as a slug, numbers kept: 'b/s1h too low' -> 'b_s1h_too_low'."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or fallback


def classify(outcome: str) -> tuple[str, str]:
    """(stage, reason) for an outcome text from the copy / long-term lanes, which report in words."""
    o = (outcome or "").strip()
    low = o.lower()
    if low.startswith("unsafe: larpcheck"):
        return "larpcheck", slug(o.split(":", 1)[1], "larpcheck")
    if low.startswith("unsafe"):
        return "safety", slug(o.split(":", 1)[1] if ":" in o else o)
    if low.startswith("waiting: market falling"):
        return "regime", "market_falling"
    if low.startswith("skipped: track record"):
        return "history", "track_record"
    if low.startswith("waiting"):
        return "filters", slug(o.split(":", 1)[1] if ":" in o else o)
    if low.startswith("buy failed"):
        return "execution", "buy_failed"
    if "live trading is solana-only" in low or "paper only" in low:
        return "execution", "paper_only_chain"
    if low.startswith("skipped"):
        return "risk", slug(o.split(":", 1)[1] if ":" in o else o)
    return "filters", slug(o)


@dataclass
class Block:
    time: str
    lane: str
    chain: str
    mint: str
    symbol: str
    rejection_stage: str
    rejection_reason: str
    detail: str
    regime_state: str
    history_verdict: str
    score: float | None
    intended_position_size: float | None


def _empty_totals(now: str) -> dict[str, Any]:
    return {"since": now, "scans": 0, "empty_scans": 0, "entry_blocked_scans": 0, "bought": 0,
            "by_stage": {s: 0 for s in STAGES}, "by_lane": {}, "by_reason": {}}


class Telemetry:
    def __init__(self, state_dir: str | Path, dedupe_minutes: float = 60.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(state_dir)
        self.blocks_path = self.dir / "rejections.jsonl"
        self.state_path = self.dir / "telemetry.json"
        self.dedupe_s = dedupe_minutes * 60
        self.clock = clock
        self._recent: dict[tuple[str, str, str], float] = {}
        self.totals = self._load()
        self.last_scan: dict[str, Any] = self._new_scan(None)

    # -- persistence ---------------------------------------------------------------------------------
    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat(timespec="seconds")

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            base = _empty_totals(data.get("since") or self._now_iso())
            base.update({k: data[k] for k in base if k in data})
            base["by_stage"] = {**{s: 0 for s in STAGES}, **(data.get("by_stage") or {})}
            return base
        except (OSError, ValueError, AttributeError):
            return _empty_totals(self._now_iso())

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.totals, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def reset(self) -> None:
        """Zero the totals (the block log is kept; the report can still read its history)."""
        self.totals = _empty_totals(self._now_iso())
        self._recent.clear()
        self.save()

    # -- one scan ------------------------------------------------------------------------------------
    @staticmethod
    def _new_scan(at: str | None) -> dict[str, Any]:
        return {"at": at, "candidates": 0, "passed": 0, "bought": 0, "by_stage": {s: 0 for s in STAGES}, "by_lane": {}}

    def begin_scan(self, candidates: int, passed: int, entry_block: str | None = None) -> None:
        self.last_scan = self._new_scan(self._now_iso())
        self.last_scan.update(candidates=candidates, passed=passed)
        self.totals["scans"] += 1
        if candidates == 0 and not entry_block:
            self.totals["empty_scans"] += 1
            self.last_scan["by_stage"]["discovery"] += 1
            self.totals["by_stage"]["discovery"] += 1
        if entry_block:
            self.totals["entry_blocked_scans"] += 1

    def block(self, stage: str, reason: str, *, lane: str = "", chain: str = "", mint: str = "", symbol: str = "",
              detail: str = "", regime_state: str | None = None, history_verdict: str | None = None,
              score: float | None = None, intended_position_size: float | None = None) -> bool:
        """Count a blocked candidate; returns True when it was new (logged), False when a repeat within the window."""
        if stage not in STAGES:
            raise ValueError(f"unknown rejection stage {stage!r}")
        lane = lane or "momentum"
        scan = self.last_scan
        scan["by_stage"][stage] += 1
        scan["by_lane"].setdefault(lane, Counter())[stage] += 1
        now = self.clock()
        key = (mint, stage, reason)
        if mint and now - self._recent.get(key, -1e18) < self.dedupe_s:
            return False
        self._recent[key] = now
        if len(self._recent) > 50_000:
            cut = now - self.dedupe_s
            self._recent = {k: t for k, t in self._recent.items() if t > cut}
        t = self.totals
        t["by_stage"][stage] = t["by_stage"].get(stage, 0) + 1
        t["by_lane"].setdefault(lane, {}).setdefault(stage, 0)
        t["by_lane"][lane][stage] += 1
        rk = f"{stage}/{reason}"
        t["by_reason"][rk] = t["by_reason"].get(rk, 0) + 1
        row = Block(self._now_iso(), lane, chain, mint, symbol, stage, reason, detail[:300], regime_state or "",
                    history_verdict or "", None if score is None else round(score, 3),
                    None if intended_position_size is None else round(intended_position_size, 2))
        self._append(row)
        return True

    def bought(self, lane: str) -> None:
        self.last_scan["bought"] += 1
        self.totals["bought"] += 1

    def end_scan(self) -> None:
        self.last_scan["by_lane"] = {k: dict(v) for k, v in self.last_scan["by_lane"].items()}
        self.save()

    def _append(self, row: Block) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.blocks_path.exists() and self.blocks_path.stat().st_size > ROTATE_BYTES:
                os.replace(self.blocks_path, self.blocks_path.with_suffix(".jsonl.1"))
        except OSError:
            pass
        with self.blocks_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

    # -- readouts ------------------------------------------------------------------------------------
    def rows(self, hours: float | None = None) -> list[dict[str, Any]]:
        cut = None if hours is None else datetime.fromtimestamp(self.clock(), tz=timezone.utc) - timedelta(hours=hours)
        out = []
        for path in (self.blocks_path.with_suffix(".jsonl.1"), self.blocks_path):
            try:
                with path.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if cut is None or datetime.fromisoformat(r["time"]) >= cut:
                            out.append(r)
            except OSError:
                continue
        return out

    def count(self, stage: str, hours: float = 24) -> int:
        return sum(1 for r in self.rows(hours) if r.get("rejection_stage") == stage)

    def view(self) -> dict[str, Any]:
        """For the dashboard: this scan's counters and the totals since the last reset."""
        t = self.totals
        return {"last_scan": self.last_scan, "since": t["since"], "scans": t["scans"], "empty_scans": t["empty_scans"],
                "bought": t["bought"], "by_stage": t["by_stage"],
                "regime_blocked": t["by_stage"].get("regime", 0), "history_blocked": t["by_stage"].get("history", 0)}


def build_report(tel: Telemetry, hours: float | None = 24) -> str:
    rows = tel.rows(hours)
    span = f"last {hours:g}h" if hours is not None else "all recorded"
    t = tel.totals
    out = [f"Blocked candidates, {span}: {len(rows)} (one per coin and reason per {tel.dedupe_s / 60:g} min)", ""]
    by_stage = Counter(r["rejection_stage"] for r in rows)
    out.append("By stage:   " + "  ".join(f"{s} {by_stage.get(s, 0)}" for s in STAGES if s != "discovery"))
    lanes = sorted({r["lane"] for r in rows})
    if lanes:
        out += ["", "By lane and stage:"]
        width = max(len(x) for x in lanes)
        for lane in lanes:
            c = Counter(r["rejection_stage"] for r in rows if r["lane"] == lane)
            out.append(f"  {lane:<{width}}  " + "  ".join(f"{s} {c[s]}" for s in STAGES if c.get(s)))
    reasons = Counter(f"{r['rejection_stage']}/{r['rejection_reason']}" for r in rows)
    if reasons:
        out += ["", "Top reasons:"]
        out += [f"  {k:<40} {v}" for k, v in reasons.most_common(15)]
    for stage, label in (("regime", "Blocked by the market regime"), ("history", "Blocked by the track record")):
        group = [r for r in rows if r["rejection_stage"] == stage]
        if group:
            out += ["", f"{label}: {len(group)}"]
            for r in group[-5:]:
                extra = r["regime_state"] if stage == "regime" else r["history_verdict"]
                out.append(f"  {r['time'][11:16]} {r['symbol'] or r['mint'][:8]} ({r['lane']}): {extra or r['detail']}"[:160])
    out += ["", f"Since {t['since'][:16].replace('T', ' ')} UTC (survives restarts; reset with --reset): {t['scans']} scans, "
                f"{t['empty_scans']} with no candidates, {t['entry_blocked_scans']} with entries blocked, {t['bought']} bought"]
    return "\n".join(out)
