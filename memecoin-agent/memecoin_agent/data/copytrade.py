"""Copy lane: a low cap is bought only when a tracked wallet buys it, on any chain GMGN covers.

Which wallets: addresses from GMGN's smart-money and KOL trade feeds and the top traders of each
chain's trending tokens, vetted on their last 30 days (copy.* in the config): a win rate between
10% and 30% (they hunt big multiples and take many small losses), first funded over two months ago,
real losing trades, still active, and net profitable. `copy.wallets` are always tracked.

What counts as a copy signal: a buy. Wallet activity is read with type=buy and the feeds with
side=buy, so transfers in (airdrops, wallets sending each other tokens) never count, and a buy of
a token the wallet itself created is dropped.

Runs on its own thread: GMGN's rate budget allows a few calls a second, so walking every wallet
takes a while; the agent reads `signals()` each scan and never waits on GMGN.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import CopyConfig
from .gmgn import GmgnClient, GmgnError, _f

log = logging.getLogger(__name__)

VET_PER_REFRESH = 60  # wallets vetted per chain per refresh (wallet_stats costs 3 of GMGN's budget)
FEED_EVERY_S = 30.0
REJECT_FOR_S = 24 * 3600  # a wallet that failed vetting isn't looked at again for a day


@dataclass
class TrackedWallet:
    chain: str  # GMGN chain name
    address: str
    win_rate: float | None = None
    pnl: float | None = None  # 30-day realized profit / cost
    trades: int = 0
    losses: int = 0
    age_days: float | None = None
    source: str = ""
    vetted_at: float = 0.0
    pinned: bool = False  # from copy.wallets: kept whatever its stats say
    last_buy_at: float = 0.0
    last_buy_symbol: str = ""


@dataclass
class CopySignal:
    chain: str  # GMGN chain name
    token: str
    symbol: str = ""
    wallets: dict[str, float] = field(default_factory=dict)  # wallet -> USD it spent
    first_at: float = 0.0  # unix time of the first tracked buy
    price_usd: float = 0.0

    @property
    def usd(self) -> float:
        return sum(self.wallets.values())


def vet_wallet(cfg: CopyConfig, stats: dict[str, Any], now: float) -> tuple[list[str], dict[str, Any]]:
    """(why not, numbers) for one wallet's 30-day stats from GMGN."""
    why: list[str] = []
    win = _f(stats.get("winrate"))
    buys = int(_f(stats.get("buy_count", stats.get("buy"))) or 0)
    sells = int(_f(stats.get("sell_count", stats.get("sell"))) or 0)
    cost = _f(stats.get("total_cost")) or 0.0
    realized = _f(stats.get("realized_profit"))
    pnl = _f(stats.get("pnl"))
    if pnl is None and realized is not None and cost > 0:
        pnl = realized / cost
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    created = _f(common.get("created_at"))
    age_days = (now - created) / 86400 if created else None
    losses = round(sells * (1 - win)) if win is not None else 0
    if win is None:
        why.append("no win rate")
    elif not cfg.min_win_rate <= win <= cfg.max_win_rate:
        why.append(f"win rate {win * 100:.0f}% (wants {cfg.min_win_rate * 100:g}-{cfg.max_win_rate * 100:g}%)")
    if age_days is None:
        why.append("wallet age unknown")
    elif age_days < cfg.min_wallet_age_days:
        why.append(f"wallet {age_days:.0f} days old (min {cfg.min_wallet_age_days:g})")
    if losses < cfg.min_losing_trades:
        why.append(f"{losses} losing trades (min {cfg.min_losing_trades})")
    if buys + sells < cfg.min_trades_30d:
        why.append(f"{buys + sells} trades in 30 days (min {cfg.min_trades_30d})")
    if cfg.min_pnl is not None and (pnl is None or pnl < cfg.min_pnl):
        why.append("30-day profit unknown" if pnl is None else f"30-day return {pnl * 100:+.0f}%")
    return why, {"win_rate": win, "pnl": pnl, "trades": buys + sells, "losses": losses, "age_days": age_days}


class CopyTracker:
    def __init__(self, cfg: CopyConfig, gmgn: GmgnClient, clock: Callable[[], float] = time.time,
                 state_file: str | Path | None = None) -> None:
        self.cfg = cfg
        self.gmgn = gmgn
        self.clock = clock
        self.state_file = Path(state_file) if state_file else None
        self.wallets: dict[tuple[str, str], TrackedWallet] = {}
        self.rejected: dict[str, float] = {}  # "chain:address" -> when it failed vetting
        self.last_refresh = 0.0
        self._signals: dict[tuple[str, str], CopySignal] = {}
        self._seen_tx: dict[str, float] = {}
        self._creators: dict[tuple[str, str], str] = {}  # (chain, token) -> creator address
        self._order: list[tuple[str, str]] = []
        self._next_wallet = 0
        self._next_feed = 0.0
        self.errors = 0
        self.last_error = ""
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()
        for entry in cfg.wallets:
            chain, sep, address = entry.partition(":")
            chain, address = (chain, address) if sep else ("sol", chain)
            key = (chain.strip(), address.strip())
            self.wallets.setdefault(key, TrackedWallet(*key, source="config"))
            self.wallets[key].pinned = True

    # -- persistence -------------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            for w in data.get("wallets", []):
                tw = TrackedWallet(**{k: v for k, v in w.items() if k in TrackedWallet.__dataclass_fields__})
                tw.pinned = False  # re-pinned from the config on start
                self.wallets[(tw.chain, tw.address)] = tw
            self.rejected = {k: float(v) for k, v in (data.get("rejected") or {}).items()}
            self.last_refresh = float(data.get("last_refresh") or 0)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Could not read %s: %s", self.state_file, exc)

    def save(self) -> None:
        if not self.state_file:
            return
        with self._lock:
            data = {"wallets": [asdict(w) for w in self.wallets.values()], "rejected": self.rejected,
                    "last_refresh": self.last_refresh}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.state_file)

    # -- thread ------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="copy-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.clock() - self.last_refresh >= self.cfg.refresh_hours * 3600:
                    self.refresh()
                self.poll_once()
            except Exception as exc:  # never let the tracker thread die
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Copy tracker: %s", self.last_error)
            self._stop.wait(self.cfg.poll_seconds)

    # -- finding wallets ---------------------------------------------------------------------------
    def candidates(self, chain: str) -> list[tuple[str, str]]:
        """(address, source) worth vetting on one chain, the trade feeds first."""
        found: dict[str, str] = {}
        for kind in ("smartmoney", "kol"):
            try:
                for row in self.gmgn.trades_feed(kind, chain, 200):
                    if row.get("maker"):
                        found.setdefault(str(row["maker"]), f"GMGN {kind} feed")
            except (httpx.HTTPError, GmgnError) as exc:
                log.info("Copy tracker: %s %s feed unavailable: %s", chain, kind, exc)
        try:
            trending = self.gmgn.trending("24h", {"limit": 10, "order_by": "volume", "direction": "desc"}, chain=chain)
        except (httpx.HTTPError, GmgnError) as exc:
            log.info("Copy tracker: %s trending unavailable: %s", chain, exc)
            trending = []
        for tok in trending[:5]:
            if not tok.get("address"):
                continue
            try:
                for row in self.gmgn.top_traders(chain, str(tok["address"]), 50):
                    addr = row.get("address") or row.get("wallet_address")
                    if addr:
                        found.setdefault(str(addr), f"top trader of {tok.get('symbol') or 'a trending token'}")
            except (httpx.HTTPError, GmgnError) as exc:
                log.info("Copy tracker: top traders unavailable: %s", exc)
        return list(found.items())

    def refresh(self) -> None:
        """Vet new candidates on every chain and keep the best max_wallets_per_chain (by 30-day return)."""
        now = self.clock()
        self.rejected = {k: t for k, t in self.rejected.items() if now - t < REJECT_FOR_S}
        for chain in self.cfg.chains:
            vetted = 0
            for address, source in self.candidates(chain):
                key = (chain, address)
                if vetted >= VET_PER_REFRESH or self._stop.is_set():
                    break
                if f"{chain}:{address}" in self.rejected:
                    continue
                w = self.wallets.get(key)
                if w is not None and now - w.vetted_at < self.cfg.refresh_hours * 3600:
                    continue
                vetted += 1
                try:
                    stats = self.gmgn.wallet_stats(chain, address, "30d")
                except (httpx.HTTPError, GmgnError) as exc:
                    log.info("Copy tracker: stats for %s unavailable: %s", address, exc)
                    continue
                why, nums = vet_wallet(self.cfg, stats, now)
                with self._lock:
                    if why and not (w and w.pinned):
                        self.wallets.pop(key, None)
                        self.rejected[f"{chain}:{address}"] = now
                        continue
                    tw = w or TrackedWallet(chain, address, source=source)
                    tw.win_rate, tw.pnl, tw.trades = nums["win_rate"], nums["pnl"], nums["trades"]
                    tw.losses, tw.age_days, tw.vetted_at = nums["losses"], nums["age_days"], now
                    self.wallets[key] = tw
            with self._lock:  # keep the best, plus every pinned wallet
                on_chain = sorted((w for w in self.wallets.values() if w.chain == chain and not w.pinned),
                                  key=lambda w: w.pnl or 0, reverse=True)
                for w in on_chain[self.cfg.max_wallets_per_chain:]:
                    self.wallets.pop((w.chain, w.address), None)
        self.last_refresh = now
        self._order = []
        n = len(self.wallets)
        log.info("Copy tracker: %d wallet(s) tracked across %s (win rate %g-%g%%, %g+ days old)", n,
                 ", ".join(self.cfg.chains), self.cfg.min_win_rate * 100, self.cfg.max_win_rate * 100,
                 self.cfg.min_wallet_age_days)
        self.save()

    # -- watching them -----------------------------------------------------------------------------
    def poll_once(self) -> None:
        now = self.clock()
        if not self.wallets:
            return
        if now >= self._next_feed:  # cheap: one call per chain covers every tagged wallet's trades
            self._next_feed = now + FEED_EVERY_S
            for chain in {w.chain for w in self.wallets.values()}:
                for kind in ("smartmoney", "kol"):
                    try:
                        rows = self.gmgn.trades_feed(kind, chain, 100)
                    except (httpx.HTTPError, GmgnError):
                        continue
                    for r in rows:
                        if str(r.get("side")).lower() != "buy" or (chain, str(r.get("maker"))) not in self.wallets:
                            continue
                        tok = r.get("base_token") if isinstance(r.get("base_token"), dict) else {}
                        self.on_buy(chain, str(r["maker"]), str(r.get("base_address") or ""), _f(r.get("amount_usd")),
                                    _f(r.get("timestamp")), _f(r.get("price_usd")), str(tok.get("symbol") or ""),
                                    str(r.get("transaction_hash") or ""))
            return
        if not self._order:
            self._order = list(self.wallets)
            self._next_wallet = 0
        key = self._order[self._next_wallet % len(self._order)]
        self._next_wallet += 1
        if key not in self.wallets:
            return
        chain, wallet = key
        try:
            rows = self.gmgn.wallet_activity(chain, wallet, 20, ("buy",))
        except (httpx.HTTPError, GmgnError) as exc:
            log.debug("Copy tracker: activity for %s unavailable: %s", wallet, exc)
            return
        for r in rows:
            if str(r.get("event_type") or r.get("type") or "").lower() != "buy":
                continue  # transfers in, adds/removes of liquidity: not a buy
            tok = r.get("token") if isinstance(r.get("token"), dict) else {}
            self.on_buy(chain, wallet, str(tok.get("address") or r.get("token_address") or ""), _f(r.get("cost_usd")),
                        _f(r.get("timestamp")), _f(r.get("price_usd")), str(tok.get("symbol") or ""),
                        str(r.get("tx_hash") or ""))

    def _creator(self, chain: str, token: str) -> str:
        key = (chain, token)
        if key not in self._creators:
            try:
                info = self.gmgn.info(token, chain)
            except (httpx.HTTPError, GmgnError):
                return ""  # unknown: checked again on the next buy
            dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
            self._creators[key] = str(dev.get("creator_address") or info.get("creator") or "")
            if len(self._creators) > 5000:
                self._creators.clear()
        return self._creators[key]

    def on_buy(self, chain: str, wallet: str, token: str, usd: float | None, ts: float | None,
               price: float | None, symbol: str, tx: str) -> None:
        now = self.clock()
        if not token or not ts or now - ts > self.cfg.max_signal_age_minutes * 60:
            return
        txkey = tx or f"{chain}:{wallet}:{token}:{ts}"
        if txkey in self._seen_tx:
            return
        self._seen_tx[txkey] = now
        if len(self._seen_tx) > 20_000:
            self._seen_tx = {k: t for k, t in self._seen_tx.items() if now - t < 3600}
        if (usd or 0) < self.cfg.min_wallet_buy_usd:
            return
        creator = self._creator(chain, token)
        if creator and creator.lower() == wallet.lower():
            log.info("Copy tracker: %s bought its own token %s; not copied", wallet[:8], symbol or token[:8])
            return
        with self._lock:
            w = self.wallets.get((chain, wallet))
            if w is not None:
                w.last_buy_at, w.last_buy_symbol = ts, symbol
            sig = self._signals.setdefault((chain, token), CopySignal(chain, token, symbol, first_at=ts))
            sig.wallets[wallet] = sig.wallets.get(wallet, 0.0) + (usd or 0.0)
            sig.price_usd = price or sig.price_usd
            sig.symbol = sig.symbol or symbol
        log.info("Copy signal: %s wallet %s bought $%.0f of %s", chain, wallet[:8], usd or 0, symbol or token)

    def signals(self) -> list[CopySignal]:
        """Fresh signals backed by at least min_wallets tracked wallets, oldest first."""
        now = self.clock()
        with self._lock:
            for key, s in list(self._signals.items()):
                if now - s.first_at > self.cfg.max_signal_age_minutes * 60:
                    del self._signals[key]
            return sorted((s for s in self._signals.values() if len(s.wallets) >= self.cfg.min_wallets),
                          key=lambda s: s.first_at)

    def consume(self, chain: str, token: str) -> None:
        with self._lock:
            self._signals.pop((chain, token), None)

    def view(self) -> dict[str, Any]:
        with self._lock:
            wallets = sorted(self.wallets.values(), key=lambda w: (w.chain, -(w.pnl or 0)))
            return {
                "wallets": [{**asdict(w), "short": w.address[:6] + "…" + w.address[-4:]} for w in wallets],
                "signals": [{"chain": s.chain, "token": s.token, "symbol": s.symbol, "wallets": len(s.wallets),
                             "usd": s.usd, "first_at": s.first_at} for s in self._signals.values()],
                "last_refresh": self.last_refresh, "errors": self.errors, "last_error": self.last_error,
                "chains": list(self.cfg.chains),
            }
