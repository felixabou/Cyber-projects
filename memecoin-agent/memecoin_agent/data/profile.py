"""What a token is (tech or meme) and whether its chart looks real.

Two free reads per candidate that passes the filters, both cached:
- RugCheck's full report: the holder count, RugCheck's own risk flags (e.g. "High holder
  correlation": wallets that move together) and the link to the token's metadata file.
- The metadata file itself (name, description, links). ipfs.io and dweb.link now refuse direct
  requests, so IPFS links are read through Pinata's public gateway.

Tech vs meme is a best guess from words and links. When in doubt the token counts as tech, so it
gets the stricter LarpCheck treatment.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from ..config import AuthenticityConfig, ClassifyConfig
from ..models import TokenSnapshot

log = logging.getLogger(__name__)

RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
IPFS_GATEWAY = "https://gateway.pinata.cloud/ipfs/"
REPORT_TTL_S = 300.0

# A word (camelCase split, plural "s" ignored) that claims a product.
TECH_WORDS = {
    "launchpad", "pad", "protocol", "platform", "app", "dapp", "sdk", "api", "dex", "swap", "exchange", "bridge",
    "staking", "stake", "yield", "lending", "vault", "oracle", "layer", "network", "net", "infra", "infrastructure",
    "terminal", "bot", "agent", "ai", "gpt", "llm", "lab", "labs", "finance", "fi", "defi", "tool", "wallet",
    "analytics", "aggregator", "marketplace", "forum", "dao", "tech", "cloud", "compute", "perp", "perps",
    "framework", "engine", "game", "gaming", "metaverse", "chain", "blockchain", "whitepaper", "roadmap", "utility",
}
SYMBOL_SUFFIXES = ("pad", "swap", "dex", "ai", "bot", "gpt", "fi", "labs", "net")
TECH_TLDS = (".app", ".tech", ".ai", ".io", ".dev", ".network", ".finance", ".exchange", ".tools")


def words(text: str) -> set[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")  # HidraNet -> Hidra Net
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])
    return out


@dataclass
class TokenProfile:
    mint: str
    kind: str = "tech"  # "tech" or "meme"
    kind_reasons: list[str] = field(default_factory=list)
    holders: int | None = None
    description: str = ""
    links: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)  # RugCheck risk names
    fetched_at: float = 0.0


def classify(name: str, symbol: str, description: str, links: list[str],
             extra_words: list[str] | None = None) -> tuple[str, list[str]]:
    """("tech" | "meme", why). A token counts as tech on any product signal."""
    vocab = TECH_WORDS | {w.lower() for w in extra_words or []}
    why: list[str] = []
    hit = sorted((words(name) | words(symbol)) & vocab)
    sym = (symbol or "").lower()
    if hit:
        why.append(f"name says {hit[0]}")
    elif len(sym) > 3 and sym.endswith(SYMBOL_SUFFIXES):
        why.append(f"symbol ends in {next(s for s in SYMBOL_SUFFIXES if sym.endswith(s)).upper()}")
    desc_hits = sorted(words(description) & vocab)
    if desc_hits:
        why.append("description mentions " + ", ".join(desc_hits[:3]))
    for url in links:
        host = (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()
        if host in ("github.com", "gitlab.com"):
            why.append("links a code repo")
        elif host.startswith("docs.") or "gitbook" in host:
            why.append("links docs")
        elif host.startswith("app.") or host.endswith(TECH_TLDS):
            why.append(f"site {host}")
    why = list(dict.fromkeys(why))
    return ("tech", why) if why else ("meme", ["no product claims in name, description or links"])


def authenticity_reasons(s: TokenSnapshot, p: TokenProfile | None, cfg: AuthenticityConfig, age_minutes: float | None) -> list[str]:
    """Why the chart looks manipulated (empty: it doesn't)."""
    reasons: list[str] = []
    hours = min(max((age_minutes or 60) / 60, 1.0), 24.0)
    if s.liquidity_usd > 0:
        turnover = s.volume_h24 / s.liquidity_usd / hours
        if turnover > cfg.max_turnover_per_hour:
            reasons.append(f"wash trading: volume is {turnover:.0f}x its liquidity per hour (limit {cfg.max_turnover_per_hour:g}x)")
    holders = p.holders if p else None
    if holders is not None:
        if holders < cfg.min_holders:
            reasons.append(f"only {holders} holders (min {cfg.min_holders})")
        elif s.volume_h24 / holders > cfg.max_volume_per_holder_usd:
            reasons.append(f"wash trading: ${s.volume_h24 / holders:,.0f} of volume per holder "
                           f"(limit ${cfg.max_volume_per_holder_usd:,.0f})")
        if holders and s.txns_h1 / holders > cfg.max_trades_per_holder_h1:
            reasons.append(f"bot churn: {s.txns_h1 / holders:.0f} trades per holder in the last hour "
                           f"(limit {cfg.max_trades_per_holder_h1:g})")
    if p and cfg.reject_holder_correlation and any("holder correlation" in r.lower() for r in p.risks):
        reasons.append("coordinated wallets (RugCheck: high holder correlation)")
    return reasons


class TokenProfiler:
    def __init__(self, classify_cfg: ClassifyConfig, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = classify_cfg
        self.http = http or httpx.Client(timeout=15.0, follow_redirects=True, headers={"Accept": "application/json"})
        self.clock = clock
        self._profiles: dict[str, TokenProfile] = {}
        self._meta: dict[str, tuple[str, list[str]]] = {}  # mint -> (description, links): never changes

    def profile(self, s: TokenSnapshot) -> TokenProfile:
        """RugCheck + metadata for one token (cached). Never raises: missing data just stays missing."""
        hit = self._profiles.get(s.mint)
        if hit and self.clock() - hit.fetched_at < REPORT_TTL_S:
            return hit
        p = TokenProfile(s.mint, fetched_at=self.clock())
        report: dict[str, Any] = {}
        try:
            resp = self.http.get(RUGCHECK_REPORT.format(mint=s.mint))
            resp.raise_for_status()
            report = resp.json() if isinstance(resp.json(), dict) else {}
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("RugCheck report for %s unavailable: %s", s.mint, exc)
        if isinstance(report.get("totalHolders"), int):
            p.holders = report["totalHolders"]
        p.risks = [str(r.get("name") or "") for r in report.get("risks") or [] if isinstance(r, dict)]
        if s.mint not in self._meta:
            self._meta[s.mint] = self._metadata((report.get("tokenMeta") or {}).get("uri"))
        p.description, meta_links = self._meta[s.mint]
        p.links = list(dict.fromkeys([*s.links, *meta_links]))
        if self.cfg.enabled:
            p.kind, p.kind_reasons = classify(s.name, s.symbol, p.description, p.links, self.cfg.extra_tech_words)
        self._profiles[s.mint] = p
        if len(self._profiles) > 2000:
            for old in list(self._profiles)[:500]:
                self._profiles.pop(old, None)
        return p

    def _metadata(self, uri: str | None) -> tuple[str, list[str]]:
        if not uri or not uri.startswith(("https://", "ipfs://")):
            return "", []
        m = re.search(r"(?:ipfs://|/ipfs/)([A-Za-z0-9]{40,})", uri)
        url = IPFS_GATEWAY + m.group(1) if m else uri
        try:
            resp = self.http.get(url)
            resp.raise_for_status()
            meta = resp.json()
        except (httpx.HTTPError, ValueError):
            return "", []
        if not isinstance(meta, dict):
            return "", []
        links = [str(meta[k]) for k in ("website", "external_url", "twitter", "telegram") if meta.get(k)]
        return str(meta.get("description") or "")[:1000], links
