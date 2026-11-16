"""Approval watcher: alerts when a contract's approved-token mapping changes for a set of tokens.

A target is one getter of the form `name(address) -> bool` on a contract, such as the launch factory's
`approvedPairTokens(address)`. Each poll calls it on the latest block for every listed token and alerts when the
answer flips, in either direction. The watcher only reads (eth_call); it sends no transactions and holds no keys.

A mapping cannot be listed in full, so the watched tokens come from config. Approvals of tokens nobody listed are
not seen until the contract's event is watched too.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from Crypto.Hash import keccak

from .alerts import Alert, AlertSink

log = logging.getLogger(__name__)

Call = Callable[[str, str], str]  # (contract, calldata) -> hex result of eth_call on the latest block


def selector(signature: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return "0x" + h.hexdigest()[:8]


def encode_address_call(sel: str, address: str) -> str:
    a = address.lower()
    if len(a) != 42 or not a.startswith("0x"):
        raise ValueError(f"not an address: {address!r}")
    return sel + a[2:].rjust(64, "0")


def decode_bool(result: str) -> bool:
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        raise ValueError(f"unexpected eth_call result: {result!r}")
    return int(result, 16) != 0


def rpc_call(url: str, timeout: float = 15.0) -> Call:
    client = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})

    def call(to: str, data: str) -> str:
        # The RPC URL carries the API key, so errors are re-raised without it: httpx messages include the URL.
        try:
            resp = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                          "params": [{"to": to, "data": data}, "latest"]})
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"RPC HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"RPC request failed ({type(exc).__name__})") from None
        if "error" in body:
            raise RuntimeError(f"RPC error: {body['error']}")
        return body["result"]

    return call


@dataclass
class ApprovalTarget:
    name: str
    contract: str
    getter: str  # signature, e.g. "approvedPairTokens(address)"
    tokens: list[str] = field(default_factory=list)


class ApprovalWatcher:
    def __init__(self, call: Call, targets: list[ApprovalTarget], sink: AlertSink, state_path: str | Path,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
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
                                  f"({t.contract})", now, f"https://robinhoodchain.blockscout.com/address/{t.contract}")
                    self.sink.emit(alert)
                    alerts.append(alert)
                if prev != approved:
                    self.state[key] = approved
                    changed = True
        if changed:
            self._save()
        return alerts
