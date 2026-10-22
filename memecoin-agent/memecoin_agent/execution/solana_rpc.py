"""Minimal Solana JSON-RPC client: just the calls the live executor needs."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from . import ExecutionError


class SolanaRpc:
    def __init__(
        self,
        url: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.http = client or httpx.Client(timeout=20.0)
        self.sleep = sleep
        self.clock = clock
        self._id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        resp = self.http.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise ExecutionError(f"RPC {method} error: {body['error']}")
        return body["result"]

    def get_balance(self, pubkey: str) -> int:
        return int(self.call("getBalance", [pubkey, {"commitment": "confirmed"}])["value"])

    def get_token_balance(self, owner: str, mint: str) -> tuple[int, int]:
        """Return (raw_amount, decimals) summed over the owner's accounts for this mint."""
        res = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        raw, decimals = 0, None
        for acct in res.get("value") or []:
            amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw += int(amt["amount"])
            decimals = int(amt["decimals"])
        if decimals is None:
            decimals = int(self.call("getTokenSupply", [mint])["value"]["decimals"])
        return raw, decimals

    def send_transaction(self, tx_b64: str) -> str:
        return self.call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 3}],
        )

    def confirm(self, signature: str, timeout_s: float) -> bool:
        """Wait for confirmation. True if confirmed, False on timeout; raises if the tx failed."""
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            res = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
            status = (res.get("value") or [None])[0]
            if status:
                if status.get("err"):
                    raise ExecutionError(f"Transaction {signature} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            self.sleep(1.5)
        return False
