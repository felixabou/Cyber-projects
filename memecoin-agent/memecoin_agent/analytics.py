"""Data for improving the agent: what each buy looked like, and what each sold coin did afterwards.

- `entries.csv`: one row per buy with the token's numbers at that moment (liquidity, market cap,
  age, volume, buy pressure, momentum, lane, LarpCheck verdict). The trade journal alone only says
  "entry score 0.72".
- `after_exit.csv`: for AFTER_EXIT_HOURS after a position is fully sold, the coin keeps being
  priced; its highest and lowest price versus the sale price are written when the window ends.
  This answers "did it go up after we sold?" (stops too tight, hold too short).

`build_report` joins these with trades.csv into round trips (see `memecoin-agent report`).
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .models import TokenSnapshot
from .portfolio import read_trades_csv

log = logging.getLogger(__name__)

AFTER_EXIT_HOURS = 12.0
CHECK_EVERY_S = 60.0
ENTRY_COLS = ["time", "mint", "symbol", "lane", "score", "price_usd", "size_usd", "liquidity_usd", "market_cap_usd",
              "age_min", "volume_h1", "txns_h1", "buy_sell_h1", "buy_sell_m5", "chg_m5", "chg_h1", "larp_verdict",
              "larp_stamp", "kind", "holders"]
AFTER_COLS = ["mint", "symbol", "exit_time", "exit_price", "exit_reason", "high_pct", "low_pct", "last_pct",
              "minutes_to_high", "watched_minutes"]


def _append(path: Path, cols: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    if not new:
        with path.open(newline="") as fh:
            header = next(csv.reader(fh), [])
        if header != cols:  # columns were added in an update: rewrite the file with the new header
            old = _read(path)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
            os.replace(tmp, path)
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow(row)


def _read(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _time(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


@dataclass
class Watch:
    mint: str
    symbol: str
    exit_time: datetime
    exit_price: float
    reason: str
    high: float = 0.0
    low: float = 0.0
    last: float = 0.0
    high_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in ("mint", "symbol", "exit_price", "reason", "high", "low", "last")}
        d["exit_time"] = self.exit_time.isoformat()
        d["high_at"] = self.high_at.isoformat() if self.high_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Watch":
        return cls(d["mint"], d["symbol"], datetime.fromisoformat(d["exit_time"]), d["exit_price"], d["reason"],
                   d.get("high", 0.0), d.get("low", 0.0), d.get("last", 0.0),
                   datetime.fromisoformat(d["high_at"]) if d.get("high_at") else None)


class Analytics:
    def __init__(self, directory: str | Path, hours: float = AFTER_EXIT_HOURS) -> None:
        self.dir = Path(directory)
        self.entries_csv = self.dir / "entries.csv"
        self.after_csv = self.dir / "after_exit.csv"
        self.watch_file = self.dir / "watch.json"
        self.window = timedelta(hours=hours)
        self.watches: dict[str, Watch] = {}
        self._next_check: datetime | None = None
        try:
            self.watches = {w["mint"]: Watch.from_dict(w) for w in json.loads(self.watch_file.read_text())}
        except (OSError, ValueError, KeyError):
            pass

    # -- recording --------------------------------------------------------------------------
    def log_entry(self, snap: TokenSnapshot, lane: str, score: float, size_usd: float, price: float,
                  now: datetime, larp: Any = None, kind: str = "", holders: int | None = None) -> None:
        row = {
            "time": now.isoformat(), "mint": snap.mint, "symbol": snap.symbol, "lane": lane, "score": round(score, 3),
            "price_usd": price, "size_usd": size_usd, "liquidity_usd": round(snap.liquidity_usd),
            "market_cap_usd": round(snap.market_cap_usd), "age_min": round(snap.age_minutes(now) or 0, 1),
            "volume_h1": round(snap.volume_h1), "txns_h1": snap.txns_h1,
            "buy_sell_h1": round(snap.buy_sell_ratio_h1, 2), "buy_sell_m5": round(snap.buy_sell_ratio_m5, 2),
            "chg_m5": snap.price_change_m5, "chg_h1": snap.price_change_h1,
            "larp_verdict": getattr(larp, "verdict", "") or "", "larp_stamp": getattr(larp, "coin_word", "") or "",
            "kind": kind, "holders": "" if holders is None else holders,
        }
        try:
            _append(self.entries_csv, ENTRY_COLS, row)
        except OSError as exc:
            log.warning("Could not write %s: %s", self.entries_csv, exc)

    def watch(self, mint: str, symbol: str, price: float, now: datetime, reason: str) -> None:
        if price > 0:
            self.watches[mint] = Watch(mint, symbol, now, price, reason, high=price, low=price, last=price, high_at=now)
            self._save()

    def update(self, now: datetime, fetch: Callable[[list[str]], dict[str, TokenSnapshot]]) -> None:
        """Re-price the watched coins (about once a minute) and close out finished windows."""
        if not self.watches or (self._next_check and now < self._next_check):
            return
        self._next_check = now + timedelta(seconds=CHECK_EVERY_S)
        snaps = fetch(list(self.watches))
        for mint, w in list(self.watches.items()):
            s = snaps.get(mint)
            if s is not None and s.price_usd > 0 and s.liquidity_usd > 0:
                w.last = s.price_usd
                if s.price_usd > w.high:
                    w.high, w.high_at = s.price_usd, now
                w.low = min(w.low, s.price_usd)
            if now - w.exit_time >= self.window:
                self._finish(w, now)
                del self.watches[mint]
        self._save()

    def _finish(self, w: Watch, now: datetime) -> None:
        up = lambda p: round((p / w.exit_price - 1) * 100, 1)  # noqa: E731
        try:
            _append(self.after_csv, AFTER_COLS, {
                "mint": w.mint, "symbol": w.symbol, "exit_time": w.exit_time.isoformat(), "exit_price": w.exit_price,
                "exit_reason": w.reason, "high_pct": up(w.high), "low_pct": up(w.low), "last_pct": up(w.last),
                "minutes_to_high": round(((w.high_at or w.exit_time) - w.exit_time).total_seconds() / 60),
                "watched_minutes": round((now - w.exit_time).total_seconds() / 60)})
        except OSError as exc:
            log.warning("Could not write %s: %s", self.after_csv, exc)

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.watch_file.with_suffix(".tmp")
            tmp.write_text(json.dumps([w.to_dict() for w in self.watches.values()]))
            os.replace(tmp, self.watch_file)
        except OSError as exc:
            log.warning("Could not save %s: %s", self.watch_file, exc)


# -- the report ---------------------------------------------------------------------------------
def exit_kind(reason: str) -> str:
    r = reason.lower()
    if r.startswith("larpcheck"):
        return "larpcheck flag"
    for key in ("take profit", "stop loss", "trailing stop", "time stop", "liquidity pulled", "manual close",
                "initials out", "runner", "dip review", "manual sell"):
        if r.startswith(key):
            return key
    return r.split(" (")[0][:30] or "other"


@dataclass
class RoundTrip:
    mint: str
    symbol: str
    opened: datetime
    closed: datetime | None
    cost: float
    pnl: float
    kinds: list[str] = field(default_factory=list)  # exit kinds in order
    entry: dict[str, str] = field(default_factory=dict)
    after: dict[str, str] = field(default_factory=dict)

    @property
    def final_kind(self) -> str:
        return self.kinds[-1] if self.kinds else "open"

    @property
    def pct(self) -> float:
        return self.pnl / self.cost * 100 if self.cost else 0.0


def round_trips(trades_csv: str | Path | list[str | Path], entries_csv: str | Path | None = None,
                after_csv: str | Path | None = None) -> list[RoundTrip]:
    open_: dict[str, RoundTrip] = {}
    held: dict[str, float] = {}
    done: list[RoundTrip] = []
    files = trades_csv if isinstance(trades_csv, list) else [trades_csv]
    for t in sorted((t for f in files for t in read_trades_csv(f)), key=lambda t: t.time):
        if t.side == "buy":
            open_[t.mint] = RoundTrip(t.mint, t.symbol, t.time, None, t.usd_value, 0.0)
            held[t.mint] = t.token_amount
        elif t.mint in open_:
            rt = open_[t.mint]
            rt.pnl += t.realized_pnl_usd
            rt.kinds.append(exit_kind(t.reason))
            held[t.mint] -= t.token_amount
            if held[t.mint] <= 1e-9 * max(t.token_amount, 1):
                rt.closed = t.time
                done.append(rt)
                del open_[t.mint]
    entries = [(e, _time(e["time"])) for e in _read(Path(entries_csv))] if entries_csv else []
    afters = _read(Path(after_csv)) if after_csv else []
    for rt in done:
        near = [e for e, et in entries if e["mint"] == rt.mint and et and abs((et - rt.opened).total_seconds()) < 120]
        rt.entry = near[0] if near else {}
        for a in afters:
            at = _time(a["exit_time"])
            if a["mint"] == rt.mint and at and rt.closed and abs((at - rt.closed).total_seconds()) < 120:
                rt.after = a
    return done


def _row(label: str, trips: list[RoundTrip]) -> str:
    n = len(trips)
    wins = sum(1 for r in trips if r.pnl > 0)
    pnl = sum(r.pnl for r in trips)
    avg = sum(r.pct for r in trips) / n if n else 0.0
    return f"  {label:<26} {n:>4}   {wins / n * 100 if n else 0:>4.0f}%   {avg:>+7.1f}%   {pnl:>+9.2f}"


def _group(trips: list[RoundTrip], key: Callable[[RoundTrip], str], order: list[str] | None = None) -> list[str]:
    groups: dict[str, list[RoundTrip]] = {}
    for r in trips:
        groups.setdefault(key(r), []).append(r)
    names = [k for k in (order or []) if k in groups] + sorted(k for k in groups if k not in (order or []))
    return [_row(k, groups[k]) for k in names]


def _bucket(value: str | None, edges: list[float], unit: str = "") -> str:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown"
    for lo, hi in zip(edges, edges[1:] + [float("inf")]):
        if v < hi:
            return f"{lo:g}-{hi:g}{unit}" if hi != float("inf") else f"{lo:g}+{unit}"
    return "unknown"


def build_report(trades_csv: str | Path | list[str | Path], entries_csv: str | Path | None = None,
                 after_csv: str | Path | None = None) -> str:
    trips = round_trips(trades_csv, entries_csv, after_csv)
    if not trips:
        return "No completed trades yet."
    head = "  {:<26} {:>4}   {:>5}   {:>8}   {:>9}".format("", "n", "won", "avg", "pnl $")
    out = ["Round trips (a buy through its final sale; partial sales count toward it)", head,
           _row("ALL", trips), "", "By how it ended (the last sale)", head,
           *_group(trips, lambda r: r.final_kind), ""]
    with_entry = [r for r in trips if r.entry]
    if with_entry:
        out += ["By lane", head, *_group(with_entry, lambda r: r.entry.get("lane", "?")), ""]
        out += ["Tech or meme", head, *_group(with_entry, lambda r: r.entry.get("kind") or "unknown"), ""]
        for title, col, edges, unit in (("By liquidity at entry ($)", "liquidity_usd", [0, 25_000, 50_000, 100_000, 250_000], ""),
                                        ("By token age at entry (min)", "age_min", [0, 20, 60, 180, 720], ""),
                                        ("By 1h change at entry (%)", "chg_h1", [-100, 0, 30, 100, 250], ""),
                                        ("By entry score", "score", [0, 0.6, 0.7, 0.8], "")):
            out += [title, head, *_group(with_entry, lambda r, c=col, e=edges, u=unit: _bucket(r.entry.get(c), e, u)), ""]
        out += ["By LarpCheck verdict at entry", head, *_group(with_entry, lambda r: r.entry.get("larp_verdict") or "none"), ""]
    watched = [r for r in trips if r.after]
    if watched:
        out += ["After the sale (what the coin did over the next 12h, versus the sale price)",
                "  {:<26} {:>4}   {:>9}   {:>9}   {:>9}".format("sold by", "n", "avg high", "avg low", "up >50%")]
        groups: dict[str, list[RoundTrip]] = {}
        for r in watched:
            groups.setdefault(r.final_kind, []).append(r)
        for k, g in sorted(groups.items()):
            hi = [float(r.after["high_pct"]) for r in g]
            lo = [float(r.after["low_pct"]) for r in g]
            out.append("  {:<26} {:>4}   {:>+8.0f}%   {:>+8.0f}%   {:>8.0f}%".format(
                k, len(g), sum(hi) / len(g), sum(lo) / len(g), sum(1 for h in hi if h > 50) / len(g) * 100))
        out.append("")
    else:
        out += ["After the sale: nothing recorded yet (each sold coin is watched for 12h, then listed here).", ""]
    n = len(trips)
    if n < 30:
        out.append(f"Only {n} completed trades: treat every number above as an anecdote, not a pattern (30+ per row to lean on).")
    elif not with_entry:
        out.append("Entry details (entries.csv) aren't available for these trades; they're recorded from now on.")
    return "\n".join(out)
