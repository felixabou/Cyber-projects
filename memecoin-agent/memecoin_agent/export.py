"""Trade downloads.

Every trade goes into one file for all time: the trade journal (agent.trades_csv). Downloads are
slices of it made on request, with the journal's columns:
- "session": this run of the agent (since it started),
- "1h" ... "24h": the last N hours,
- "all": every trade.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .portfolio import TRADE_COLUMNS

MAX_HOURS = 24
RANGES = ("session", *(f"{h}h" for h in range(1, MAX_HOURS + 1)), "all")
_HOURS = re.compile(r"^(?:last\s*)?(\d{1,2})\s*(?:h|hr|hrs|hour|hours)?$")


def parse_range(text: str | None) -> str:
    """'session', 'all' or 'Nh' (1-24) from what a person typed: '6', '6h', '6 hours', 'last 6h'..."""
    t = (text or "all").strip().lower()
    if t in ("session", "this session", "all", "everything"):
        return "all" if t == "everything" else t.replace("this ", "")
    m = _HOURS.match(t)
    if m and 1 <= int(m.group(1)) <= MAX_HOURS:
        return f"{int(m.group(1))}h"
    raise ValueError(f"Pick session, all, or 1 to {MAX_HOURS} hours")


def read_journal(path: str | Path) -> bytes:
    """The journal's complete rows (a row the agent is still appending is left out)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return b""
    return data[: data.rfind(b"\n") + 1]


@dataclass
class TradeSlice:
    which: str
    since: datetime | None
    until: datetime
    rows: list[dict[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        if self.which == "session" and self.since:
            return f"trades_session_{self.since:%Y-%m-%d_%H%M}-{self.until:%H%M}_UTC.csv"
        label = "all" if self.which == "all" else f"last_{self.which}"
        return f"trades_{label}_to_{self.until:%Y-%m-%d_%H%M}_UTC.csv"

    @property
    def label(self) -> str:
        if self.which == "all":
            return "All trades"
        if self.which == "session":
            return f"This session's trades (since {self.since:%b %d %H:%M} UTC)"
        hours = int(self.which[:-1])
        return f"Trades in the last {hours} hour{'s' if hours > 1 else ''}"

    def to_csv(self) -> bytes:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=self.columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows)
        return out.getvalue().encode()

    def summary(self) -> str:
        sells = [r for r in self.rows if r.get("side") == "sell"]
        try:
            realized = sum(float(r.get("realized_pnl_usd") or 0) for r in sells)
        except ValueError:
            realized = 0.0
        sign = "+" if realized >= 0 else "-"
        return (f"{self.label}: {len(self.rows)} ({len(self.rows) - len(sells)} buys, {len(sells)} sells), "
                f"realized {sign}${abs(realized):,.2f}")


def _utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)


def select(journal: str | Path, which: str, now: datetime, session_start: datetime) -> TradeSlice:
    """The journal's trades for one download range (see parse_range)."""
    which = parse_range(which)
    now = _utc(now)
    since = None if which == "all" else _utc(session_start) if which == "session" \
        else now - timedelta(hours=int(which[:-1]))
    reader = csv.DictReader(io.StringIO(read_journal(journal).decode("utf-8", "replace")))
    slice_ = TradeSlice(which, since, now, columns=list(reader.fieldnames or TRADE_COLUMNS))
    for row in reader:
        if since is not None:
            try:
                if _utc(datetime.fromisoformat(row["time"])) < since:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
        slice_.rows.append(row)
    return slice_
