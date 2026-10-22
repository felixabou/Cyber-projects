"""Core data types shared across the agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TokenSnapshot:
    """Point-in-time market view of a token's most liquid pair."""

    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    quote_symbol: str
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    volume_m5: float
    volume_h1: float
    volume_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    price_change_m5: float
    price_change_h1: float
    price_change_h24: float
    pair_created_at: datetime | None
    url: str = ""
    fetched_at: datetime = field(default_factory=utcnow)

    def age_minutes(self, now: datetime) -> float | None:
        if self.pair_created_at is None:
            return None
        return (now - self.pair_created_at).total_seconds() / 60

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pair_created_at"] = self.pair_created_at.isoformat() if self.pair_created_at else None
        d["fetched_at"] = self.fetched_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TokenSnapshot":
        d = {k: v for k, v in d.items() if k in _SNAPSHOT_FIELDS}
        d["pair_created_at"] = datetime.fromisoformat(d["pair_created_at"]) if d.get("pair_created_at") else None
        d["fetched_at"] = datetime.fromisoformat(d["fetched_at"])
        return cls(**d)

    @property
    def txns_h1(self) -> int:
        return self.buys_h1 + self.sells_h1

    @property
    def buy_sell_ratio_h1(self) -> float:
        return self.buys_h1 / max(self.sells_h1, 1)

    @property
    def buy_sell_ratio_m5(self) -> float:
        return self.buys_m5 / max(self.sells_m5, 1)

    @property
    def volume_to_liquidity_h1(self) -> float:
        return self.volume_h1 / self.liquidity_usd if self.liquidity_usd > 0 else 0.0


_SNAPSHOT_FIELDS = {f.name for f in fields(TokenSnapshot)}


@dataclass
class SafetyReport:
    ok: bool
    score: float | None
    reasons: list[str]


@dataclass
class Fill:
    """Result of an executed (or simulated) swap."""

    token_amount: float
    usd_value: float
    price_usd: float
    fee_usd: float = 0.0
    signature: str | None = None


@dataclass
class Position:
    mint: str
    symbol: str
    pair_address: str
    opened_at: datetime
    entry_price_usd: float
    token_amount: float
    cost_basis_usd: float
    initial_cost_usd: float
    peak_price_usd: float
    entry_liquidity_usd: float
    last_price_usd: float
    last_liquidity_usd: float
    tp_levels_hit: list[int] = field(default_factory=list)
    proceeds_usd: float = 0.0
    entry_score: float = 0.0

    def pnl_pct(self, price: float | None = None) -> float:
        p = self.last_price_usd if price is None else price
        return (p / self.entry_price_usd - 1) * 100 if self.entry_price_usd > 0 else 0.0

    def market_value_usd(self, price: float | None = None) -> float:
        return self.token_amount * (self.last_price_usd if price is None else price)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        return cls(**d)


@dataclass
class TradeRecord:
    time: datetime
    side: str  # "buy" or "sell"
    mint: str
    symbol: str
    token_amount: float
    usd_value: float
    price_usd: float
    fee_usd: float
    reason: str
    realized_pnl_usd: float = 0.0
    signature: str | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["time"] = self.time.isoformat()
        return d


@dataclass
class ClosedTrade:
    """One completed round trip (entry through final exit, including partial sells)."""

    mint: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    cost_usd: float
    proceeds_usd: float
    exit_reason: str
    entry_score: float

    @property
    def pnl_usd(self) -> float:
        return self.proceeds_usd - self.cost_usd

    @property
    def pnl_pct(self) -> float:
        return self.pnl_usd / self.cost_usd * 100 if self.cost_usd else 0.0

    @property
    def hold_minutes(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 60
