"""Everything a Discord call shows beyond the price data: holder risk, momentum, known traders and LarpCheck.

Each source is optional and read once per call (calls are rare, so the extra requests are cheap):

- GMGN token security and info (needs a GMGN key): top-10 share, dev, insiders, bundlers, snipers, fresh wallets,
  known traders and smart money holding, the creator's past launches;
- Jupiter token data (Solana, free): holders, top holders, organic score, where GMGN has nothing;
- RugCheck summary (Solana, free): the share of the pool that is locked or burned;
- GeckoTerminal 5-minute candles (free): the last 15 minutes' move, and volume against the 15 minutes before;
- data.kols.KolTracker: which known traders bought and how many already sold;
- LarpCheck: only a check that already exists (lookups spend no credits).

A source that fails or isn't configured leaves its fields empty; the call goes out regardless.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .models import TokenSnapshot

log = logging.getLogger(__name__)

GMGN_CHAIN = {"solana": "sol", "ethereum": "eth", "bsc": "bsc", "base": "base", "arbitrum": "arb",
              "robinhood": "robinhood", "monad": "monad", "tron": "tron", "hyperevm": "hyperevm"}
GECKO_NETWORK = {"solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base", "arbitrum": "arbitrum",
                 "polygon": "polygon_pos", "avalanche": "avax", "tron": "tron"}
BUBBLEMAPS_CHAIN = {"solana": "sol", "ethereum": "eth", "bsc": "bsc", "base": "base", "arbitrum": "arbi",
                    "polygon": "poly", "avalanche": "avax", "tron": "tron", "sonic": "sonic", "fantom": "ftm",
                    "cronos": "cro", "linea": "linea", "apechain": "apechain"}


def bubblemaps_url(chain: str, address: str) -> str | None:
    c = BUBBLEMAPS_CHAIN.get(chain)
    return f"https://app.bubblemaps.io/{c}/token/{address}" if c else None


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v: Any) -> float | None:
    """A GMGN rate (0-1) as a percentage."""
    x = _f(v)
    return None if x is None else x * 100


@dataclass
class TokenFacts:
    holders: float | None = None
    top10_pct: float | None = None
    dev_pct: float | None = None
    insiders_pct: float | None = None
    bundlers_pct: float | None = None
    snipers: float | None = None
    fresh_pct: float | None = None
    lp_locked_pct: float | None = None
    kol_holders: float | None = None
    smart_holders: float | None = None
    creator_launches: float | None = None
    organic_score: float | None = None
    m15_pct: float | None = None
    vol15: float | None = None
    vol15_prev: float | None = None
    kols: Any = None  # data.kols.KolStats
    larp: dict[str, Any] | None = None  # verdict, score, coin_stamp, summary, url
    xray: Any = None  # data.memeos.Xray
    bundle: Any = None  # data.memeos.Bundle
    sources: list[str] = field(default_factory=list)


class FactGatherer:
    def __init__(self, gmgn: Any = None, kols: Any = None, larpcheck: Any = None, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, memeos: Any = None) -> None:
        self.gmgn, self.kols, self.larpcheck, self.sleep, self.memeos = gmgn, kols, larpcheck, sleep, memeos
        self.http = http or httpx.Client(timeout=10.0, headers={"Accept": "application/json"})

    def gather(self, snap: TokenSnapshot) -> TokenFacts:
        f = TokenFacts()
        for name, step in (("GMGN", self._gmgn), ("Jupiter", self._jupiter), ("RugCheck", self._rugcheck),
                           ("GeckoTerminal", self._momentum), ("KOL feed", self._kols), ("LarpCheck", self._larp),
                           ("MEME OS", self._memeos)):
            try:
                if step(snap, f):
                    f.sources.append(name)
            except Exception as exc:  # every source is extra; a failure only leaves its fields empty
                log.debug("%s facts for %s failed: %s", name, snap.symbol, exc)
        return f

    def _get(self, url: str, **params: Any) -> Any:
        resp = self.http.get(url, params=params or None)
        if resp.status_code == 429:  # free APIs: one short wait, then give up on this source
            self.sleep(2.0)
            resp = self.http.get(url, params=params or None)
        resp.raise_for_status()
        return resp.json()

    def _gmgn(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        chain = GMGN_CHAIN.get(s.chain)
        if self.gmgn is None or not chain:
            return False
        sec, info = self.gmgn.security(s.mint, chain=chain) or {}, self.gmgn.info(s.mint, chain=chain) or {}
        stat, tags, dev = info.get("stat") or {}, info.get("wallet_tags_stat") or {}, info.get("dev") or {}
        f.top10_pct = _pct(sec.get("top_10_holder_rate", stat.get("top_10_holder_rate")))
        f.dev_pct = _pct(sec.get("dev_team_hold_rate", stat.get("dev_team_hold_rate", sec.get("creator_balance_rate"))))
        f.insiders_pct = _pct(sec.get("suspected_insider_hold_rate"))
        f.bundlers_pct = _pct(sec.get("bundler_trader_amount_rate", stat.get("top_bundler_trader_percentage")))
        f.fresh_pct = _pct(stat.get("fresh_wallet_rate"))
        f.snipers = _f(tags.get("sniper_wallets"))
        f.kol_holders = _f(tags.get("renowned_wallets"))
        f.smart_holders = _f(tags.get("smart_wallets"))
        f.creator_launches = _f(dev.get("creator_open_count"))
        f.holders = _f(info.get("holder_count") or stat.get("holder_count"))
        return bool(sec or info)

    def _jupiter(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        if s.chain != "solana":
            return False
        rows = self._get("https://lite-api.jup.ag/tokens/v2/search", query=s.mint)
        t = next((r for r in rows or [] if isinstance(r, dict) and r.get("id") == s.mint), None)
        if not t:
            return False
        audit = t.get("audit") or {}
        f.holders = f.holders if f.holders is not None else _f(t.get("holderCount"))
        f.top10_pct = f.top10_pct if f.top10_pct is not None else _f(audit.get("topHoldersPercentage"))
        f.dev_pct = f.dev_pct if f.dev_pct is not None else _f(audit.get("devBalancePercentage"))
        f.organic_score = _f(t.get("organicScore"))
        return True

    def _rugcheck(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        if s.chain != "solana":
            return False
        d = self._get(f"https://api.rugcheck.xyz/v1/tokens/{s.mint}/report/summary")
        f.lp_locked_pct = _f((d or {}).get("lpLockedPct"))
        return f.lp_locked_pct is not None

    def _momentum(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        net = GECKO_NETWORK.get(s.chain)
        if not net or not s.pair_address:
            return False
        d = self._get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{s.pair_address}/ohlcv/minute",
                      aggregate=5, limit=6, currency="usd")
        c = sorted(((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or [], key=lambda k: k[0])
        if len(c) < 3:
            return False
        last, prev = c[-3:], c[-6:-3]
        if last[0][1]:
            f.m15_pct = (last[-1][4] / last[0][1] - 1) * 100
        f.vol15 = sum(k[5] for k in last)
        f.vol15_prev = sum(k[5] for k in prev) if len(prev) == 3 else None
        return True

    def _kols(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        if self.kols is None:
            return False
        f.kols = self.kols.stats(s.chain, s.mint)
        return f.kols is not None

    def _larp(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        gate = self.larpcheck
        if gate is None:
            return False
        from .data.larpcheck import Verdict

        v = getattr(gate, "last_verdicts", {}).get(s.mint)
        if v is None:
            data = gate.client.lookup_coin(s.mint)
            v = Verdict.from_latest((data.get("latest") or {}) if isinstance(data, dict) else {})
        if v is None:
            return False
        f.larp = {**v.as_dict(), "url": v.url or f"https://larpcheck.app/{s.mint}"}
        return True


    def _memeos(self, s: TokenSnapshot, f: TokenFacts) -> bool:
        if self.memeos is None:
            return False
        f.xray = self.memeos.reports.get(s.mint) or self.memeos.xray(s.mint)  # usually read before the buy already
        f.bundle = self.memeos.bundle(s.mint)
        return f.xray is not None or f.bundle is not None


def judge(s: TokenSnapshot, f: TokenFacts, age_min: float | None, unchecked: bool = False) -> tuple[list[str], list[str]]:
    """(best points, worst points) for a call, from whatever facts were found."""
    best, worst = [], []

    def add(lst: list[str], cond: bool, text: str) -> None:
        if cond:
            lst.append(text)

    # most telling first: the lists are cut at five points
    v = (f.larp or {}).get("verdict")
    add(best, v in ("REAL", "LIKELY REAL"), f"LarpCheck {v}")
    add(worst, v in ("LARP", "LIKELY LARP", "SCAM"), f"LarpCheck {v}")
    add(worst, unchecked, "no safety check on this chain")
    x = f.xray
    add(best, x is not None and x.score is not None and x.score >= 60 and x.verdict not in ("HIGH RISK", "SCAM", "RUG"),
        f"MEME OS X-Ray {x.verdict if x else ''} {x.score if x and x.score is not None else 0:g}")
    add(worst, x is not None and (x.verdict in ("HIGH RISK", "SCAM", "RUG", "DANGER", "CRITICAL")
                                  or (x.score is not None and x.score < 40)),
        f"MEME OS X-Ray {x.verdict if x else ''}" + (f" {x.score:g}" if x and x.score is not None else ""))
    b = f.bundle
    add(worst, b is not None and (b.linked_supply_pct or 0) >= 30, f"linked wallets hold {b.linked_supply_pct if b else 0:.0f}%")
    add(best, b is not None and b.linked_supply_pct is not None and b.linked_supply_pct <= 10, "little linked supply")
    k = f.kols
    add(best, k is not None and k.bought >= 3 and k.sold_share < 0.5, f"{k.bought - k.sold if k else 0} KOLs still holding")
    add(worst, k is not None and k.bought >= 3 and k.sold_share >= 0.75, f"{k.sold if k else 0} of {k.bought if k else 0} KOLs already sold")
    add(best, f.lp_locked_pct is not None and f.lp_locked_pct >= 95, f"LP locked/burned {f.lp_locked_pct or 0:.0f}%")
    add(worst, f.lp_locked_pct is not None and f.lp_locked_pct < 50, f"only {f.lp_locked_pct or 0:.0f}% of LP locked")
    add(best, f.top10_pct is not None and f.top10_pct <= 20, f"top 10 only {f.top10_pct or 0:.0f}%")
    add(worst, f.top10_pct is not None and f.top10_pct > 30, f"top 10 hold {f.top10_pct or 0:.0f}%")
    add(best, f.dev_pct is not None and f.dev_pct <= 1, f"dev holds {f.dev_pct or 0:.1f}%")
    add(worst, f.dev_pct is not None and f.dev_pct > 5, f"dev holds {f.dev_pct or 0:.1f}%")
    add(best, f.bundlers_pct is not None and f.bundlers_pct <= 5, "few bundlers")
    add(worst, f.bundlers_pct is not None and f.bundlers_pct > 15, f"bundlers {f.bundlers_pct or 0:.0f}%")
    add(worst, f.insiders_pct is not None and f.insiders_pct > 10, f"insiders hold {f.insiders_pct or 0:.0f}%")
    add(worst, f.fresh_pct is not None and f.fresh_pct > 25, f"fresh wallets {f.fresh_pct or 0:.0f}%")
    add(best, f.holders is not None and f.holders >= 500, f"{f.holders or 0:,.0f} holders")
    add(best, f.organic_score is not None and f.organic_score >= 60, f"organic score {f.organic_score or 0:.0f}/100")
    add(worst, age_min is not None and age_min < 10, f"only {age_min or 0:.0f} min old")
    txns = s.buys_h1 + s.sells_h1
    add(worst, txns >= 50 and s.sells_h1 / txns > 0.55, f"heavy selling ({s.sells_h1 / txns * 100 if txns else 0:.0f}% sells)")
    add(best, bool(s.links), "has socials")
    add(worst, not s.links, "no socials")
    return best[:5], worst[:5]
