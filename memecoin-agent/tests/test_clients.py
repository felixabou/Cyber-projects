from __future__ import annotations

import httpx
import pytest

from memecoin_agent.data.dexscreener import USDC_MINT, WSOL_MINT, DexScreenerClient
from memecoin_agent.data.rugcheck import RugCheckClient


def pair(mint, quote="SOL", liq=50_000.0, price="0.0012", chain="solana", quote_addr=WSOL_MINT):
    return {
        "chainId": chain, "dexId": "raydium", "url": f"https://dexscreener.com/solana/{mint}",
        "pairAddress": f"{mint}-{quote}", "baseToken": {"address": mint, "name": "Tok", "symbol": "TOK"},
        "quoteToken": {"address": quote_addr, "symbol": quote}, "priceUsd": price,
        "txns": {"m5": {"buys": 10, "sells": 5}, "h1": {"buys": 200, "sells": 100}},
        "volume": {"m5": 1000, "h1": 30000, "h24": 200000}, "priceChange": {"m5": 2, "h1": 20, "h24": 50},
        "liquidity": {"usd": liq}, "marketCap": 400000, "pairCreatedAt": 1767225600000,
    }


def test_dexscreener_discover_and_snapshots():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/token-profiles/latest/v1":
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "A"},
                                             {"chainId": "base", "tokenAddress": "0xB"}])
        if req.url.path == "/token-boosts/latest/v1":
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "A"},
                                             {"chainId": "solana", "tokenAddress": "C"}])
        if req.url.path == "/token-boosts/top/v1":
            return httpx.Response(500)
        if req.url.path.startswith("/tokens/v1/solana/"):
            return httpx.Response(200, json=[
                pair("A", liq=10_000), pair("A", liq=90_000), pair("A", quote="BONK", liq=500_000),
                pair("C", price=None),
            ])
        return httpx.Response(404)

    client = DexScreenerClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.discover() == ["A", "C"]  # deduped, non-Solana dropped, failing feed tolerated
    snaps = client.snapshots(["A", "C"])
    assert set(snaps) == {"A"}  # C has no price
    assert snaps["A"].liquidity_usd == 90_000  # best pair with an allowed quote
    assert snaps["A"].buys_h1 == 200 and snaps["A"].pair_created_at.year == 2026


def test_dexscreener_sol_price():
    def handler(req):
        return httpx.Response(200, json=[
            pair(WSOL_MINT, quote="USDC", liq=1e6, price="140", quote_addr=USDC_MINT),
            pair(WSOL_MINT, quote="USDC", liq=5e7, price="151.5", quote_addr=USDC_MINT),
        ])

    client = DexScreenerClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.sol_price_usd() == 151.5


def test_rugcheck_evaluation_and_caching():
    hits = []
    reports = {
        "SAFE": {"score_normalised": 3, "risks": [{"name": "Mutable metadata", "level": "warn"}], "lpLockedPct": 100},
        "MINTABLE": {"score_normalised": 20, "risks": [{"name": "Mint Authority still enabled", "level": "danger"}]},
        "RISKY": {"score_normalised": 85, "risks": []},
    }

    def handler(req):
        mint = req.url.path.split("/")[3]
        hits.append(mint)
        return httpx.Response(200, json=reports[mint]) if mint in reports else httpx.Response(503)

    rc = RugCheckClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert rc.check("SAFE").ok
    assert rc.check("SAFE").ok and hits.count("SAFE") == 1  # cached
    assert "Mint Authority" in rc.check("MINTABLE").reasons[0]
    assert not rc.check("RISKY").ok
    down = rc.check("DOWN")
    assert not down.ok and down.reasons == ["rugcheck unavailable"]  # fails closed
    rc.check("DOWN")
    assert hits.count("DOWN") == 2  # failures are not cached

    lp = RugCheckClient(min_lp_locked_pct=90, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert lp.check("SAFE").ok and not lp.check("MINTABLE").ok
