"""Live Pump.fun bonding-curve route via PumpPortal's Local Transaction API.

The feed and entry filters decide whether a new-token candidate is eligible. This
adapter only builds, signs locally, and submits the actual transaction. It does
not hold private keys outside this process.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from ..config import SniperConfig
from ..models import Fill, Position, TokenSnapshot
from ..portfolio import Portfolio
from . import ExecutionError

log = logging.getLogger(__name__)

TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
LAMPORTS_PER_SOL = 1_000_000_000


class PumpPortalExecutor:
    """Route pre-graduation buys through PumpPortal, other trades through Jupiter."""

    def __init__(
        self,
        jupiter: Any,
        market: Any,
        cfg: SniperConfig,
        client: httpx.Client | None = None,
        trade_url: str = TRADE_LOCAL_URL,
    ) -> None:
        self.jupiter = jupiter
        self.market = market
        self.cfg = cfg
        self.http = client or httpx.Client(timeout=10.0)
        self.trade_url = trade_url
        self.execution_cfg = jupiter.cfg
        self.keypair = jupiter.keypair
        self.owner = jupiter.owner
        self.rpc = jupiter.rpc

    def is_pregraduation(self, mint: str) -> bool:
        check = getattr(self.market, "is_pregraduation", None)
        return bool(check and check(mint))

    def preflight(self, snap: TokenSnapshot, usd: float) -> str | None:
        return self.jupiter.preflight(snap, usd)

    def preflight_pregraduation(self, snap: TokenSnapshot, usd: float) -> str | None:
        if not self.is_pregraduation(snap.mint):
            return "PumpPortal launch is no longer marked pre-graduation"
        if usd <= 0 or usd > self.cfg.max_position_usd:
            return f"sniper position ${usd:.2f} exceeds the ${self.cfg.max_position_usd:.2f} cap"
        if usd > self.available_usd(None):
            return "insufficient SOL after reserve for Pump.fun curve buy"
        # sendTransaction uses skipPreflight=false; the RPC simulates the signed
        # transaction immediately before forwarding it.
        return None

    def available_usd(self, pf: Portfolio | None) -> float:
        return self.jupiter.available_usd(pf)

    def sync_cash(self, pf: Portfolio) -> None:
        self.jupiter.sync_cash(pf)

    def buy(self, snap: TokenSnapshot, usd: float) -> Fill:
        return self.jupiter.buy(snap, usd)

    def buy_pregraduation(self, snap: TokenSnapshot, usd: float) -> Fill:
        if not self.is_pregraduation(snap.mint):
            raise ExecutionError("Pump.fun launch is no longer marked pre-graduation")
        if usd > self.cfg.max_position_usd:
            raise ExecutionError(f"sniper buy exceeds ${self.cfg.max_position_usd:.2f} position cap")
        sol_price = self.market.sol_price_usd()
        lamports = int(usd / sol_price * LAMPORTS_PER_SOL)
        spendable = int(self.available_usd(None) / sol_price * LAMPORTS_PER_SOL)
        if lamports <= 0 or lamports > spendable:
            raise ExecutionError("insufficient SOL after reserve for Pump.fun curve buy")
        before, _ = self.rpc.get_token_balance(self.owner, snap.mint)
        signature = self._submit(snap.mint, "buy", lamports / LAMPORTS_PER_SOL, True, "pump")
        if not self.rpc.confirm(signature, self.execution_cfg.confirm_timeout_s):
            log.warning("PumpPortal buy %s not confirmed within %.0fs; reconciling token balance",
                        signature, self.execution_cfg.confirm_timeout_s)
        after, decimals = self.jupiter._token_balance_after(snap.mint, before, rising=True)
        received = after - before
        if received <= 0:
            raise ExecutionError(f"PumpPortal buy {signature} did not change {snap.symbol} balance")
        tokens = received / 10**decimals
        log.info("Pump.fun curve BUY %s landed: %s", snap.symbol, signature)
        return Fill(token_amount=tokens, usd_value=usd, price_usd=usd / tokens, signature=signature)

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill:
        if pos.lane != "sniper" or pos.chain != "solana":
            return self.jupiter.sell(pos, fraction, snap)
        before_tokens, decimals = self.rpc.get_token_balance(self.owner, pos.mint)
        if before_tokens <= 0:
            raise ExecutionError(f"No {pos.symbol} balance to sell")
        amount: str | float = "100%" if fraction >= 0.999 else pos.token_amount * fraction
        if amount != "100%" and amount <= 0:
            raise ExecutionError(f"No {pos.symbol} amount to sell")
        before_sol = self.rpc.get_balance(self.owner)
        signature = self._submit(pos.mint, "sell", amount, False, "auto")
        if not self.rpc.confirm(signature, self.execution_cfg.confirm_timeout_s):
            log.warning("PumpPortal sell %s not confirmed within %.0fs; reconciling balances",
                        signature, self.execution_cfg.confirm_timeout_s)
        after_tokens, _ = self.jupiter._token_balance_after(pos.mint, before_tokens, rising=False)
        sold_raw = before_tokens - after_tokens
        if sold_raw <= 0:
            raise ExecutionError(f"PumpPortal sell {signature} did not change {pos.symbol} balance")
        received_lamports = self.rpc.get_balance(self.owner) - before_sol
        if received_lamports <= 0:
            raise ExecutionError(f"PumpPortal sell {signature} did not increase SOL balance")
        sold = sold_raw / 10**decimals
        usd = received_lamports / LAMPORTS_PER_SOL * self.market.sol_price_usd()
        log.info("PumpPortal curve/AMM SELL %s landed: %s", pos.symbol, signature)
        return Fill(token_amount=sold, usd_value=usd, price_usd=usd / sold, signature=signature)

    def _submit(self, mint: str, action: str, amount: str | float, in_sol: bool, pool: str) -> str:
        from solders.transaction import VersionedTransaction

        body = {
            "publicKey": self.owner,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if in_sol else "false",
            "slippage": max(0.1, self.execution_cfg.slippage_bps / 100),
            "priorityFee": self.execution_cfg.priority_fee_max_lamports / LAMPORTS_PER_SOL,
            "pool": pool,
        }
        try:
            response = self.http.post(self.trade_url, json=body)
            if response.status_code != 200:
                raise ExecutionError(f"PumpPortal transaction build failed ({response.status_code}): "
                                     f"{response.text[:240]}")
            unsigned = VersionedTransaction.from_bytes(response.content)
            signed = VersionedTransaction(unsigned.message, [self.keypair])
            return self.rpc.send_transaction(base64.b64encode(bytes(signed)).decode())
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionError(f"PumpPortal {action} failed: {exc}") from exc
