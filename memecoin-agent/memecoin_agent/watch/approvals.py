"""Approval watchers: alert when a contract's approved-token list changes. Read-only: no transactions, no keys.

Two ways to watch, both needed for full coverage:

- ApprovalWatcher polls a getter such as `approvedPairTokens(address) -> bool` for a configured list of tokens.
  It catches flips for tokens you name, in either direction.
- ApprovalEventWatcher reads the contract's `PairTokenApprovalUpdated(address,bool)` logs. It catches approvals of
  any token, including ones nobody listed, but only from the block it starts at (plus an optional lookback).

The RPC URL carries the API key, so errors are re-raised without it: httpx messages include the URL.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from Crypto.Hash import keccak

from .alerts import Alert, AlertSink

log = logging.getLogger(__name__)

APPROVAL_EVENT = "PairTokenApprovalUpdated(address,bool)"  # PonsV2LaunchFactory, pairToken indexed
MAX_LOG_RANGE = 2_000  # blocks per eth_getLogs call
SEEN_KEEP = 500
EXPLORER = "https://robinhoodchain.blockscout.com"


def keccak256_hex(text: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(text.encode())
    return "0x" + h.hexdigest()


def selector(signature: str) -> str:
    return keccak256_hex(signature)[:10]


def encode_address_call(sel: str, address: str) -> str:
    a = address.lower()
    if len(a) != 42 or not a.startswith("0x"):
        raise ValueError(f"not an address: {address!r}")
    return sel + a[2:].rjust(64, "0")


def decode_bool(result: str) -> bool:
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        raise ValueError(f"unexpected eth_call result: {result!r}")
    return int(result, 16) != 0


class Rpc:
    def __init__(self, url: str, timeout: float = 15.0) -> None:
        self.url = url
        self.client = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})

    def request(self, method: str, params: list[Any]) -> Any:
        try:
            resp = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"RPC HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"RPC request failed ({type(exc).__name__})") from None
        if "error" in body:
            raise RuntimeError(f"RPC error: {body['error']}")
        return body["result"]

    def eth_call(self, to: str, data: str) -> str:
        return self.request("eth_call", [{"to": to, "data": data}, "latest"])


def rpc_call(url: str) -> Callable[[str, str], str]:
    return Rpc(url).eth_call


@dataclass
class ApprovalTarget:
    name: str
    contract: str
    getter: str  # signature, e.g. "approvedPairTokens(address)"
    tokens: list[str] = field(default_factory=list)


class ApprovalWatcher:
    def __init__(self, call: Callable[[str, str], str], targets: list[ApprovalTarget], sink: AlertSink,
                 state_path: str | Path, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.call, self.targets, self.sink, self.clock = call, targets, sink, clock
        self.state_path = Path(state_path)
        self.state: dict[str, bool] = self._load()
        self._selectors = {t.name: selector(t.getter) for t in targets}

    def _load(self) -> dict[str, bool]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): bool(v) for k, v in data.items()}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def poll_once(self) -> list[Alert]:
        now = self.clock()
        alerts: list[Alert] = []
        changed = False
        for t in self.targets:
            for token in t.tokens:
                key = f"{t.contract.lower()}:{token.lower()}"
                try:
                    approved = decode_bool(self.call(t.contract, encode_address_call(self._selectors[t.name], token)))
                except (RuntimeError, ValueError) as exc:
                    log.warning("Approval check %s for %s failed: %s", t.name, token, exc)
                    continue
                prev = self.state.get(key)
                if prev is None:
                    log.info("Approval baseline %s %s: %s", t.name, token, "approved" if approved else "not approved")
                elif prev != approved:
                    alert = Alert("approvals", token, f"{'approved' if approved else 'revoked'} on {t.name} "
                                  f"({t.contract})", now, f"{EXPLORER}/address/{t.contract}")
                    self.sink.emit(alert)
                    alerts.append(alert)
                if prev != approved:
                    self.state[key] = approved
                    changed = True
        if changed:
            self._save()
        return alerts


@dataclass
class ApprovalEventTarget:
    name: str
    contract: str
    lookback_blocks: int = 0  # 0: start at the chain head and only alert on later approvals


class ApprovalEventWatcher:
    def __init__(self, rpc: Rpc, target: ApprovalEventTarget, sink: AlertSink, state_path: str | Path,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.rpc, self.target, self.sink, self.clock = rpc, target, sink, clock
        self.state_path = Path(state_path)
        self.topic = keccak256_hex(APPROVAL_EVENT)
        self.last_block, self.seen = self._load()

    def _load(self) -> tuple[int | None, list[str]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data.get("last_block"), list(data.get("seen", []))
        except (OSError, ValueError, AttributeError):
            return None, []

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"last_block": self.last_block, "seen": self.seen[-SEEN_KEEP:]}),
                                   encoding="utf-8")

    def poll_once(self) -> list[Alert]:
        now = self.clock()
        try:
            head = int(self.rpc.request("eth_blockNumber", []), 16)
            if self.last_block is None:
                self.last_block = max(0, head - self.target.lookback_blocks)
                log.info("Approval events from block %d on %s", self.last_block + 1, self.target.name)
                self._save()
                if self.target.lookback_blocks == 0:
                    return []
            if head <= self.last_block:
                return []
            start, end = self.last_block + 1, min(head, self.last_block + MAX_LOG_RANGE)
            logs = self.rpc.request("eth_getLogs", [{
                "address": self.target.contract, "topics": [self.topic],
                "fromBlock": hex(start), "toBlock": hex(end)}])
        except (RuntimeError, ValueError) as exc:
            log.warning("Approval event check for %s failed: %s", self.target.name, exc)
            return []

        alerts: list[Alert] = []
        for lg in sorted(logs, key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16))):
            key = f"{lg['transactionHash']}:{int(lg['logIndex'], 16)}"
            if key in self.seen:
                continue
            self.seen.append(key)
            token = "0x" + lg["topics"][1][-40:]
            approved = int(lg["data"], 16) != 0
            alert = Alert("approval-event", token, f"{'approved' if approved else 'revoked'} by {self.target.name} "
                          f"in block {int(lg['blockNumber'], 16)}", now, f"{EXPLORER}/tx/{lg['transactionHash']}")
            self.sink.emit(alert)
            alerts.append(alert)
        self.last_block = end
        self._save()
        return alerts
