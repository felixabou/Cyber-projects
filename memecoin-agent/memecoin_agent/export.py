"""Trade CSVs on a schedule, for downloading later.

Each finished period (a UTC clock hour by default) that had trades gets its own file in
export.dir, e.g. `trades_2026-10-22_1300-1400_UTC.csv`, with the same columns as the trade journal.
The journal (agent.trades_csv) stays the complete history; these are its hourly slices. Periods
missed while the agent was off are filled in on the next start (up to two days back).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ExportConfig
from .models import TradeRecord
from .portfolio import read_trades_csv, write_trades_csv

log = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=48)
FILE_RE = re.compile(r"^trades_\d{4}-\d{2}-\d{2}_\d{4}-\d{4}_UTC\.csv$")


@dataclass
class ExportedFile:
    path: Path
    start: datetime
    end: datetime
    trades: list[TradeRecord]

    def summary(self) -> str:
        sells = [t for t in self.trades if t.side == "sell"]
        realized = sum(t.realized_pnl_usd for t in sells)
        sign = "+" if realized >= 0 else "-"
        return (f"Trades {self.start:%b %d %H:%M}-{self.end:%H:%M} UTC: {len(self.trades)} "
                f"({len(self.trades) - len(sells)} buys, {len(sells)} sells), realized {sign}${abs(realized):,.2f}")


class TradeExporter:
    def __init__(self, cfg: ExportConfig, trades_csv: str | Path, now: datetime) -> None:
        self.dir = Path(cfg.dir)
        self.trades_csv = Path(trades_csv)
        self.period = timedelta(minutes=cfg.every_minutes)
        # Periods starting before this are settled (exported, or had no trades).
        self._done_until = self.period_start(now) - LOOKBACK

    def period_start(self, t: datetime) -> datetime:
        t = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
        day = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return day + self.period * ((t - day) // self.period)

    def path_for(self, start: datetime) -> Path:
        end = start + self.period
        end_label = "2400" if end.date() != start.date() else f"{end:%H%M}"
        return self.dir / f"trades_{start:%Y-%m-%d}_{start:%H%M}-{end_label}_UTC.csv"

    def due(self, now: datetime) -> bool:
        return self.period_start(now) > self._done_until

    def run(self, now: datetime) -> list[ExportedFile]:
        """Write a file for every finished period since the last run that had trades and has no file yet."""
        until = self.period_start(now)  # the current period isn't finished
        if until <= self._done_until:
            return []
        since, self._done_until = self._done_until, until
        groups: dict[datetime, list[TradeRecord]] = {}
        for t in read_trades_csv(self.trades_csv):
            if since <= t.time < until:
                groups.setdefault(self.period_start(t.time), []).append(t)
        written: list[ExportedFile] = []
        for start, trades in sorted(groups.items()):
            path = self.path_for(start)
            if path.exists():  # written before a restart
                continue
            try:
                write_trades_csv(path, trades)
            except OSError as exc:
                log.warning("Could not write trade export %s: %s", path, exc)
                continue
            written.append(ExportedFile(path, start, start + self.period, trades))
            log.info("Saved %s (%d trades)", path, len(trades))
        return written

    def files(self, limit: int = 200) -> list[Path]:
        """Exported files, newest first."""
        if not self.dir.is_dir():
            return []
        return sorted((p for p in self.dir.iterdir() if FILE_RE.match(p.name)), key=lambda p: p.name,
                      reverse=True)[:limit]
