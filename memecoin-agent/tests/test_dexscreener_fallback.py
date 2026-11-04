"""A token whose batch lookup returns only a junk pool still gets its real pools from the per-token endpoint."""

from __future__ import annotations

import httpx

from memecoin_agent.data.dexscreener import DexScreenerClient

MINT = "6i3oFRo48Xbnu7B9YH5DgypHRvsr47GagoJhSvs4pump"


def pair(quote, price, liq, dex="meteora"):
    p = {"chainId": "solana", "dexId": dex, "pairAddress": f"{dex}{quote}",
         "baseToken": {"address": MINT, "symbol": "Other", "name": "Other Cat"},
         "quoteToken": {"address": "q", "symbol": quote}, "priceNative": "0.000002"}
    if price:
        p["priceUsd"] = str(price)
        p["liquidity"] = {"usd": liq}
        p["marketCap"] = 382_000
    return p


def test_batch_junk_pool_falls_back_to_token_pairs():
    def api(req: httpx.Request) -> httpx.Response:
        if "/tokens/v1/" in req.url.path:
            return httpx.Response(200, json=[pair("POLYMARKET", None, 0, "pumpswap")])
        assert "/token-pairs/v1/solana/" in req.url.path
        return httpx.Response(200, json=[pair("SOL", 0.0003579, 17_391), pair("SOL", 0.000386, 522),
                                         pair("POLYMARKET", None, 0, "pumpswap")])

    dex = DexScreenerClient(client=httpx.Client(transport=httpx.MockTransport(api)))
    got = dex.snapshots_on("solana", [MINT])
    assert got[MINT].liquidity_usd == 17_391 and got[MINT].quote_symbol == "SOL"
