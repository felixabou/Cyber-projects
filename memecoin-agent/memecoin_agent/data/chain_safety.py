"""Honeypot and tax checks for tokens on chains other than Solana (copy trades, long-term holds).

GMGN's token security when a GMGN key is set (it covers every chain the copy lane watches), else
GoPlus's free token-security API (Ethereum, BSC, Base, Arbitrum...). Either one flags honeypots
(buys work, sells don't), high or changeable taxes, and owner powers over balances or transfers.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from ..models import SafetyReport
from .dexscreener import DEX_TO_GMGN
from .gmgn import GmgnClient, GmgnError, _f, _truthy

log = logging.getLogger(__name__)

GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
GOPLUS_CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453", "arbitrum": "42161", "polygon": "137",
                    "optimism": "10", "avalanche": "43114"}
MAX_TAX = 0.10
NO_CHECK = "no honeypot check available on this chain"


def goplus_reasons(d: dict[str, Any]) -> list[str]:
    flags = {"is_honeypot": "honeypot (it can't be sold)", "cannot_sell_all": "holders can't sell everything",
             "transfer_pausable": "the owner can pause transfers", "owner_change_balance": "the owner can change balances",
             "hidden_owner": "hidden owner", "slippage_modifiable": "the owner can change the tax",
             "is_blacklisted": "the owner can blacklist sellers", "selfdestruct": "the contract can self-destruct"}
    why = [msg for key, msg in flags.items() if str(d.get(key)) == "1"]
    for side in ("buy_tax", "sell_tax"):
        tax = _f(d.get(side))
        if tax is not None and tax > MAX_TAX:
            why.append(f"{side.replace('_', ' ')} {tax * 100:.0f}%")
    return why


def gmgn_reasons(d: dict[str, Any]) -> list[str]:
    why = []
    if str(d.get("is_honeypot")).lower() in ("yes", "1", "true"):
        why.append("honeypot (it can't be sold)")
    for side in ("buy_tax", "sell_tax"):
        tax = _f(d.get(side))
        if tax is not None and tax > MAX_TAX:
            why.append(f"{side.replace('_', ' ')} {tax * 100:.0f}%")
    if _truthy(d.get("is_blacklist")) or _truthy(d.get("can_not_sell")):
        why.append("sells can be blocked")
    return why


class ChainSafety:
    def __init__(self, gmgn: GmgnClient | None = None, http: httpx.Client | None = None, fail_closed: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.gmgn = gmgn
        self.http = http or httpx.Client(timeout=15.0)
        self.fail_closed = fail_closed
        self.clock = clock
        self._cache: dict[tuple[str, str], tuple[float, SafetyReport]] = {}

    def check(self, chain: str, address: str) -> SafetyReport:
        """chain: DexScreener's chain id."""
        hit = self._cache.get((chain, address))
        if hit and hit[0] > self.clock():
            return hit[1]
        rep = self._check(chain, address)
        if rep.reasons != [NO_CHECK]:
            self._cache[(chain, address)] = (self.clock() + 600, rep)
        return rep

    def _check(self, chain: str, address: str) -> SafetyReport:
        gchain = DEX_TO_GMGN.get(chain)
        if self.gmgn is not None and gchain:
            try:
                why = gmgn_reasons(self.gmgn.security(address, gchain))
                return SafetyReport(ok=not why, score=None, reasons=[f"gmgn: {w}" for w in why])
            except (httpx.HTTPError, GmgnError) as exc:
                log.info("GMGN security for %s on %s unavailable: %s", address, chain, exc)
        chain_id = GOPLUS_CHAIN_IDS.get(chain)
        if chain_id:
            try:
                resp = self.http.get(GOPLUS.format(chain_id=chain_id), params={"contract_addresses": address})
                resp.raise_for_status()
                result = (resp.json().get("result") or {})
                data = result.get(address.lower()) or next(iter(result.values()), None)
                if isinstance(data, dict):
                    why = goplus_reasons(data)
                    return SafetyReport(ok=not why, score=None, reasons=[f"goplus: {w}" for w in why])
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                log.info("GoPlus for %s on %s unavailable: %s", address, chain, exc)
        return SafetyReport(ok=not self.fail_closed, score=None, reasons=[NO_CHECK] if self.fail_closed else [])
