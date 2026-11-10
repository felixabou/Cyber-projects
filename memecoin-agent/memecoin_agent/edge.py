"""Three things that decide how much to trust a candidate that already passed the filters and safety checks:

- MarketRegime: is the market itself falling? Measured on the coins of each scan (what share are up over the
  last hour) and on SOL's own price. In a falling market the automatic lanes stop opening new positions.
- ScanHistory: what did coins like this one do before? Read from the agent's own scan log (state/scans.csv):
  the share that rugged and the typical 1-hour return of earlier coins in the same lane, or of the same size,
  age and buy pressure. A group with a bad record is skipped; the record also scales the position size.
- conviction(): position size by how far the candidate's score is above the lane's minimum.

None of these replaces a safety check. They only ever block a buy or make it smaller or (within the lane's cap)
larger.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import LearningConfig, RegimeConfig
from .models import TokenSnapshot

log = logging.getLogger(__name__)


def conviction(score: float, min_score: float, full_score: float, floor: float) -> float:
    """Size factor in [floor, 1]: `floor` at the lane's minimum score, 1 at `full_score` and above."""
    if full_score <= min_score:
        return 1.0
    x = max(0.0, min(1.0, (score - min_score) / (full_score - min_score)))
    return round(floor + (1 - floor) * x, 3)


class MarketRegime:
    def __init__(self, cfg: RegimeConfig, sol_price: Callable[[], float] | None = None) -> None:
        self.cfg = cfg
        self.sol_price = sol_price
        self._breadth: deque[tuple[datetime, float, int]] = deque()  # (when, share of coins up over 1h, coins)
        self._sol: deque[tuple[datetime, float]] = deque()
        self._next_sol: datetime | None = None
        self.breadth: float | None = None
        self.coins = 0
        self.sol_change_h1: float | None = None

    def observe(self, now: datetime, snaps: list[TokenSnapshot]) -> None:
        c = self.cfg
        coins = [s for s in snaps if s.chain == "solana" and s.volume_h1 >= c.min_volume_h1_usd and s.price_usd > 0]
        if coins:
            up = sum(1 for s in coins if s.price_change_h1 > 0) / len(coins)
            self._breadth.append((now, up, len(coins)))
        cutoff = now - timedelta(minutes=c.window_minutes)
        while self._breadth and self._breadth[0][0] < cutoff:
            self._breadth.popleft()
        total = sum(n for _, _, n in self._breadth)
        self.coins = max((n for _, _, n in self._breadth), default=0)
        self.breadth = sum(b * n for _, b, n in self._breadth) / total if total else None
        if self.sol_price is not None and (self._next_sol is None or now >= self._next_sol):
            self._next_sol = now + timedelta(seconds=60)
            try:
                price = float(self.sol_price())
            except Exception:  # the regime is extra evidence; a failed price read must not stop a scan
                price = 0.0
            if price > 0:
                self._sol.append((now, price))
        hour_ago = now - timedelta(hours=1)
        while len(self._sol) > 1 and self._sol[1][0] <= hour_ago:
            self._sol.popleft()
        if len(self._sol) >= 2 and (now - self._sol[0][0]) >= timedelta(minutes=10):
            self.sol_change_h1 = (self._sol[-1][1] / self._sol[0][1] - 1) * 100
        else:
            self.sol_change_h1 = None

    def risk_off(self) -> str | None:
        """Why new automatic entries should wait (None: the market is fine, or there isn't enough to judge)."""
        c = self.cfg
        if not c.enabled:
            return None
        if c.max_sol_drop_h1_pct is not None and self.sol_change_h1 is not None \
                and self.sol_change_h1 <= -c.max_sol_drop_h1_pct:
            return f"market falling: SOL {self.sol_change_h1:+.1f}% over the last hour (limit -{c.max_sol_drop_h1_pct:g}%)"
        if c.min_breadth is not None and self.breadth is not None and self.coins >= c.min_coins \
                and self.breadth < c.min_breadth:
            return (f"market falling: only {self.breadth * 100:.0f}% of {self.coins} scanned coins are up over the "
                    f"last hour (need {c.min_breadth * 100:.0f}%)")
        return None

    def view(self) -> dict[str, Any]:
        return {"breadth": self.breadth, "coins": self.coins, "sol_change_h1": self.sol_change_h1,
                "risk_off": self.risk_off()}


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _band(value: float | None, edges: tuple[float, ...]) -> int:
    if value is None:
        return -1
    return sum(1 for e in edges if value >= e)


MCAP_EDGES = (25_000, 50_000, 100_000, 250_000, 1_000_000)
AGE_EDGES = (15, 30, 60, 180, 720)
BS_EDGES = (1.0, 1.3, 1.8)


class Verdict:
    def __init__(self, n: int, rug_rate: float, avg_1h: float, runners: float, group: str) -> None:
        self.n, self.rug_rate, self.avg_1h, self.runners, self.group = n, rug_rate, avg_1h, runners, group

    def text(self) -> str:
        return (f"{self.n} earlier coins ({self.group}): {self.rug_rate * 100:.0f}% rugged, "
                f"average 1h {self.avg_1h:+.0f}%, {self.runners * 100:.0f}% doubled")


class ScanHistory:
    """The scan log as a track record. Reloaded every reload_minutes; silent until a group has enough rows."""

    def __init__(self, cfg: LearningConfig, csv_path: str | Path, clock: Callable[[], datetime] | None = None) -> None:
        self.cfg, self.path = cfg, Path(csv_path)
        self._next: datetime | None = None
        self._lane: dict[str, list[dict]] = {}
        self._band: dict[tuple[int, int, int], list[dict]] = {}
        self.rows = 0

    def reload(self, now: datetime) -> None:
        if self._next is not None and now < self._next:
            return
        self._next = now + timedelta(minutes=self.cfg.reload_minutes)
        from .scanlog import load_rows

        rows = [r for r in load_rows(self.path) if r.get("chain", "solana") == "solana"][-self.cfg.max_rows:]
        self._lane, self._band = {}, {}
        for r in rows:
            if r.get("passed") == "1" and r.get("lane"):
                self._lane.setdefault(r["lane"], []).append(r)
            self._band.setdefault(self._key(_f(r.get("mcap0")), _f(r.get("age_min")), _f(r.get("bs_h1"))), []).append(r)
        if len(rows) != self.rows:
            log.info("Track record: %d finished scan-log rows loaded", len(rows))
        self.rows = len(rows)

    @staticmethod
    def _key(mcap: float | None, age: float | None, bs: float | None) -> tuple[int, int, int]:
        return _band(mcap, MCAP_EDGES), _band(age, AGE_EDGES), _band(bs, BS_EDGES)

    @staticmethod
    def _stats(rows: list[dict], group: str) -> Verdict:
        r1 = [max(min(v, 300.0), -100.0) for v in (_f(r.get("ret_1h")) for r in rows) if v is not None]
        mx = [v for v in (_f(r.get("max_ret")) for r in rows) if v is not None]
        return Verdict(len(rows), sum(1 for r in rows if r.get("rugged") == "1") / len(rows),
                       statistics.fmean(r1) if r1 else 0.0, sum(1 for v in mx if v >= 100) / len(mx) if mx else 0.0, group)

    def verdict(self, snap: TokenSnapshot, lane: str, now: datetime) -> Verdict | None:
        """The record of earlier coins like this one; None until there are enough of them."""
        c = self.cfg
        if not c.enabled or snap.chain != "solana":
            return None
        self.reload(now)
        rows = self._lane.get(lane, [])
        if len(rows) >= c.min_lane_samples:
            return self._stats(rows, f"passed the {lane} lane")
        rows = self._band.get(self._key(snap.market_cap_usd, snap.age_minutes(now), snap.buy_sell_ratio_h1), [])
        if len(rows) >= c.min_band_samples:
            return self._stats(rows, "same size, age and buy pressure")
        return None

    def judge(self, snap: TokenSnapshot, lane: str, now: datetime) -> tuple[str | None, float, Verdict | None]:
        """(why not to buy or None, size factor, the record)."""
        c, v = self.cfg, self.verdict(snap, lane, now)
        if v is None:
            return None, 1.0, None
        if c.max_rug_rate is not None and v.rug_rate > c.max_rug_rate:
            return f"skipped: track record: {v.text()} (limit {c.max_rug_rate * 100:.0f}% rugged)", 1.0, v
        if c.min_avg_ret_1h is not None and v.avg_1h < c.min_avg_ret_1h:
            return f"skipped: track record: {v.text()} (limit {c.min_avg_ret_1h:+g}% average)", 1.0, v
        # A group that returned +scale_ret_1h% an hour on average gets the full boost; a losing one, the full cut.
        x = max(-1.0, min(1.0, v.avg_1h / c.scale_ret_1h)) if c.scale_ret_1h > 0 else 0.0
        factor = 1 + x * ((c.max_size_factor - 1) if x > 0 else (1 - c.min_size_factor))
        return None, round(factor, 3), v
