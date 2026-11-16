"""Alerts from the watchers: one log line and one CSV row each. Watchers never place trades."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

COLUMNS = ["seen_at", "source", "subject", "detail", "url"]


@dataclass
class Alert:
    source: str
    subject: str
    detail: str
    seen_at: datetime
    url: str = ""


class AlertSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def emit(self, alert: Alert) -> None:
        log.warning("WATCH %s %s: %s %s", alert.source, alert.subject, alert.detail, alert.url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(COLUMNS)
            writer.writerow([alert.seen_at.isoformat(), alert.source, alert.subject, alert.detail, alert.url])
