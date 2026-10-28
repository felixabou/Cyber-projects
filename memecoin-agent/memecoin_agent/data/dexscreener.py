"""DexScreener public API client (no key required).

Rate limits at time of writing: 60 req/min for profile/boost endpoints,
300 req/min for pair/token endpoints. One scan uses 3 discovery calls plus
one token call per 30 mints, which stays well inside both.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from ..models import TokenSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"
CHAIN = "solana"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Discovery feeds. These surface tokens that recently set up a profile or bought a boost,
# which skews toward actively promoted launches; add `filters.watchlist` for anything else.
DISCOVERY_PATHS = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1")
TOKENS_PER_REQUEST = 30
# GMGN's chain names -> DexScreener's chain ids (copy trading and long-term holds span chains).
GMGN_TO_DEX = {"sol": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base", "arbitrum": "arbitrum",
               "hyperevm": "hyperevm", "robinhood": "robinhood", "monad": "monad", "tron": "tron"}
DEX_TO_GMGN = {v: k for k, v in GMGN_TO_DEX.items()}
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def norm_address(chain: str, address: str) -> str:
    """Token addresses as the agent keys them: EVM hex lowercased (DexScreener returns checksummed
    case, users paste either), everything else as given."""
    address = (address or "").strip()
    return address.lower() if EVM_ADDRESS.match(address) else address


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _i(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_pair(pair: dict[str, Any]) -> TokenSnapshot | None:
    base = pair.get("baseToken") or {}
    price = _f(pair.get("priceUsd"))
    if not base.get("address") or price <= 0:
        return None
    txns = pair.get("txns") or {}
    info = pair.get("info") or {}
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    created_ms = pair.get("pairCreatedAt")
    created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None
    chain = pair.get("chainId") or CHAIN
    return TokenSnapshot(
        mint=norm_address(chain, base["address"]),
        chain=chain,
        symbol=base.get("symbol") or "?",
        name=base.get("name") or "",
        pair_address=pair.get("pairAddress") or "",
        dex_id=pair.get("dexId") or "",
        quote_symbol=(pair.get("quoteToken") or {}).get("symbol") or "",
        price_usd=price,
        liquidity_usd=_f((pair.get("liquidity") or {}).get("usd")),
        market_cap_usd=_f(pair.get("marketCap") or pair.get("fdv")),
        volume_m5=_f(vol.get("m5")),
        volume_h1=_f(vol.get("h1")),
        volume_h24=_f(vol.get("h24")),
        buys_m5=_i((txns.get("m5") or {}).get("buys")),
        sells_m5=_i((txns.get("m5") or {}).get("sells")),
        buys_h1=_i((txns.get("h1") or {}).get("buys")),
        sells_h1=_i((txns.get("h1") or {}).get("sells")),
        buys_h24=_i((txns.get("h24") or {}).get("buys")),
        sells_h24=_i((txns.get("h24") or {}).get("sells")),
        price_change_m5=_f(chg.get("m5")),
        price_change_h1=_f(chg.get("h1")),
        price_change_h6=_f(chg.get("h6")),
        price_change_h24=_f(chg.get("h24")),
        pair_created_at=created,
        url=pair.get("url") or "",
        links=[str(x["url"]) for x in [*(info.get("websites") or []), *(info.get("socials") or [])]
               if isinstance(x, dict) and x.get("url")],
    )


class DexScreenerClient:
    def __init__(
        self,
        quote_symbols: Iterable[str] = ("SOL", "USDC"),
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.quote_symbols = {s.upper() for s in quote_symbols}
        self.http = client or httpx.Client(timeout=15.0, headers={"Accept": "application/json"})
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str) -> Any:
        resp = self.http.get(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json()

    def discover(self) -> list[str]:
        seen: dict[str, None] = {}
        for path in DISCOVERY_PATHS:
            try:
                items = self._get(path)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener discovery %s failed: %s", path, exc)
                continue
            if isinstance(items, dict):  # some endpoints have wrapped results historically
                items = items.get("data") or items.get("tokens") or []
            for item in items or []:
                if item.get("chainId") == CHAIN and item.get("tokenAddress"):
                    seen.setdefault(item["tokenAddress"], None)
        return list(seen)

    def _pairs_for(self, mints: list[str]) -> list[dict[str, Any]]:
        pairs: list[dict[str, Any]] = []
        for i in range(0, len(mints), TOKENS_PER_REQUEST):
            chunk = mints[i : i + TOKENS_PER_REQUEST]
            try:
                data = self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener token lookup failed for %d mints: %s", len(chunk), exc)
                continue
            if isinstance(data, dict):
                data = data.get("pairs") or []
            pairs.extend(p for p in data or [] if p.get("chainId") == CHAIN)
        return pairs

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        wanted = list(dict.fromkeys(mints))
        best: dict[str, TokenSnapshot] = {}
        for pair in self._pairs_for(wanted):
            snap = parse_pair(pair)
            if snap is None or snap.mint not in wanted:
                continue
            if snap.quote_symbol.upper() not in self.quote_symbols:
                continue
            cur = best.get(snap.mint)
            if cur is None or snap.liquidity_usd > cur.liquidity_usd:
                best[snap.mint] = snap
        return best

    def snapshots_on(self, chain: str, addresses: Iterable[str]) -> dict[str, TokenSnapshot]:
        """Most liquid pair per token on any chain DexScreener covers (any quote token)."""
        wanted = list(dict.fromkeys(norm_address(chain, a) for a in addresses))
        best: dict[str, TokenSnapshot] = {}
        for i in range(0, len(wanted), TOKENS_PER_REQUEST):
            chunk = wanted[i : i + TOKENS_PER_REQUEST]
            try:
                data = self._get(f"/tokens/v1/{chain}/{','.join(chunk)}")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener %s lookup failed for %d tokens: %s", chain, len(chunk), exc)
                continue
            for pair in (data.get("pairs") or [] if isinstance(data, dict) else data) or []:
                snap = parse_pair(pair) if pair.get("chainId") == chain else None
                if snap is None or snap.mint not in chunk:
                    continue
                if snap.mint not in best or snap.liquidity_usd > best[snap.mint].liquidity_usd:
                    best[snap.mint] = snap
        return best

    def lookup(self, query: str, limit: int = 12) -> list[TokenSnapshot]:
        """Tokens matching a contract address (any chain) or a name/symbol: the most liquid pair of
        each, most traded first (fake copies of a name often show huge liquidity and no volume).
        For the dashboard's manual trades."""
        q = query.strip()
        if not q:
            return []
        is_address = bool(EVM_ADDRESS.match(q)) or (len(q) >= 32 and q.isalnum())
        path = f"/latest/dex/tokens/{q}" if is_address else f"/latest/dex/search?q={quote(q)}"
        data = self._get(path)
        pairs = data.get("pairs") if isinstance(data, dict) else data
        best: dict[tuple[str, str], TokenSnapshot] = {}
        for pair in pairs or []:
            snap = parse_pair(pair)
            if snap is None:
                continue
            if is_address and snap.mint != norm_address(snap.chain, q):
                continue  # a pool where this token is the quote side
            key = (snap.chain, snap.mint)
            if key not in best or snap.liquidity_usd > best[key].liquidity_usd:
                best[key] = snap
        return sorted(best.values(), key=lambda s: (s.volume_h24, s.liquidity_usd), reverse=True)[:limit]

    def sol_price_usd(self) -> float:
        best_liq, price = 0.0, 0.0
        for pair in self._pairs_for([WSOL_MINT]):
            base = (pair.get("baseToken") or {}).get("address")
            quote = (pair.get("quoteToken") or {}).get("address")
            liq = _f((pair.get("liquidity") or {}).get("usd"))
            if base == WSOL_MINT and quote == USDC_MINT and liq > best_liq:
                best_liq, price = liq, _f(pair.get("priceUsd"))
        if price <= 0:
            raise RuntimeError("Could not determine SOL/USD price from DexScreener")
        return price
