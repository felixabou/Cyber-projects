"""Launch checker: the early-buyer signals from the article's cases, read from a token's first transfers.

Given a token and its creation transaction, it reads the token's Transfer logs from the creation block onward
(eth_getLogs in chunks) and reports:

- early recipients: the first distinct addresses that received the token;
- identical buys: the same exact amount going to three or more wallets, with the time span;
- smart-account share: how many early recipients are EIP-7702 delegated accounts (code 0xef0100...);
- shared recipients: early recipients that also appear for another token checked in the same run.

Read-only: eth_getTransactionReceipt, eth_getLogs, eth_getBlockByNumber and eth_getCode. No keys, no transactions.
A shared recipient or an identical buy is a lead to check, not proof of coordination.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .approvals import Rpc, keccak256_hex

log = logging.getLogger(__name__)

TRANSFER = keccak256_hex("Transfer(address,address,uint256)")
ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
EIP7702_PREFIX = "0xef0100"
CHUNK_BLOCKS = 2_000
MIN_CHUNK_BLOCKS = 10
MIN_IDENTICAL = 3


@dataclass
class Transfer:
    block: int
    time: int
    sender: str
    recipient: str
    amount: int


@dataclass
class LaunchReport:
    token: str
    creation_block: int
    transfers_scanned: int
    first_trade_time: int | None
    early_recipients: list[str]
    identical: list[dict[str, Any]]
    smart_account_share: float | None


def _int(value: str) -> int:
    return int(value, 16)


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def scan_transfers(rpc: Rpc, token: str, creation_block: int, first_n: int) -> list[Transfer]:
    head = _int(rpc.request("eth_blockNumber", []))
    times: dict[int, int] = {}
    out: list[Transfer] = []
    lo, chunk = creation_block, CHUNK_BLOCKS
    while lo <= head and len(out) < first_n and chunk >= MIN_CHUNK_BLOCKS:
        hi = min(head, lo + chunk - 1)
        try:
            logs = rpc.request("eth_getLogs", [{"address": token, "topics": [TRANSFER],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}])
        except RuntimeError as exc:
            if "HTTP 400" not in str(exc) and "HTTP 413" not in str(exc) and "range" not in str(exc).lower():
                raise
            chunk //= 2
            log.info("Log range %d blocks refused, trying %d: %s", hi - lo + 1, chunk, exc)
            continue
        for lg in sorted(logs, key=lambda x: (_int(x["blockNumber"]), _int(x["logIndex"]))):
            if len(lg.get("topics", [])) != 3:
                continue
            block = _int(lg["blockNumber"])
            if block not in times:
                times[block] = _int(rpc.request("eth_getBlockByNumber", [hex(block), False])["timestamp"])
            out.append(Transfer(block, times[block], _addr(lg["topics"][1]), _addr(lg["topics"][2]),
                                _int(lg["data"])))
            if len(out) >= first_n:
                break
        lo = hi + 1
    return out


def analyze(rpc: Rpc, token: str, creation_block: int, transfers: list[Transfer], code_sample: int = 50) -> LaunchReport:
    recipients: list[str] = []
    by_amount: dict[int, list[Transfer]] = defaultdict(list)
    for t in transfers:
        if t.recipient in (ZERO, DEAD):
            continue
        by_amount[t.amount].append(t)
        if t.recipient not in recipients:
            recipients.append(t.recipient)

    identical = []
    for amount, ts in by_amount.items():
        if len(ts) >= MIN_IDENTICAL:
            identical.append({"amount_raw": str(amount), "wallets": len(ts),
                              "span_s": max(t.time for t in ts) - min(t.time for t in ts)})
    identical.sort(key=lambda d: -d["wallets"])

    sample = recipients[:code_sample]
    share = None
    if sample:
        delegated = sum(1 for a in sample if rpc.request("eth_getCode", [a, "latest"]).lower().startswith(EIP7702_PREFIX))
        share = round(delegated / len(sample), 3)

    return LaunchReport(token.lower(), creation_block, len(transfers),
                        transfers[0].time if transfers else None, recipients, identical, share)


def shared_recipients(reports: list[LaunchReport]) -> dict[str, list[str]]:
    where: dict[str, list[str]] = defaultdict(list)
    for r in reports:
        for a in r.early_recipients:
            if r.token not in where[a]:
                where[a].append(r.token)
    return {a: tokens for a, tokens in where.items() if len(tokens) > 1}


def check_token(rpc: Rpc, token: str, creation_tx: str, first_n: int = 200) -> LaunchReport:
    receipt = rpc.request("eth_getTransactionReceipt", [creation_tx])
    if not receipt or not receipt.get("blockNumber"):
        raise ValueError(f"creation transaction not found: {creation_tx}")
    creation_block = _int(receipt["blockNumber"])
    transfers = scan_transfers(rpc, token, creation_block, first_n)
    return analyze(rpc, token, creation_block, transfers)


def save(reports: list[LaunchReport], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reports": [asdict(r) for r in reports],
                             "shared": shared_recipients(reports)}, indent=1), encoding="utf-8")
