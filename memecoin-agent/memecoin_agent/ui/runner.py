"""Runs the agent on a background thread and publishes read-only views for the dashboard.

The agent thread is the only one that touches the agent and portfolio. HTTP
handlers read `runner.view`, a plain dict rebuilt after every tick and
command and swapped in atomically, so a slow tick (network calls) never
blocks the UI. Controls are queued to the agent thread and run between
ticks.
"""

from __future__ import annotations

import copy
import csv
import logging
import queue
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..agent import Agent
from ..models import TradeRecord

log = logging.getLogger(__name__)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


class LogBuffer(logging.Handler):
    """Ring buffer of recent log records, fetched incrementally by id."""

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__(level=logging.INFO)
        self.entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
        except Exception:
            msg = str(record.msg)
        with self._lock:
            self.seq += 1
            self.entries.append({
                "id": self.seq,
                "t": datetime.fromtimestamp(record.created).astimezone().isoformat(),
                "level": record.levelname,
                "source": record.name.rsplit(".", 1)[-1],
                "msg": msg,
            })

    def since(self, after: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.entries if e["id"] > after][-limit:]


class EquityHistory:
    """Equity samples for the chart; persisted at most once a minute so restarts keep the curve."""

    def __init__(self, path: str | Path | None = None, maxlen: int = 50_000) -> None:
        self.path = Path(path) if path else None
        self.points: deque[tuple[datetime, float]] = deque(maxlen=maxlen)
        self._last_persisted: datetime | None = None
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                with self.path.open() as fh:
                    for row in csv.reader(fh):
                        if len(row) == 2 and row[0] != "time":
                            self.points.append((datetime.fromisoformat(row[0]), float(row[1])))
            except (OSError, ValueError) as exc:
                log.warning("Ignoring unreadable equity history %s: %s", self.path, exc)
            if self.points:
                self._last_persisted = self.points[-1][0]

    def add(self, t: datetime, equity: float) -> None:
        with self._lock:
            if self.points and t <= self.points[-1][0]:
                return
            persist = self._last_persisted is None or t - self._last_persisted >= timedelta(minutes=1)
            if not persist and self.points and t - self.points[-1][0] < timedelta(seconds=10):
                return  # in-memory resolution: one point per 10s is plenty for a chart
            self.points.append((t, equity))
        if persist and self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            with self.path.open("a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["time", "equity"])
                w.writerow([t.isoformat(), f"{equity:.6f}"])
            self._last_persisted = t

    def series(self, since: datetime | None = None, max_points: int = 600) -> list[list[Any]]:
        with self._lock:
            pts = [p for p in self.points if since is None or p[0] >= since]
        return [[t.isoformat(), round(v, 4)] for t, v in downsample(pts, max_points)]


def downsample(points: list, max_points: int) -> list:
    if len(points) <= max_points:
        return list(points)
    step = len(points) / max_points
    out = [points[int(i * step)] for i in range(max_points - 1)]
    out.append(points[-1])  # always keep the latest value
    return out


def trade_view(t: TradeRecord) -> dict[str, Any]:
    return {
        "time": _iso(t.time), "side": t.side, "mint": t.mint, "symbol": t.symbol,
        "token_amount": t.token_amount, "usd_value": t.usd_value, "price_usd": t.price_usd,
        "fee_usd": t.fee_usd, "reason": t.reason, "realized_pnl_usd": t.realized_pnl_usd,
        "signature": t.signature,
    }


def load_recent_trades(csv_path: str | Path, limit: int = 500) -> list[TradeRecord]:
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
            out.append(TradeRecord(
                time=datetime.fromisoformat(r["time"]), side=r["side"], mint=r["mint"], symbol=r["symbol"],
                token_amount=float(r["token_amount"]), usd_value=float(r["usd_value"]),
                price_usd=float(r["price_usd"]), fee_usd=float(r["fee_usd"]), reason=r["reason"],
                realized_pnl_usd=float(r.get("realized_pnl_usd") or 0), signature=r.get("signature") or None,
            ))
        except (KeyError, ValueError):
            continue
    return out


class BacktestJob:
    """At most one dashboard-launched backtest at a time, on its own thread and config copy."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"status": "idle"}
        self._thread: threading.Thread | None = None

    def start(self, cfg: Any, directory: str, overrides: list[str], assume_safe: bool) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("A backtest is already running")
        from ..config import apply_override

        bt_cfg = copy.deepcopy(cfg)  # run_backtest mutates its config; never touch the live agent's
        for o in overrides:
            apply_override(bt_cfg, o)  # raises ValueError on a bad override, before the thread starts
        self.state = {"status": "running", "started_at": _iso(datetime.now().astimezone()),
                      "dir": directory, "overrides": overrides}
        self._thread = threading.Thread(target=self._run, args=(bt_cfg, directory, assume_safe),
                                        name="backtest", daemon=True)
        self._thread.start()
        return self.state

    def _run(self, cfg: Any, directory: str, assume_safe: bool) -> None:
        from ..backtest import format_report, run_backtest
        from ..recording import read_recordings

        started = time.monotonic()
        try:
            res = run_backtest(cfg, read_recordings(directory), assume_safe=assume_safe)
            stats = res.stats()
            if stats["profit_factor"] == float("inf"):
                stats["profit_factor"] = None  # not JSON-serialisable
            self.state = {
                **self.state, "status": "done", "elapsed_s": round(time.monotonic() - started, 1),
                "start": _iso(res.start), "end": _iso(res.end), "records": res.records_read,
                "starting_equity": res.starting_equity, "final_equity": res.final_equity,
                "stats": stats, "report": format_report(res),
                "equity": [[_iso(t), round(v, 4)] for t, v in downsample(res.equity_curve, 600)],
                "closed": [
                    {"symbol": c.symbol, "mint": c.mint, "opened_at": _iso(c.opened_at), "closed_at": _iso(c.closed_at),
                     "cost_usd": c.cost_usd, "proceeds_usd": c.proceeds_usd, "pnl_usd": c.pnl_usd,
                     "pnl_pct": c.pnl_pct, "hold_minutes": c.hold_minutes, "exit_reason": c.exit_reason}
                    for c in res.closed[-500:]
                ],
            }
        except Exception as exc:
            log.warning("Dashboard backtest failed: %s", exc)
            self.state = {**self.state, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                          "detail": traceback.format_exc(limit=3)}


class AgentRunner:
    def __init__(
        self,
        agent: Agent,
        feed: Any = None,
        equity: EquityHistory | None = None,
        advance: Callable[[float], None] | None = None,
        speed: float = 1.0,
        synthetic: bool = False,
    ) -> None:
        self.agent = agent
        self.feed = feed
        self.equity = equity or EquityHistory()
        self.advance = advance  # synthetic mode: move simulated time forward each tick
        self.speed = max(speed, 0.01)
        self.synthetic = synthetic
        self.backtest = BacktestJob()
        self.started_at = agent.clock()
        self._commands: queue.Queue[tuple[str, dict[str, Any], Future]] = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._force_tick = False
        agent.scan_when_blocked = True
        if not agent.recent_trades:
            agent.recent_trades.extend(load_recent_trades(agent.cfg.agent.trades_csv))
        self.view: dict[str, Any] = {}
        self._publish()

    # -- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="agent", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        a = self.agent
        log.info("Agent started in %s mode. Equity $%.2f, %d open position(s).",
                 a.cfg.execution.mode.upper(), a.pf.equity_usd(), len(a.pf.positions))
        next_tick = 0.0
        while not self._stop.is_set():
            self._drain_commands()
            if self._force_tick or time.monotonic() >= next_tick:
                self._force_tick = False
                if self.advance:
                    self.advance(a.cfg.agent.tick_seconds)
                try:
                    a.tick()
                except Exception:
                    a.tick_errors += 1
                    log.exception("Tick failed")
                self.equity.add(a.clock(), a.pf.equity_usd())
                self._publish()
                next_tick = time.monotonic() + a.cfg.agent.tick_seconds / self.speed
            self._wake.wait(timeout=max(0.0, next_tick - time.monotonic()))
            self._wake.clear()
        self._drain_commands()
        a.save()
        log.info("Agent stopped; state saved.")

    # -- controls -------------------------------------------------------------------
    def submit(self, action: str, timeout: float = 120.0, **kwargs: Any) -> Any:
        fut: Future = Future()
        self._commands.put((action, kwargs, fut))
        self._wake.set()
        return fut.result(timeout=timeout)

    def _drain_commands(self) -> None:
        while True:
            try:
                action, kwargs, fut = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                fut.set_result(self._execute(action, **kwargs))
            except Exception as exc:
                fut.set_exception(exc)
            self._publish()

    def _execute(self, action: str, **kw: Any) -> dict[str, Any]:
        a = self.agent
        if action == "pause":
            a.entries_paused = True
            log.info("Entries paused from the dashboard")
        elif action == "resume":
            a.entries_paused = False
            log.info("Entries resumed from the dashboard")
        elif action == "close":
            mint = str(kw.get("mint") or "")
            if mint not in a.pf.positions:
                raise ValueError("No open position for that token")
            symbol = a.pf.positions[mint].symbol
            if not a.close_position(mint, "manual close (dashboard)"):
                raise RuntimeError(f"Sell of {symbol} failed; see the activity log")
            return {"ok": True, "message": f"Closed {symbol}"}
        elif action == "close_all":
            n = len(a.pf.positions)
            a.close_all("manual close-all (dashboard)")
            left = len(a.pf.positions)
            if left:
                raise RuntimeError(f"{left} of {n} position(s) could not be sold; see the activity log")
            return {"ok": True, "message": f"Closed {n} position(s)"}
        elif action == "scan_now":
            a.scan_and_enter(a.clock())
            a.save()
            return {"ok": True, "message": f"Scanned {len(a.last_scan)} candidate(s)"}
        else:
            raise ValueError(f"Unknown action {action!r}")
        return {"ok": True}

    # -- view -----------------------------------------------------------------------
    def _publish(self) -> None:
        try:
            self.view = self._build_view()
        except Exception:
            log.exception("Could not build dashboard view")

    def _build_view(self) -> dict[str, Any]:
        a, cfg, pf = self.agent, self.agent.cfg, self.agent.pf
        now = a.clock()
        day = now.date().isoformat()
        equity = pf.equity_usd()
        day_start = pf.day_start_equity.get(day, equity)
        realized_today = pf.realized_pnl_by_day.get(day, 0.0)
        unrealized_losses = sum(min(0.0, p.market_value_usd() - p.cost_basis_usd) for p in pf.positions.values())
        loss_limit = day_start * cfg.risk.max_daily_loss_pct / 100
        loss_now = max(0.0, -(realized_today + unrealized_losses))
        hour_ago = now - timedelta(hours=1)
        entries_last_hour = sum(1 for t in pf.entry_times if datetime.fromisoformat(t) >= hour_ago)
        live = cfg.execution.mode == "live"
        baseline = (self.equity.points[0][1] if self.equity.points else equity) if live else cfg.risk.starting_balance_usd
        e = cfg.exits

        positions = []
        for p in pf.positions.values():
            peak_gain = (p.peak_price_usd / p.entry_price_usd - 1) * 100 if p.entry_price_usd else 0.0
            stop_price = p.entry_price_usd * (1 - e.stop_loss_pct / 100)
            trail_armed = peak_gain >= e.trailing_activation_pct
            trail_price = p.peak_price_usd * (1 - e.trailing_stop_pct / 100) if trail_armed else None
            value = p.market_value_usd()
            positions.append({
                "mint": p.mint, "symbol": p.symbol,
                "url": f"https://dexscreener.com/solana/{p.pair_address}" if not self.synthetic else None,
                "opened_at": _iso(p.opened_at), "held_minutes": (now - p.opened_at).total_seconds() / 60,
                "max_hold_minutes": e.max_hold_minutes,
                "entry_price_usd": p.entry_price_usd, "price_usd": p.last_price_usd,
                "cost_basis_usd": p.cost_basis_usd, "initial_cost_usd": p.initial_cost_usd,
                "value_usd": value, "unrealized_usd": value - p.cost_basis_usd,
                "total_pnl_usd": value + p.proceeds_usd - p.initial_cost_usd,
                "pnl_pct": p.pnl_pct(), "peak_pct": peak_gain,
                "stop_price_usd": max(stop_price, trail_price or 0.0), "trailing_armed": trail_armed,
                "tp_hit": len(p.tp_levels_hit), "tp_total": len(e.take_profit_levels),
                "liquidity_change_pct": (p.last_liquidity_usd / p.entry_liquidity_usd - 1) * 100
                if p.entry_liquidity_usd else 0.0,
                "entry_score": p.entry_score,
            })
        positions.sort(key=lambda x: x["opened_at"] or "")

        closed = pf.closed
        wins = [c for c in closed if c.pnl_usd > 0]
        feed = None
        if self.feed is not None:
            feed = {"connected": self.feed.connected.is_set(), "buffered": len(self.feed.recent()),
                    "events": self.feed.events_received}
        kill = bool(cfg.agent.kill_switch_file) and Path(cfg.agent.kill_switch_file).exists()

        return {
            "mode": cfg.execution.mode, "synthetic": self.synthetic, "now": _iso(now),
            "started_at": _iso(self.started_at), "last_tick_at": _iso(a.last_tick_at),
            "ticks": a.ticks, "tick_errors": a.tick_errors, "tick_seconds": cfg.agent.tick_seconds,
            "speed": self.speed,
            "equity": equity, "cash": pf.cash_usd, "positions_value": pf.positions_value_usd(),
            "baseline_equity": baseline, "day_start_equity": day_start,
            "today_pnl": equity - day_start, "today_realized": realized_today,
            "realized_pnl": pf.total_realized_pnl(),
            "unrealized_pnl": sum(p["unrealized_usd"] for p in positions),
            "entries_paused": a.entries_paused, "kill_switch": kill, "entry_block": a.entry_block,
            "risk": {
                "open_positions": len(pf.positions), "max_open_positions": cfg.risk.max_open_positions,
                "entries_last_hour": entries_last_hour, "max_entries_per_hour": cfg.risk.max_entries_per_hour,
                "daily_loss_usd": loss_now, "daily_loss_limit_usd": loss_limit,
                "daily_loss_limit_pct": cfg.risk.max_daily_loss_pct,
            },
            "session": {
                "round_trips": len(closed), "wins": len(wins),
                "win_rate_pct": len(wins) / len(closed) * 100 if closed else None,
                "pnl_usd": sum(c.pnl_usd for c in closed),
            },
            "positions": positions,
            "trades": [trade_view(t) for t in reversed(a.recent_trades)][:200],
            "scan": {"at": _iso(a.last_scan_at), "every_s": cfg.agent.tick_seconds * cfg.agent.scan_every_ticks,
                     "candidates": a.last_scan},
            "feed": feed,
            "backtest_status": self.backtest.state.get("status"),
        }
