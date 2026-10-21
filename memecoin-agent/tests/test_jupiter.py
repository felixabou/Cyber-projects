"""Live executor against mocked Jupiter + RPC, with real transaction signing."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

solders = pytest.importorskip("solders")
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0, to_bytes_versioned  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from memecoin_agent.config import ExecutionConfig  # noqa: E402
from memecoin_agent.data.dexscreener import WSOL_MINT  # noqa: E402
from memecoin_agent.execution import ExecutionError  # noqa: E402
from memecoin_agent.execution.jupiter import JupiterExecutor, load_keypair  # noqa: E402
from memecoin_agent.execution.solana_rpc import SolanaRpc  # noqa: E402
from memecoin_agent.portfolio import Portfolio  # noqa: E402

from .conftest import NOW, make_snap  # noqa: E402

MINT = "MINT1"
SOL_PRICE = 150.0


class Chain:
    """Toy chain state: a submitted swap moves balances as quoted."""

    def __init__(self, kp: Keypair, lamports: int, roundtrip_keep: float = 0.95):
        self.kp = kp
        self.lamports = lamports
        self.tokens = 0
        self.roundtrip_keep = roundtrip_keep
        self.last_quote = None
        self.sent: list[VersionedTransaction] = []

    def unsigned_tx_b64(self) -> str:
        ix = transfer(TransferParams(from_pubkey=self.kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
        msg = MessageV0.try_compile(self.kp.pubkey(), [ix], [], Hash.default())
        return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()

    def jupiter(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/quote"):
            q = dict(req.url.params)
            amt = int(q["amount"])
            # 1 SOL (1e9 lamports) buys 1e12 raw tokens (6 decimals => 1,000,000 tokens).
            out = amt * 1000 if q["inputMint"] == WSOL_MINT else int(amt / 1000 * self.roundtrip_keep)
            quote = {**q, "outAmount": str(out), "otherAmountThreshold": str(int(out * 0.97))}
            return httpx.Response(200, json=quote)
        if req.url.path.endswith("/swap"):
            body = json.loads(req.content)
            assert body["userPublicKey"] == str(self.kp.pubkey())
            self.last_quote = body["quoteResponse"]
            return httpx.Response(200, json={"swapTransaction": self.unsigned_tx_b64()})
        return httpx.Response(404)

    def rpc(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        method, params = body["method"], body["params"]
        if method == "getBalance":
            result = {"value": self.lamports}
        elif method == "getTokenAccountsByOwner":
            amt = {"amount": str(self.tokens), "decimals": 6}
            result = {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": amt}}}}}]}
        elif method == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
            self.sent.append(tx)
            q = self.last_quote
            if q["inputMint"] == WSOL_MINT:
                self.lamports -= int(q["amount"]) + 5000
                self.tokens += int(q["outAmount"])
            else:
                self.tokens -= int(q["amount"])
                self.lamports += int(q["outAmount"]) - 5000
            result = str(tx.signatures[0])
        elif method == "getSignatureStatuses":
            result = {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"message": method}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


class Market:
    def sol_price_usd(self):
        return SOL_PRICE


def make_executor(chain: Chain) -> JupiterExecutor:
    rpc = SolanaRpc("http://rpc", client=httpx.Client(transport=httpx.MockTransport(chain.rpc)), sleep=lambda s: None)
    return JupiterExecutor(ExecutionConfig(mode="live"), chain.kp, Market(), rpc=rpc,
                           client=httpx.Client(transport=httpx.MockTransport(chain.jupiter)), sleep=lambda s: None)


def test_buy_and_sell_sign_and_reconcile_from_balances():
    kp = Keypair()
    chain = Chain(kp, lamports=2_000_000_000)  # 2 SOL = $300
    ex = make_executor(chain)
    pf = Portfolio(cash_usd=0)
    ex.sync_cash(pf)
    assert pf.cash_usd == pytest.approx(300.0)
    assert ex.available_usd(pf) == pytest.approx((2.0 - 0.05) * SOL_PRICE)

    snap = make_snap(mint=MINT)
    assert ex.preflight(snap, 30.0) is None
    fill = ex.buy(snap, 30.0)  # 0.2 SOL -> 200,000 tokens
    assert fill.token_amount == pytest.approx(200_000)
    assert fill.signature

    # The submitted transaction carries a valid signature from our wallet.
    tx = chain.sent[-1]
    assert tx.signatures[0].verify(kp.pubkey(), to_bytes_versioned(tx.message))

    pos = pf.open(snap, fill, NOW, 0.8)
    part = ex.sell(pos, 0.5, snap)
    assert part.token_amount == pytest.approx(100_000)
    assert part.usd_value == pytest.approx((0.1 * 0.95 - 0.000005) * SOL_PRICE)
    pf.reduce(MINT, 0.5, part, NOW, "tp")

    rest = ex.sell(pf.positions[MINT], 1.0, snap)
    assert rest.token_amount == pytest.approx(100_000)
    assert chain.tokens == 0


def test_preflight_rejects_honeypot_and_buy_respects_reserve():
    chain = Chain(Keypair(), lamports=200_000_000, roundtrip_keep=0.5)  # 0.2 SOL; sells lose 50%
    ex = make_executor(chain)
    assert "round-trip loss" in ex.preflight(make_snap(mint=MINT), 10.0)
    with pytest.raises(ExecutionError, match="Insufficient SOL"):
        ex.buy(make_snap(mint=MINT), 25.0)  # 0.1667 SOL > 0.2 - 0.05 reserve


def test_failed_transaction_raises():
    chain = Chain(Keypair(), lamports=2_000_000_000)
    ex = make_executor(chain)
    orig = chain.rpc

    def failing(req):
        if json.loads(req.content)["method"] == "getSignatureStatuses":
            body = json.loads(req.content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"value": [{"err": {"InstructionError": [0, "Custom"]}}]}})
        return orig(req)

    ex.rpc.http = httpx.Client(transport=httpx.MockTransport(failing))
    with pytest.raises(ExecutionError, match="failed"):
        ex.buy(make_snap(mint=MINT), 30.0)


def test_load_keypair_formats(monkeypatch, tmp_path):
    kp = Keypair()
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(kp))
    assert load_keypair().pubkey() == kp.pubkey()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", json.dumps(list(bytes(kp))))
    assert load_keypair().pubkey() == kp.pubkey()
    monkeypatch.delenv("SOLANA_PRIVATE_KEY")
    path = tmp_path / "id.json"
    path.write_text(json.dumps(list(bytes(kp))))
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", str(path))
    assert load_keypair().pubkey() == kp.pubkey()
