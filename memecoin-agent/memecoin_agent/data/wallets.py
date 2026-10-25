"""Wallet scan for launch-lane buys: who bought at launch, and how much can they still dump?

Two free sources:
- The chain (execution.rpc_url). Walking the mint's transactions back to the first block gives
  the real launch time (on pump.fun the coin's creation, not its later move to PumpSwap) and every
  wallet that bought in that block: the creator's own buy plus anything bundled with it. Their
  current balances are what the bundle check judges.
- RugCheck's full report: linked-wallet clusters ("insider networks") and insider-flagged holders,
  the creator's balance and rug flags, and the top holders with pools and lockers labelled.

A scan takes 10-60 s against a public RPC, so it runs on a background thread. Until it finishes a
candidate reads as SCANNING and the agent carries on (exits are never delayed).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from ..config import LaunchConfig
from ..models import SafetyReport

log = logging.getLogger(__name__)

SCANNING = "wallet scan running"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
POOL_TYPES = {"AMM", "LOCKER"}
MAX_BUYER_LOOKUPS = 25
MAX_EARLY_TXS = 40
RESULT_TTL_S = 300.0
ERROR_TTL_S = 60.0


class WalletScanError(RuntimeError):
    pass


class PacedRpc:
    """Solana JSON-RPC with a minimum gap between calls and back-off on HTTP 429: public RPCs
    rate-limit hard. Errors never include the URL, which can hold a private RPC's API key."""

    def __init__(self, url: str, client: httpx.Client | None = None, gap_s: float = 0.3,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.url = url
        self.http = client or httpx.Client(timeout=30.0)
        self.gap_s = gap_s
        self.sleep = sleep
        self.clock = clock
        self._last = -1e9

    def call(self, method: str, params: list[Any]) -> Any:
        for attempt in range(6):
            wait = self._last + self.gap_s - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                resp = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except httpx.HTTPError as exc:
                raise WalletScanError(f"{method}: {type(exc).__name__}") from None
            if resp.status_code == 429:
                self.sleep(min(2.0 ** attempt, 20.0))
                continue
            if resp.status_code >= 400:
                raise WalletScanError(f"{method}: HTTP {resp.status_code}")
            body = resp.json()
            if "error" in body:
                raise WalletScanError(f"{method}: {(body['error'] or {}).get('message', body['error'])}")
            return body.get("result")
        raise WalletScanError(f"{method}: rate limited by the RPC")


@dataclass
class LaunchBlock:
    launched_at: datetime
    slot: int
    buyers: dict[str, float]  # wallet -> tokens it bought in the launch block (bundles + the creator's buy)
    snipers: dict[str, float]  # the next two blocks: reported, not judged
    transactions: int  # how far back the walk went


@dataclass
class WalletScan:
    mint: str
    scanned_at: float  # monotonic
    launched_at: datetime | None = None
    bundle_wallets: int = 0
    bundle_bought_pct: float = 0.0
    bundle_held_pct: float = 0.0
    sniper_bought_pct: float = 0.0
    cluster_pct: float = 0.0
    creator_pct: float = 0.0
    top10_pct: float = 0.0
    flags: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"wallet scan failed: {self.error}"
        return (f"bundles hold {self.bundle_held_pct:.1f}% ({self.bundle_bought_pct:.1f}% bought in the launch block "
                f"by {self.bundle_wallets} wallet{'s' if self.bundle_wallets != 1 else ''}), linked wallets "
                f"{self.cluster_pct:.1f}%, top 10 {self.top10_pct:.1f}%, creator {self.creator_pct:.1f}%")


def _token_balances(entries: list[dict[str, Any]] | None, mint: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for b in entries or []:
        if b.get("mint") == mint and b.get("owner"):
            amount = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
            out[b["owner"]] = out.get(b["owner"], 0.0) + float(amount)
    return out


def _signers(tx: dict[str, Any]) -> set[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return {k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")}


class WalletScanner:
    def __init__(self, cfg: LaunchConfig, rpc_url: str, rpc: PacedRpc | None = None,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic,
                 background: bool = True) -> None:
        self.cfg = cfg
        self.rpc = rpc or PacedRpc(rpc_url)
        self.http = http or httpx.Client(timeout=30.0, headers={"Accept": "application/json"})
        self.clock = clock
        self.background = background
        self._launches: dict[str, LaunchBlock] = {}  # a launch block never changes: kept for good
        self._results: dict[str, WalletScan] = {}
        self._pending: set[str] = set()
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # -- the agent's side -------------------------------------------------------------------
    def check(self, mint: str, qualified_at: datetime | None = None) -> SafetyReport:
        """The verdict for a launch-lane candidate, or SCANNING while a scan runs."""
        start = False
        with self._lock:
            res = self._results.get(mint)
            if res is not None and self.clock() - res.scanned_at < (ERROR_TTL_S if res.error else RESULT_TTL_S):
                return self.judge(res, qualified_at)
            if mint not in self._pending:
                self._pending.add(mint)
                self._queue.put(mint)
                if self.background and self._worker is None:
                    self._worker = threading.Thread(target=self._work, name="wallet-scan", daemon=True)
                    start = True
        if start:
            self._worker.start()
        if not self.background:
            self.run_pending()
            return self.check(mint, qualified_at)
        return SafetyReport(ok=False, score=None, reasons=[SCANNING])

    def result(self, mint: str) -> WalletScan | None:
        with self._lock:
            return self._results.get(mint)

    def judge(self, scan: WalletScan, qualified_at: datetime | None) -> SafetyReport:
        if scan.error:
            return SafetyReport(ok=False, score=None, reasons=[f"wallet scan failed: {scan.error}"])
        c = self.cfg
        reasons: list[str] = []
        if scan.launched_at is not None and qualified_at is not None:
            age = (qualified_at - scan.launched_at).total_seconds() / 60
            if age > c.max_age_minutes + 1:  # +1: block times vs our scan clock
                reasons.append(f"launched {age:.0f}m before it reached the volume (limit {c.max_age_minutes:.0f}m)")
        # Judged on what the launch-block wallets took and on what they still hold: a bundle that already
        # sold made most of the early "volume" by dumping on buyers, which is no safer than one still holding.
        if max(scan.bundle_bought_pct, scan.bundle_held_pct) > c.max_bundle_pct:
            reasons.append(f"bundled launch: {scan.bundle_wallets} wallets bought {scan.bundle_bought_pct:.1f}% in the "
                           f"launch block and still hold {scan.bundle_held_pct:.1f}% (limit {c.max_bundle_pct:.0f}%)")
        if scan.cluster_pct > c.max_cluster_pct:
            reasons.append(f"linked wallets hold {scan.cluster_pct:.1f}% > {c.max_cluster_pct:.0f}%")
        if scan.creator_pct > c.max_creator_pct:
            reasons.append(f"creator holds {scan.creator_pct:.1f}% > {c.max_creator_pct:.0f}%")
        if scan.top10_pct > c.max_top10_pct:
            reasons.append(f"top 10 holders {scan.top10_pct:.1f}% > {c.max_top10_pct:.0f}%")
        reasons += scan.flags
        return SafetyReport(ok=not reasons, score=scan.bundle_held_pct, reasons=reasons)

    # -- the worker -------------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            self.run_pending(block=True)

    def run_pending(self, block: bool = False) -> None:
        while True:
            try:
                mint = self._queue.get(block=block)
            except queue.Empty:
                return
            try:
                res = self.scan(mint)
                log.info("Wallet scan %s: %s", mint, res.summary())
            except Exception as exc:  # any failure is a failed scan: launch lane fails closed
                res = WalletScan(mint, self.clock(), error=str(exc) or type(exc).__name__)
                log.warning("Wallet scan %s failed: %s", mint, res.error)
            with self._lock:
                self._results[mint] = res
                self._pending.discard(mint)
                if len(self._results) > 500:
                    for old in sorted(self._results, key=lambda m: self._results[m].scanned_at)[:100]:
                        self._results.pop(old, None)
            self._queue.task_done()
            if block:
                return

    # -- the scan -----------------------------------------------------------------------------
    def scan(self, mint: str) -> WalletScan:
        launch = self._launches.get(mint) or self.find_launch(mint)
        self._launches[mint] = launch
        supply = float(((self.rpc.call("getTokenSupply", [mint]) or {}).get("value") or {}).get("uiAmount") or 0)
        if supply <= 0:
            raise WalletScanError("token supply unknown")
        held = sum(self.holding(owner, mint) for owner in list(launch.buyers)[:MAX_BUYER_LOOKUPS])
        res = WalletScan(
            mint, self.clock(), launched_at=launch.launched_at, bundle_wallets=len(launch.buyers),
            bundle_bought_pct=sum(launch.buyers.values()) / supply * 100, bundle_held_pct=held / supply * 100,
            sniper_bought_pct=sum(launch.snipers.values()) / supply * 100,
        )
        self.apply_rugcheck(res, self.rugcheck_report(mint))
        return res

    def find_launch(self, mint: str) -> LaunchBlock:
        before: str | None = None
        oldest: list[dict[str, Any]] = []
        total = 0
        for _ in range(self.cfg.max_signature_pages):
            params: dict[str, Any] = {"limit": 1000}
            if before:
                params["before"] = before
            page = self.rpc.call("getSignaturesForAddress", [mint, params]) or []
            if not page:
                break
            oldest, before, total = page, page[-1]["signature"], total + len(page)
            if len(page) < 1000:
                break
        else:
            raise WalletScanError(f"over {total:,} transactions; couldn't reach the launch block")
        if not oldest:
            raise WalletScanError("no transactions found for this token")
        first = oldest[-1]
        slot0 = int(first["slot"])
        block_time = first.get("blockTime") or self.rpc.call("getBlockTime", [slot0])
        if not block_time:
            raise WalletScanError("launch block time unknown")
        buyers: dict[str, float] = {}
        snipers: dict[str, float] = {}
        early = [s for s in reversed(oldest) if int(s["slot"]) <= slot0 + 2 and not s.get("err")]
        for sig in early[:MAX_EARLY_TXS]:
            tx = self.rpc.call("getTransaction", [sig["signature"], {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
            if not tx or not tx.get("meta"):
                continue
            pre = _token_balances(tx["meta"].get("preTokenBalances"), mint)
            post = _token_balances(tx["meta"].get("postTokenBalances"), mint)
            # Buyers sign their buys; the pool or bonding curve that hands the tokens out doesn't.
            bucket = buyers if int(sig["slot"]) == slot0 else snipers
            for owner in _signers(tx):
                gained = post.get(owner, 0.0) - pre.get(owner, 0.0)
                if gained > 0:
                    bucket[owner] = bucket.get(owner, 0.0) + gained
        return LaunchBlock(datetime.fromtimestamp(int(block_time), timezone.utc), slot0, buyers, snipers, total)

    def holding(self, owner: str, mint: str) -> float:
        res = self.rpc.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
        total = 0.0
        for acc in res.get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            total += float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
        return total

    def rugcheck_report(self, mint: str) -> dict[str, Any]:
        try:
            resp = self.http.get(RUGCHECK_REPORT.format(mint=mint))
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WalletScanError(f"RugCheck report unavailable ({type(exc).__name__})") from None
        if not isinstance(data, dict):
            raise WalletScanError("RugCheck report unreadable")
        return data

    @staticmethod
    def apply_rugcheck(res: WalletScan, rep: dict[str, Any]) -> None:
        raw_supply = float((rep.get("token") or {}).get("supply") or 0)
        known = rep.get("knownAccounts") or {}
        pools = {addr for addr, k in known.items() if (k or {}).get("type") in POOL_TYPES}
        holders = [h for h in rep.get("topHolders") or [] if h.get("owner") not in pools and h.get("address") not in pools]
        res.top10_pct = sum(float(h.get("pct") or 0) for h in holders[:10])
        insider_holders = sum(float(h.get("pct") or 0) for h in holders if h.get("insider"))
        networks = 0.0
        if raw_supply > 0:
            networks = sum(float(n.get("currentHolding", n.get("tokenAmount")) or 0)
                           for n in rep.get("insiderNetworks") or []) / raw_supply * 100
            res.creator_pct = float(rep.get("creatorBalance") or 0) / raw_supply * 100
        res.cluster_pct = max(networks, insider_holders)
        if rep.get("rugged"):
            res.flags.append("RugCheck marks this token rugged")
        for risk in rep.get("risks") or []:
            name = str(risk.get("name") or "")
            if "rug" in name.lower():  # e.g. the creator's history of rugged tokens
                res.flags.append(f"RugCheck: {name}")
