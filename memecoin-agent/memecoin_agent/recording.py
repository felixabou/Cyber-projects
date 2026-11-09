"""Record market data to disk for backtesting.

The recorder polls discovery (DexScreener + launch feed) like the live agent
does, then keeps sampling every discovered token for `track_hours`, so a
backtest sees the full path after any entry, including for tokens a given
config would not have bought. It writes three kinds of records:

  {"type": "discover", "t": ..., "mints": [...]}   what discovery returned
  {"type": "snap", ...TokenSnapshot fields..., "pregraduation": bool} one per token per tick
  {"type": "safety", "t": ..., "mint": ..., ...}   RugCheck verdict (once per token)
  {"type": "wallet", "t": ..., "mint": ..., ...}   recent wallet scan for curve entries
  {"type": "wallet", "t": ..., "scan": ...}        point-in-time early wallet scan

Files are gzipped JSON lines, one per UTC hour. Each tick is appended as its
own gzip member, so a crash loses at most the tick being written.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import zlib
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Config
from .data import MarketData, SafetyChecker
from .data.rugcheck import UNAVAILABLE
from .models import utcnow
from .strategy import lowcap_reasons

log = logging.getLogger(__name__)

FILE_GLOB = "snapshots-*.jsonl.gz"


class RecordingWriter:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records_written = 0

    def path_for(self, t: datetime) -> Path:
        return self.dir / f"snapshots-{t:%Y%m%d-%H}.jsonl.gz"

    def write(self, t: datetime, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        with gzip.open(self.path_for(t), "at", encoding="utf-8") as fh:
            fh.write(payload)
        self.records_written += len(records)


def summarize_recordings(directory: str | Path) -> dict[str, Any] | None:
    """Cheap overview from file names and sizes (no decompression), for the dashboard."""
    files = sorted(Path(directory).glob(FILE_GLOB))
    if not files:
        return None
    hours = [datetime.strptime(f.name[len("snapshots-"):-len(".jsonl.gz")], "%Y%m%d-%H") for f in files]
    return {
        "files": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "first_hour": hours[0].isoformat(),
        "last_hour": hours[-1].isoformat(),
        "hours_with_data": len(files),
    }


def record_time(rec: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(rec["fetched_at"] if rec.get("type") == "snap" else rec["t"])


def read_recordings(directory: str | Path) -> Iterator[dict[str, Any]]:
    """Yield records in file (= chronological) order, skipping a truncated tail."""
    files = sorted(Path(directory).glob(FILE_GLOB))
    if not files:
        raise FileNotFoundError(f"No recordings ({FILE_GLOB}) in {directory}")
    for path in files:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        log.warning("Skipping corrupt line in %s", path.name)
        except (EOFError, OSError, zlib.error) as exc:
            log.warning("Recording %s ends early (%s); using what was readable", path.name, exc)


class Recorder:
    def __init__(
        self,
        cfg: Config,
        market: MarketData,
        safety: SafetyChecker | None,
        writer: RecordingWriter,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        wallet_scan: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.rc = cfg.recorder
        self.market = market
        self.safety = safety if self.rc.record_safety else None
        self.wallet_scan = wallet_scan
        # Real engine wiring passes the same scanner used by Agent.launch_scan. Keep the historical
        # auto-construction for the opt-in sniper, but do not create a live RPC scanner implicitly for
        # synthetic/offline recordings just because the low-cap lane is enabled by default.
        if self.wallet_scan is None and cfg.sniper.enabled and self.safety is not None:
            from .data.wallets import WalletScanner

            self.wallet_scan = WalletScanner(cfg.launch, cfg.execution.rpc_url)
        self.writer = writer
        self.clock = clock
        self.sleep = sleep
        self.tracked: dict[str, datetime] = {}  # mint -> stop tracking at
        self.seen: dict[str, datetime] = {}  # mint -> first seen; never re-track an expired mint
        self.safety_done: set[str] = set()
        self._wallet_requested_at: dict[str, datetime] = {}
        self.ticks = 0
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        return self.last_tick_at is None or (now - self.last_tick_at).total_seconds() >= self.rc.interval_s - 1e-6

    def tick(self) -> None:
        now = self.clock()
        self.started_at = self.started_at or now
        self.last_tick_at = now
        records: list[dict[str, Any]] = []
        if self.ticks % self.rc.discover_every_ticks == 0:
            try:
                mints = self.market.discover()
            except Exception as exc:
                log.warning("Discovery failed: %s", exc)
                mints = []
            records.append({"type": "discover", "t": now.isoformat(), "mints": mints})
            until = now + timedelta(hours=self.rc.track_hours)
            for m in mints:
                if m not in self.seen:
                    self.seen[m] = now
                    self.tracked[m] = until
        self.ticks += 1

        self.tracked = {m: u for m, u in self.tracked.items() if u > now}
        if len(self.tracked) > self.rc.max_tracked:  # keep the most recently discovered
            keep = sorted(self.tracked, key=lambda m: self.seen[m], reverse=True)[: self.rc.max_tracked]
            self.tracked = {m: self.tracked[m] for m in keep}
        week_ago = now - timedelta(days=7)
        self.seen = {m: t for m, t in self.seen.items() if t > week_ago}

        try:
            snaps = self.market.snapshots(list(self.tracked)) if self.tracked else {}
        except Exception as exc:
            log.warning("Snapshot fetch failed: %s", exc)
            snaps = {}
        pregraduation = getattr(self.market, "is_pregraduation", None)
        records.extend(
            {"type": "snap", **s.to_dict(),
             "pregraduation": bool(pregraduation(s.mint)) if pregraduation else False}
            for s in snaps.values()
        )
        records.extend(self._safety_records(snaps, now))
        records.extend(self._wallet_records(snaps, now))
        self.writer.write(now, records)

    def _wallet_records(self, snaps: dict, now: datetime) -> list[dict[str, Any]]:
        """Capture scans for plausible Solana early/low-cap candidates; unresolved scans stay unknown."""
        if self.wallet_scan is None or not (self.cfg.sniper.enabled or self.cfg.lowcap.enabled):
            return []
        sniper = self.cfg.sniper
        is_pregraduation = getattr(self.market, "is_pregraduation", lambda _: False)
        eligible: list[Any] = []
        for snap in snaps.values():
            if snap.chain != "solana":
                continue
            age = snap.age_minutes(now)
            if self.cfg.sniper.enabled and (
                    age is not None and 0 <= age <= sniper.max_age_minutes
                    and snap.liquidity_usd >= sniper.min_liquidity_usd
                    and sniper.min_market_cap_usd <= snap.market_cap_usd <= sniper.max_market_cap_usd
                    and snap.volume_h1 >= sniper.min_volume_h1_usd and snap.txns_h1 >= sniper.min_txns_h1
                    and is_pregraduation(snap.mint)):
                eligible.append(snap)
                continue
            # The low-cap lane uses the same Solana wallet scan as launches, but only after a token
            # satisfies the cheap market-window rules. EVM low caps use ChainSafety instead.
            if self.cfg.lowcap.enabled and not lowcap_reasons(snap, self.cfg.lowcap, self.cfg.filters, now):
                eligible.append(snap)
        # Rotate requests so the oldest candidates do not monopolize a busy feed.
        eligible.sort(key=lambda s: (self._wallet_requested_at.get(s.mint, datetime.min.replace(tzinfo=now.tzinfo)),
                                     -(s.pair_created_at.timestamp() if s.pair_created_at else 0)))
        for snap in eligible[:10]:
            try:
                self.wallet_scan.check(snap.mint)
            except Exception as exc:
                log.warning("Wallet scan recording failed for %s: %s", snap.mint, type(exc).__name__)
            self._wallet_requested_at[snap.mint] = now
        self._wallet_requested_at = {m: t for m, t in self._wallet_requested_at.items()
                                     if t > now - timedelta(days=1)}
        out: list[dict[str, Any]] = []
        for snap in eligible:
            try:
                scan = self.wallet_scan.result(snap.mint)
            except Exception as exc:
                log.warning("Wallet scan recording failed for %s: %s", snap.mint, type(exc).__name__)
                continue
            if scan is None:
                continue
            scanner_clock = getattr(self.wallet_scan, "clock", None)
            if scanner_clock is not None:
                from .data.wallets import ERROR_TTL_S, RESULT_TTL_S

                ttl = ERROR_TTL_S if scan.error else RESULT_TTL_S
                if not 0 <= scanner_clock() - scan.scanned_at < ttl:
                    continue
            if scan.mint != snap.mint:
                continue
            raw = asdict(scan)
            raw.pop("scanned_at", None)  # monotonic clock does not survive a recording
            raw["launched_at"] = scan.launched_at.isoformat() if scan.launched_at else None
            out.append({"type": "wallet", "t": max(now, snap.fetched_at).isoformat(), "mint": snap.mint,
                        "scan": raw})
        return out

    def _safety_records(self, snaps: dict, now: datetime) -> list[dict[str, Any]]:
        if self.safety is None:
            return []
        out = []
        # Only tokens that could plausibly pass a liquidity filter; RugCheck isn't free to hammer.
        floor = self.cfg.filters.min_liquidity_usd * 0.5
        todo = [m for m, s in snaps.items() if m not in self.safety_done and s.liquidity_usd >= floor]
        for mint in todo[:10]:
            rep = self.safety.check(mint)
            if rep.reasons == [UNAVAILABLE]:
                continue  # retry next tick rather than recording an outage as a verdict
            self.safety_done.add(mint)
            out.append({"type": "safety", "t": now.isoformat(), "mint": mint,
                        "ok": rep.ok, "score": rep.score, "reasons": rep.reasons})
        return out

    def run(self, hours: float | None = None) -> None:
        end = self.clock() + timedelta(hours=hours) if hours else None
        log.info("Recording to %s every %.0fs%s", self.writer.dir, self.rc.interval_s,
                 f" for {hours}h" if hours else "")
        try:
            while end is None or self.clock() < end:
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:
                    log.exception("Recorder tick failed")
                if self.ticks % max(1, int(600 / self.rc.interval_s)) == 0:  # every ~10 min
                    log.info("Tracking %d tokens, %d records written", len(self.tracked), self.writer.records_written)
                self.sleep(max(0.0, self.rc.interval_s - (time.monotonic() - started)))
        except KeyboardInterrupt:
            log.info("Recorder stopped.")

