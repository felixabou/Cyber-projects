"""DexScreener public API client (no key required).

Rate limits at time of writing: 60 req/min for profile/boost endpoints,
300 req/min for pair/token endpoints. One scan uses 3 discovery calls plus
one token call per 30 mints, which stays well inside both.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
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
# Chain names are not consistent across feeds: DexScreener calls the BNB chain ``bsc`` while
# wallets and users commonly call it ``bnb`` (and ``bnc`` is a frequent typo). Keep one canonical
# id internally so discovery, market data and honeypot checks cannot silently split the same chain.
CHAIN_ALIASES = {
    "sol": "solana", "solana": "solana",
    "eth": "ethereum", "ethereum": "ethereum",
    "bsc": "bsc", "bnb": "bsc", "bnbchain": "bsc", "binance-smart-chain": "bsc", "bnc": "bsc",
    "base": "base", "arb": "arbitrum", "arb1": "arbitrum", "arbitrum": "arbitrum",
    "polygon": "polygon", "matic": "polygon", "poly": "polygon",
    "optimism": "optimism", "op": "optimism",
    "avalanche": "avalanche", "avax": "avalanche",
    "fantom": "fantom", "ftm": "fantom", "linea": "linea", "scroll": "scroll",
    "zksync": "zksync", "zksync-era": "zksync", "blast": "blast", "mantle": "mantle",
    "celo": "celo", "gnosis": "gnosis", "xdai": "gnosis", "cronos": "cronos",
    "moonbeam": "moonbeam", "metis": "metis", "opbnb": "opbnb", "manta": "manta",
    "mode": "mode", "zora": "zora", "sui": "sui", "ton": "ton", "tron": "tron",
    "monad": "monad", "robinhood": "robinhood", "hyperevm": "hyperevm",
}


def canonical_chain(chain: str | None) -> str:
    """Return the chain id used internally and by DexScreener endpoints."""
    raw = str(chain or CHAIN).strip().lower()
    return CHAIN_ALIASES.get(raw, raw)


# GMGN's chain names -> DexScreener's chain ids (copy trading and long-term holds span chains).
GMGN_TO_DEX = {
    "sol": "solana", "eth": "ethereum", "bsc": "bsc", "bnb": "bsc", "bnc": "bsc",
    "base": "base", "arb": "arbitrum", "arbitrum": "arbitrum",
    "polygon": "polygon", "optimism": "optimism", "avalanche": "avalanche",
    "hyperevm": "hyperevm", "robinhood": "robinhood", "monad": "monad", "tron": "tron",
    "fantom": "fantom", "linea": "linea", "scroll": "scroll", "zksync": "zksync",
    "blast": "blast", "mantle": "mantle", "celo": "celo", "gnosis": "gnosis", "cronos": "cronos",
    "moonbeam": "moonbeam", "metis": "metis", "opbnb": "opbnb", "manta": "manta", "mode": "mode",
    "zora": "zora", "sui": "sui", "ton": "ton",
}
# Keep canonical GMGN spellings when aliases are present (otherwise the last alias, e.g. ``bnc``,
# would accidentally be sent to the API for every BSC safety request).
DEX_TO_GMGN = {v: k for k, v in GMGN_TO_DEX.items() if k not in {"bnb", "bnc", "arb"}}
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
    chain = canonical_chain(pair.get("chainId") or CHAIN)
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
        discovery_chains: Iterable[str] = (CHAIN,),
    ) -> None:
        self.quote_symbols = {s.upper() for s in quote_symbols}
        self.http = client or httpx.Client(timeout=15.0, headers={"Accept": "application/json"})
        self.base_url = base_url.rstrip("/")
        self.discovery_chains = {canonical_chain(c) for c in discovery_chains}
        self._discovered_chains: dict[str, str] = {}

    def _get(self, path: str) -> Any:
        resp = self.http.get(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json()

    def discover(self) -> list[str]:
        seen: dict[str, None] = {}
        # These endpoints are independent. Preserve their configured priority while
        # waiting only as long as the slowest response, not the sum of all three.
        with ThreadPoolExecutor(max_workers=len(DISCOVERY_PATHS)) as pool:
            requests = [(path, pool.submit(self._get, path)) for path in DISCOVERY_PATHS]
            pages = []
            for path, request in requests:
                try:
                    pages.append(request.result())
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("DexScreener discovery %s failed: %s", path, exc)
                    pages.append([])
        for items in pages:
            if isinstance(items, dict):  # some endpoints have wrapped results historically
                items = items.get("data") or items.get("tokens") or []
            for item in items or []:
                chain = canonical_chain(item.get("chainId")) if item.get("chainId") else ""
                address = norm_address(chain, str(item.get("tokenAddress") or ""))
                if ("*" in self.discovery_chains or chain in self.discovery_chains) and address:
                    seen.setdefault(address, None)
                    self._discovered_chains[address] = chain
        return list(seen)

    def chain_of(self, mint: str) -> str | None:
        return self._discovered_chains.get(mint)

    def _pairs_for(self, mints: list[str]) -> list[dict[str, Any]]:
        chunks = [mints[i : i + TOKENS_PER_REQUEST] for i in range(0, len(mints), TOKENS_PER_REQUEST)]

        def fetch(chunk: list[str]) -> list[dict[str, Any]]:
            try:
                data = self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener token lookup failed for %d mints: %s", len(chunk), exc)
                return []
            if isinstance(data, dict):
                data = data.get("pairs") or []
            return [p for p in data or [] if p.get("chainId") == CHAIN]

        if not chunks:
            return []
        if len(chunks) == 1:
            return fetch(chunks[0])
        # A standard scan may request 150 tokens (five batches); each remains
        # independent and results retain input order for stable pair selection.
        with ThreadPoolExecutor(max_workers=min(5, len(chunks))) as pool:
            pages = list(pool.map(fetch, chunks))
        pairs: list[dict[str, Any]] = []
        for page in pages:
            pairs.extend(page)
        return pairs

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        wanted = list(dict.fromkeys(mints))
        best: dict[str, TokenSnapshot] = {}
        # Discovery records the chain for each mint. Keep the historic Solana path (which applies the
        # configured quote allow-list) but fetch non-Solana mints through the chain-aware endpoint so
        # BSC/BNB and every other DexScreener chain can be recorded and paper-traded too.
        solana = [m for m in wanted if canonical_chain(self._discovered_chains.get(m)) == CHAIN]
        other: dict[str, list[str]] = {}
        for mint in wanted:
            chain = canonical_chain(self._discovered_chains.get(mint))
            if chain != CHAIN:
                other.setdefault(chain, []).append(mint)
        for pair in self._pairs_for(solana):
            snap = parse_pair(pair)
            if snap is None or snap.mint not in wanted:
                continue
            if snap.quote_symbol.upper() not in self.quote_symbols:
                continue
            cur = best.get(snap.mint)
            if cur is None or snap.liquidity_usd > cur.liquidity_usd:
                best[snap.mint] = snap
        for chain, mints in other.items():
            best.update(self.snapshots_on(chain, mints))
        return best

    def snapshots_on(self, chain: str, addresses: Iterable[str]) -> dict[str, TokenSnapshot]:
        """Most liquid pair per token on any chain DexScreener covers (any quote token)."""
        chain = canonical_chain(chain)
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
        # The batch endpoint can answer with a single junk pool (no liquidity or price, quoted in some
        # unrelated coin) while the token's real pools are only listed by the per-token endpoint.
        for mint in wanted:
            if mint in best and best[mint].liquidity_usd > 0:
                continue
            try:
                data = self._get(f"/token-pairs/v1/{chain}/{mint}")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener %s pool lookup failed for %s: %s", chain, mint, exc)
                continue
            for pair in (data.get("pairs") or [] if isinstance(data, dict) else data) or []:
                snap = parse_pair(pair) if pair.get("chainId") == chain else None
                if snap is None or snap.mint != mint:
                    continue
                if mint not in best or snap.liquidity_usd > best[mint].liquidity_usd:
                    best[mint] = snap
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

