"""Statics PR watcher: alerts when a pull request adds token addresses to a repo's coin-statics files.

The article's lead was a public BitGoJS pull request that added a token's identifiers to the statics files,
hours or days before the listing. This watcher reads recently updated pull requests through the GitHub REST API,
scans the added lines of files under the path prefix for 0x addresses, and alerts once per address per pull
request. It only reads from GitHub.

A hit is a lead, not proof of a listing: the statics files are custody metadata, which the article notes too.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .alerts import Alert, AlertSink

log = logging.getLogger(__name__)

API = "https://api.github.com"
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
DEFAULT_PREFIX = "modules/statics/src/coins/"
SEEN_KEEP = 2_000


def added_addresses(patch: str) -> list[tuple[str, str]]:
    """(address, the added line) for every 0x address on a line the patch adds."""
    out: list[tuple[str, str]] = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for addr in dict.fromkeys(a.lower() for a in ADDRESS_RE.findall(line)):
                out.append((addr, line[1:].strip()[:200]))
    return out


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHub:
    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(timeout=20.0, headers=headers, transport=transport)

    def get(self, path: str, params: dict[str, Any] | None = None, etag: str | None = None) -> tuple[int, Any, str | None]:
        # Errors are re-raised without the URL and token: httpx messages can include request details.
        try:
            resp = self.client.get(f"{API}{path}", params=params, headers={"If-None-Match": etag} if etag else {})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GitHub request failed ({type(exc).__name__})") from None
        if resp.status_code == 304:
            return 304, None, etag
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub HTTP {resp.status_code}")
        try:
            return resp.status_code, resp.json(), resp.headers.get("ETag")
        except ValueError:
            raise RuntimeError("GitHub returned a non-JSON response") from None


class StaticsPrWatcher:
    def __init__(self, gh: GitHub, repo: str, sink: AlertSink, state_path: str | Path,
                 path_prefix: str = DEFAULT_PREFIX, recent_hours: float = 48.0, pr_limit: int = 30,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.gh, self.repo, self.sink, self.clock = gh, repo, sink, clock
        self.prefix, self.recent = path_prefix, timedelta(hours=recent_hours)
        self.pr_limit = pr_limit
        self.state_path = Path(state_path)
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {"etag": data.get("etag"), "seen": list(data.get("seen", []))}
        except (OSError, ValueError, AttributeError):
            return {"etag": None, "seen": []}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state["seen"] = self.state["seen"][-SEEN_KEEP:]
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def _files(self, number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            _, batch, _ = self.gh.get(f"/repos/{self.repo}/pulls/{number}/files", {"per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def poll_once(self) -> list[Alert]:
        now = self.clock()
        try:
            status, prs, etag = self.gh.get(f"/repos/{self.repo}/pulls",
                                            {"state": "all", "sort": "updated", "direction": "desc",
                                             "per_page": self.pr_limit},
                                            etag=self.state.get("etag"))
        except RuntimeError as exc:
            log.warning("Statics watch %s: %s", self.repo, exc)
            return []
        if status == 304:
            return []
        self.state["etag"] = etag

        alerts: list[Alert] = []
        for pr in prs:
            if now - parse_time(pr["updated_at"]) > self.recent:
                continue
            number = pr["number"]
            try:
                files = self._files(number)
            except RuntimeError as exc:
                log.warning("Statics watch %s PR #%s: %s", self.repo, number, exc)
                continue
            for f in files:
                if not f["filename"].startswith(self.prefix):
                    continue
                patch = f.get("patch")
                if patch is None:
                    log.warning("Statics watch %s PR #%s: no patch for %s (diff too large?)", self.repo, number,
                                f["filename"])
                    continue
                for addr, line in added_addresses(patch):
                    key = f"{self.repo}#{number}:{addr}"
                    if key in self.state["seen"]:
                        continue
                    self.state["seen"].append(key)
                    alert = Alert("statics-pr", addr,
                                  f"{self.repo} PR #{number} ({pr['state']}, opened {pr['created_at']}) adds in "
                                  f"{f['filename']}: {line}", now, pr["html_url"])
                    self.sink.emit(alert)
                    alerts.append(alert)
        self._save()
        return alerts
