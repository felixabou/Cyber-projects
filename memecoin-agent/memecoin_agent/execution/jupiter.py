"""Live executor: swaps through Jupiter, signs locally, submits via your RPC.

Fills are measured from on-chain balance changes rather than trusting the
quote, so the ledger reflects what actually landed (including a transaction
that confirmed after our confirmation wait timed out).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import ExecutionConfig
from ..data import MarketData
from ..data.dexscreener import WSOL_MINT
from ..models import Fill, Position, TokenSnapshot
from ..portfolio import Portfolio
from . import ExecutionError
from .solana_rpc import SolanaRpc

log = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


def load_keypair() -> Any:
    """Load the trading wallet from SOLANA_PRIVATE_KEY (base58 or JSON array) or SOLANA_KEYPAIR_PATH."""
    try:
        from solders.keypair import Keypair
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ExecutionError("Live mode needs the 'solders' package: pip install 'memecoin-agent[live]'") from exc

    secret = os.environ.get("SOLANA_PRIVATE_KEY")
    path = os.environ.get("SOLANA_KEYPAIR_PATH")
    if secret:
        secret = secret.strip()
        if secret.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(secret)))
        return Keypair.from_base58_string(secret)
    if path:
        return Keypair.from_bytes(bytes(json.loads(Path(path).expanduser().read_text())))
    raise ExecutionError("Set SOLANA_PRIVATE_KEY or SOLANA_KEYPAIR_PATH for live trading")


class JupiterExecutor:
    def __init__(
        self,
        cfg: ExecutionConfig,
        keypair: Any,
        market: MarketData,
        rpc: SolanaRpc | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.keypair = keypair
        self.owner = str(keypair.pubkey())
        self.market = market
        self.rpc = rpc or SolanaRpc(cfg.rpc_url)
        headers = {"x-api-key": cfg.jupiter_api_key} if cfg.jupiter_api_key else {}
        self.http = client or httpx.Client(timeout=20.0, headers=headers)
        self.base = cfg.jupiter_base_url.rstrip("/")
        self.sleep = sleep

    # -- Jupiter ------------------------------------------------------------------
    def quote(self, input_mint: str, output_mint: str, amount: int) -> dict[str, Any]:
        resp = self.http.get(
            f"{self.base}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(self.cfg.slippage_bps),
                "restrictIntermediateTokens": "true",
            },
        )
        resp.raise_for_status()
        q = resp.json()
        if "outAmount" not in q:
            raise ExecutionError(f"No Jupiter route {input_mint} -> {output_mint}: {q}")
        return q

    def _swap(self, quote: dict[str, Any]) -> str:
        from solders.transaction import VersionedTransaction

        resp = self.http.post(
            f"{self.base}/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": self.owner,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": self.cfg.priority_fee_max_lamports,
                        "priorityLevel": "veryHigh",
                    }
                },
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if "swapTransaction" not in body:
            raise ExecutionError(f"Jupiter swap build failed: {body}")
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(body["swapTransaction"]))
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        sig = self.rpc.send_transaction(base64.b64encode(bytes(signed)).decode())
        if not self.rpc.confirm(sig, self.cfg.confirm_timeout_s):
            log.warning("Swap %s not confirmed within %.0fs; reconciling from balances", sig, self.cfg.confirm_timeout_s)
        return sig

    def _token_balance_after(self, mint: str, before: int, rising: bool) -> tuple[int, int]:
        """Poll briefly: RPC nodes can lag a moment behind confirmation."""
        raw, decimals = self.rpc.get_token_balance(self.owner, mint)
        for _ in range(5):
            if (raw > before) if rising else (raw < before):
                break
            self.sleep(2.0)
            raw, decimals = self.rpc.get_token_balance(self.owner, mint)
        return raw, decimals

    # -- Executor interface ---------------------------------------------------------
    def _spendable_lamports(self) -> int:
        reserve = int(self.cfg.min_sol_reserve * LAMPORTS_PER_SOL)
        return max(0, self.rpc.get_balance(self.owner) - reserve)

    def available_usd(self, pf: Portfolio) -> float:
        return self._spendable_lamports() / LAMPORTS_PER_SOL * self.market.sol_price_usd()

    def sync_cash(self, pf: Portfolio) -> None:
        try:
            lamports = self.rpc.get_balance(self.owner)
            pf.cash_usd = lamports / LAMPORTS_PER_SOL * self.market.sol_price_usd()
        except Exception as exc:  # keep last known cash if RPC/price is briefly unavailable
            log.warning("Could not sync wallet balance: %s", exc)

    def preflight(self, snap: TokenSnapshot, usd: float) -> str | None:
        """Honeypot / thin-route guard: quote the buy, then quote selling it straight back."""
        try:
            lamports = int(usd / self.market.sol_price_usd() * LAMPORTS_PER_SOL)
            buy_q = self.quote(WSOL_MINT, snap.mint, lamports)
            sell_q = self.quote(snap.mint, WSOL_MINT, int(buy_q["outAmount"]))
        except (httpx.HTTPError, ExecutionError, ValueError, RuntimeError) as exc:
            return f"quote failed: {exc}"
        loss = (1 - int(sell_q["outAmount"]) / lamports) * 100 if lamports else 100.0
        if loss > self.cfg.max_roundtrip_loss_pct:
            return f"round-trip loss {loss:.1f}% > {self.cfg.max_roundtrip_loss_pct:.1f}%"
        return None

    def buy(self, snap: TokenSnapshot, usd: float) -> Fill:
        lamports = int(usd / self.market.sol_price_usd() * LAMPORTS_PER_SOL)
        if lamports > self._spendable_lamports():
            raise ExecutionError(f"Insufficient SOL for ${usd:.2f} buy (keeping {self.cfg.min_sol_reserve} SOL reserve)")
        before, _ = self.rpc.get_token_balance(self.owner, snap.mint)
        sig = self._swap(self.quote(WSOL_MINT, snap.mint, lamports))
        after, decimals = self._token_balance_after(snap.mint, before, rising=True)
        received = after - before
        if received <= 0:
            raise ExecutionError(f"Buy {sig} did not change {snap.symbol} balance")
        tokens = received / 10**decimals
        log.info("BUY %s landed: %s", snap.symbol, sig)
        return Fill(token_amount=tokens, usd_value=usd, price_usd=usd / tokens, signature=sig)

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill:
        balance, _ = self.rpc.get_token_balance(self.owner, pos.mint)
        amount = balance if fraction >= 0.999 else int(balance * fraction)
        if amount <= 0:
            raise ExecutionError(f"No {pos.symbol} balance to sell")
        sol_before = self.rpc.get_balance(self.owner)
        quote = self.quote(pos.mint, WSOL_MINT, amount)
        sig = self._swap(quote)
        after, decimals = self._token_balance_after(pos.mint, balance, rising=False)
        sold = balance - after
        if sold <= 0:
            raise ExecutionError(f"Sell {sig} did not change {pos.symbol} balance")
        received = self.rpc.get_balance(self.owner) - sol_before
        if received <= 0:  # balance read raced the tx; fall back to the quoted minimum
            received = int(quote.get("otherAmountThreshold") or quote["outAmount"])
        usd = received / LAMPORTS_PER_SOL * self.market.sol_price_usd()
        tokens = sold / 10**decimals
        log.info("SELL %s landed: %s", pos.symbol, sig)
        return Fill(token_amount=tokens, usd_value=usd, price_usd=usd / tokens, signature=sig)
