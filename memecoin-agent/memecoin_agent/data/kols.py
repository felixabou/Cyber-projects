"""Which known traders (GMGN's KOL feed) bought a coin, and how many of them already sold.

GMGN's /v1/user/kol feed lists the latest trades by the wallets it tags as KOLs, across all coins. Polling it every
kols.poll_every_s and remembering each wallet's first buy and any later sell per coin gives, for any coin: how many
KOLs bought, who, how long ago, and how many already sold. Only trades seen while the agent runs are counted, so a
coin's record starts when the agent started watching. Needs a GMGN API key.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import KolConfig
from .dexscreener import DEX_TO_GMGN

log = logging.getLogger(__name__)


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trader_name(row: dict[str, Any]) -> str:
    info = row.get("maker_info") if isinstance(row.get("maker_info"), dict) else {}
    for d in (info, row):
        for k in ("twitter_name", "name", "twitter_username", "ens"):
            if d.get(k):
                return str(d[k])[:32]
    maker = str(row.get("maker") or "")
    return f"{maker[:4]}…{maker[-4:]}" if len(maker) > 8 else maker


@dataclass
class KolStats:
    bought: int
    sold: int
    names: list[tuple[str, float]] = field(default_factory=list)  # (name, minutes since its first buy), newest first

    @property
    def sold_share(self) -> float:
        return self.sold / self.bought if self.bought else 0.0


class KolTracker:
    def __init__(self, cfg: KolConfig, gmgn: Any, clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.gmgn, self.clock = cfg, gmgn, clock
        self._next = 0.0
        # (chain, token) -> maker -> {"name", "bought" (unix s), "sold" (unix s or None)}
        self.coins: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._warned = False

    def poll(self) -> None:
        if not self.cfg.enabled or self.gmgn is None or self.clock() < self._next:
            return
        self._next = self.clock() + self.cfg.poll_every_s
        for chain in self.cfg.chains:
            try:
                rows = self.gmgn.trades_feed("kol", chain, 100)
            except Exception as exc:  # the feed is extra evidence; never let it stop a tick
                if not self._warned:
                    self._warned = True
                    log.warning("KOL feed unavailable (%s): %s", chain, exc)
                continue
            for r in rows:
                self._add(chain, r)
        self._prune()

    def _add(self, chain: str, r: dict[str, Any]) -> None:
        token, maker = str(r.get("base_address") or ""), str(r.get("maker") or "")
        side = str(r.get("side") or "").lower()
        if not token or not maker or side not in ("buy", "sell"):
            return
        ts = _f(r.get("timestamp")) or self.clock()
        key = (chain, token.lower() if token.startswith("0x") else token)
        rec = self.coins.setdefault(key, {}).setdefault(maker, {"name": trader_name(r), "bought": None, "sold": None})
        if side == "buy":
            rec["bought"] = ts if rec["bought"] is None else min(rec["bought"], ts)
        elif rec["bought"] is not None and ts >= rec["bought"]:
            rec["sold"] = ts if rec["sold"] is None else min(rec["sold"], ts)

    def _prune(self) -> None:
        cut = self.clock() - self.cfg.max_age_hours * 3600
        for key in list(self.coins):
            makers = {m: r for m, r in self.coins[key].items() if (r["bought"] or 0) >= cut}
            if makers:
                self.coins[key] = makers
            else:
                del self.coins[key]

    def stats(self, chain: str, token: str) -> KolStats | None:
        """None when GMGN doesn't cover the chain or no KOL buy of the coin has been seen."""
        g = DEX_TO_GMGN.get(chain, chain)
        makers = self.coins.get((g, token.lower() if token.startswith("0x") else token))
        if not makers:
            return None
        bought = [r for r in makers.values() if r["bought"] is not None]
        now = self.clock()
        names = sorted(((r["name"], (now - r["bought"]) / 60) for r in bought), key=lambda x: x[1])
        return KolStats(len(bought), sum(1 for r in bought if r["sold"] is not None), names)

    def skip_reason(self, chain: str, token: str) -> str | None:
        """The 'most KOLs already sold' rule (kols.skip_sold_share; null = off)."""
        c = self.cfg
        s = self.stats(chain, token) if c.skip_sold_share is not None else None
        if s is None or s.bought < c.skip_min_kols or s.sold_share < c.skip_sold_share:
            return None
        return f"skipped: {s.sold} of {s.bought} KOLs who bought already sold"

