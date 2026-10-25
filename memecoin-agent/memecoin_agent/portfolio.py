"""Portfolio ledger with JSON persistence and a CSV trade journal."""

from __future__ import annotations

import csv
import json
import os
from collections import deque
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from .models import ClosedTrade, Fill, Position, TokenSnapshot, TradeRecord

TRADE_COLUMNS = [f.name for f in fields(TradeRecord)]


def read_trades_csv(csv_path: str | Path, limit: int | None = None) -> list[TradeRecord]:
    """The trade journal (the last `limit` rows if given); unreadable rows are skipped."""
    path = Path(csv_path)
    if not path.exists():
        return []
    rows: deque[dict[str, str]] = deque(maxlen=limit)
    try:
        with path.open(newline="") as fh:
            rows.extend(csv.DictReader(fh))
    except OSError:
        return []
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["time"])
            out.append(TradeRecord(
                time=t if t.tzinfo else t.replace(tzinfo=timezone.utc), side=r["side"], mint=r["mint"],
                symbol=r["symbol"], token_amount=float(r["token_amount"]), usd_value=float(r["usd_value"]),
                price_usd=float(r["price_usd"]), fee_usd=float(r["fee_usd"]), reason=r["reason"],
                realized_pnl_usd=float(r.get("realized_pnl_usd") or 0), signature=r.get("signature") or None,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def write_trades_csv(csv_path: str | Path, trades: list[TradeRecord]) -> None:
    """Write a complete trade CSV atomically (readers never see a half-written file)."""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for t in trades:
            writer.writerow(t.to_row())
    os.replace(tmp, path)


@dataclass
class Portfolio:
    cash_usd: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl_by_day: dict[str, float] = field(default_factory=dict)
    day_start_equity: dict[str, float] = field(default_factory=dict)
    cooldowns: dict[str, str] = field(default_factory=dict)  # mint -> ISO time the cooldown ends
    entry_times: list[str] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)  # unsaved, flushed to CSV
    closed: list[ClosedTrade] = field(default_factory=list)  # in-memory round-trip log (backtest stats)

    def active_positions(self) -> dict[str, Position]:
        """Positions that count toward limits (dust doesn't)."""
        return {m: p for m, p in self.positions.items() if not p.dust}

    # -- valuation ----------------------------------------------------------------
    def positions_value_usd(self) -> float:
        return sum(p.market_value_usd() for p in self.positions.values())

    def equity_usd(self) -> float:
        return self.cash_usd + self.positions_value_usd()

    def total_realized_pnl(self) -> float:
        return sum(self.realized_pnl_by_day.values())

    # -- bookkeeping ----------------------------------------------------------------
    def mark(self, snap: TokenSnapshot) -> None:
        pos = self.positions.get(snap.mint)
        if pos is None:
            return
        pos.last_price_usd = snap.price_usd
        pos.last_liquidity_usd = snap.liquidity_usd
        pos.peak_price_usd = max(pos.peak_price_usd, snap.price_usd)

    def open(self, snap: TokenSnapshot, fill: Fill, now: datetime, score: float, reason: str | None = None) -> Position:
        if snap.mint in self.positions:
            raise ValueError(f"Already holding {snap.symbol}")
        pos = Position(
            mint=snap.mint,
            symbol=snap.symbol,
            pair_address=snap.pair_address,
            opened_at=now,
            entry_price_usd=fill.price_usd,
            token_amount=fill.token_amount,
            cost_basis_usd=fill.usd_value,
            initial_cost_usd=fill.usd_value,
            peak_price_usd=max(fill.price_usd, snap.price_usd),
            entry_liquidity_usd=snap.liquidity_usd,
            last_price_usd=snap.price_usd,
            last_liquidity_usd=snap.liquidity_usd,
            entry_score=score,
        )
        self.positions[snap.mint] = pos
        self.cash_usd -= fill.usd_value
        self.entry_times.append(now.isoformat())
        self.trades.append(
            TradeRecord(now, "buy", snap.mint, snap.symbol, fill.token_amount, fill.usd_value,
                        fill.price_usd, fill.fee_usd, reason or f"entry score {score:.2f}", 0.0, fill.signature)
        )
        return pos

    def reduce(self, mint: str, fraction: float, fill: Fill, now: datetime, reason: str,
               cooldown_until: datetime | None = None) -> float:
        """Apply a sell fill. Returns the realized PnL of this sale."""
        pos = self.positions[mint]
        fraction = min(1.0, max(0.0, fraction))
        closing = fraction >= 0.999 or fill.token_amount >= pos.token_amount * 0.999
        sold_cost = pos.cost_basis_usd if closing else pos.cost_basis_usd * fraction
        realized = fill.usd_value - sold_cost
        pos.cost_basis_usd -= sold_cost
        pos.token_amount = 0.0 if closing else max(0.0, pos.token_amount - fill.token_amount)
        pos.proceeds_usd += fill.usd_value
        self.cash_usd += fill.usd_value
        day = now.date().isoformat()
        self.realized_pnl_by_day[day] = self.realized_pnl_by_day.get(day, 0.0) + realized
        self.trades.append(
            TradeRecord(now, "sell", mint, pos.symbol, fill.token_amount, fill.usd_value,
                        fill.price_usd, fill.fee_usd, reason, realized, fill.signature)
        )
        if closing:
            del self.positions[mint]
            self.closed.append(ClosedTrade(mint, pos.symbol, pos.opened_at, now, pos.initial_cost_usd,
                                           pos.proceeds_usd, reason, pos.entry_score))
            if cooldown_until is not None:
                self.cooldowns[mint] = cooldown_until.isoformat()
        return realized

    # -- persistence ----------------------------------------------------------------
    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash_usd": self.cash_usd,
            "positions": {m: p.to_dict() for m, p in self.positions.items()},
            "realized_pnl_by_day": self.realized_pnl_by_day,
            "day_start_equity": self.day_start_equity,
            "cooldowns": self.cooldowns,
            "entry_times": self.entry_times[-500:],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    def flush_trades(self, csv_path: str | Path) -> None:
        if not self.trades:
            return
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not csv_path.exists()
        with csv_path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
            if new_file:
                writer.writeheader()
            for t in self.trades:
                writer.writerow(t.to_row())
        self.trades.clear()

    @classmethod
    def load(cls, path: str | Path, starting_cash: float) -> "Portfolio":
        path = Path(path)
        if not path.exists():
            return cls(cash_usd=starting_cash)
        data = json.loads(path.read_text())
        return cls(
            cash_usd=float(data["cash_usd"]),
            positions={m: Position.from_dict(p) for m, p in data.get("positions", {}).items()},
            realized_pnl_by_day=data.get("realized_pnl_by_day", {}),
            day_start_equity=data.get("day_start_equity", {}),
            cooldowns=data.get("cooldowns", {}),
            entry_times=data.get("entry_times", []),
        )
