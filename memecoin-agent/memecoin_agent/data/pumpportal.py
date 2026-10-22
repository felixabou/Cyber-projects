"""Real-time pump.fun launch feed over PumpPortal's free websocket.

DexScreener's public feeds only list tokens that paid for a profile or boost.
This feed sees every pump.fun graduation (token migrating from the bonding
curve to a real AMM pool) the moment it happens, and optionally every new
token creation. It runs in a background thread and keeps a time-bounded buffer
of mints; `FeedAugmentedMarket` puts those mints at the front of discovery.

PumpPortal asks clients to hold a single connection and subscribe on it, which
is what this does, with exponential-backoff reconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..models import TokenSnapshot

log = logging.getLogger(__name__)

SUBSCRIBE_MIGRATIONS = "subscribeMigration"
SUBSCRIBE_NEW_TOKENS = "subscribeNewToken"
EVENT_KINDS = {"migrate": "migration", "create": "launch"}


@dataclass
class FeedEvent:
    mint: str
    kind: str  # "migration" or "launch"
    seen_at: float  # unix seconds
    symbol: str = ""


class PumpPortalFeed:
    def __init__(
        self,
        url: str = "wss://pumpportal.fun/api/data",
        migrations: bool = True,
        new_tokens: bool = False,
        max_age_s: float = 72 * 3600,
        max_mints: int = 2_000,
        buffer_file: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        connect_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.url = url
        self.methods = [m for m, on in ((SUBSCRIBE_MIGRATIONS, migrations), (SUBSCRIBE_NEW_TOKENS, new_tokens)) if on]
        self.max_age_s = max_age_s
        self.max_mints = max_mints
        self.buffer_file = Path(buffer_file) if buffer_file else None
        self.clock = clock
        self.connect_kwargs = connect_kwargs or {}
        self.connected = threading.Event()
        self.events_received = 0
        self._events: OrderedDict[str, FeedEvent] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._last_save = 0.0

    # -- buffer ---------------------------------------------------------------------
    def handle_message(self, raw: str | bytes) -> FeedEvent | None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(msg, dict) or not msg.get("mint"):
            return None  # subscription acks, errors, keepalives
        kind = EVENT_KINDS.get(str(msg.get("txType", "")).lower())
        if kind is None:
            return None
        ev = FeedEvent(mint=str(msg["mint"]), kind=kind, seen_at=self.clock(), symbol=str(msg.get("symbol") or ""))
        with self._lock:
            prev = self._events.pop(ev.mint, None)
            if prev and prev.kind == "migration" and kind == "launch":
                ev = prev  # never downgrade a graduated token
            self._events[ev.mint] = ev
            while len(self._events) > self.max_mints:
                self._events.popitem(last=False)
            self.events_received += 1
        if kind == "migration":
            # Migration events usually carry no symbol; DexScreener supplies it once the pool is indexed.
            log.info("Pump.fun graduation: %s", f"{ev.symbol} {ev.mint}" if ev.symbol else ev.mint)
        return ev

    def _prune(self) -> None:
        cutoff = self.clock() - self.max_age_s
        with self._lock:
            for mint in [m for m, e in self._events.items() if e.seen_at < cutoff]:
                del self._events[mint]

    def recent(self) -> list[str]:
        """Buffered mints, graduations first, newest first within each group."""
        self._prune()
        with self._lock:
            events = list(self._events.values())
        # Insertion order breaks timestamp ties, so the latest arrival still comes first.
        order = sorted(range(len(events)), key=lambda i: (events[i].kind != "migration", -events[i].seen_at, -i))
        return [events[i].mint for i in order]

    def evict(self, mint: str) -> None:
        with self._lock:
            self._events.pop(mint, None)

    def seen_age_s(self, mint: str) -> float | None:
        with self._lock:
            ev = self._events.get(mint)
        return None if ev is None else self.clock() - ev.seen_at

    # -- persistence ----------------------------------------------------------------
    def save(self) -> None:
        if self.buffer_file is None:
            return
        with self._lock:
            data = [e.__dict__ for e in self._events.values()]
        self.buffer_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.buffer_file.with_suffix(f".{os.getpid()}.tmp")  # run + record may share the file
        tmp.write_text(json.dumps(data))
        tmp.replace(self.buffer_file)
        self._last_save = self.clock()

    def load(self) -> None:
        if self.buffer_file is None or not self.buffer_file.exists():
            return
        try:
            items = json.loads(self.buffer_file.read_text())
        except ValueError:
            log.warning("Ignoring unreadable feed buffer %s", self.buffer_file)
            return
        with self._lock:
            for d in items:
                self._events[d["mint"]] = FeedEvent(**d)
        self._prune()
        log.info("Loaded %d buffered launch-feed mints", len(self._events))

    # -- connection -----------------------------------------------------------------
    def start(self) -> None:
        if not self.methods or self._thread is not None:
            return
        self.load()
        self._thread = threading.Thread(target=self._thread_main, name="pumpportal-feed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._loop is not None and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.save()

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            log.error("Launch feed disabled: install the 'websockets' package")
            return
        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with connect(self.url, open_timeout=15, ping_interval=20, ping_timeout=20,
                                   **self.connect_kwargs) as ws:
                    self._ws = ws
                    for method in self.methods:
                        await ws.send(json.dumps({"method": method}))
                    self.connected.set()
                    log.info("Launch feed connected (%s)", ", ".join(self.methods))
                    backoff = 1.0
                    async for raw in ws:
                        self.handle_message(raw)
                        if self.clock() - self._last_save > 300:
                            self.save()
            except Exception as exc:  # network errors, server closes, bad handshakes
                if not self._stop.is_set():
                    log.warning("Launch feed disconnected: %s (retrying in %.0fs)", exc, backoff)
            finally:
                self._ws = None
                self.connected.clear()
            # Sleep in small steps so stop() is honoured promptly.
            deadline = time.monotonic() + backoff
            while not self._stop.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            backoff = min(backoff * 2, 60.0)


class FeedAugmentedMarket:
    """MarketData wrapper: launch-feed mints first in discovery, everything else delegated."""

    def __init__(self, base: Any, feed: PumpPortalFeed, evict_below_liquidity_usd: float = 5_000.0,
                 evict_grace_s: float = 1800.0) -> None:
        self.base = base
        self.feed = feed
        self.evict_below = evict_below_liquidity_usd
        # Right after a graduation DexScreener may still show only the drained bonding-curve
        # pair; give the new pool time to be indexed before judging the token dead.
        self.evict_grace_s = evict_grace_s

    def discover(self) -> list[str]:
        try:
            discovered = self.base.discover()
        except Exception as exc:
            log.warning("Base discovery failed: %s", exc)
            discovered = []
        return list(dict.fromkeys([*self.feed.recent(), *discovered]))

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        snaps = self.base.snapshots(mints)
        for mint, snap in snaps.items():
            age = self.feed.seen_age_s(mint)
            if age is not None and age > self.evict_grace_s and snap.liquidity_usd < self.evict_below:
                self.feed.evict(mint)  # pool is dead; stop spending scan budget on it
        return snaps

    def sol_price_usd(self) -> float:
        return self.base.sol_price_usd()
