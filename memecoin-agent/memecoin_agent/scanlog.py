"""Scan-everything data logger.

Logs EVERY coin the agent scans (not just the ones it buys): its features when first seen, whether it passed the
filters, why it was rejected or skipped, then what the price and liquidity did 15 minutes, 1 hour and 4 hours later,
the best and worst it got, and whether it rugged. That is the data to tune filters and scores on (thousands of
samples instead of ~75 trades), and the launchpad win-rate data the narrative sniper needs.

Duck-typed: a snapshot only needs .mint .symbol .chain .price_usd .liquidity_usd .market_cap_usd .volume_h1
.buys_h1 .sells_h1 .price_change_m5 .price_change_h1 .dex_id and .age_minutes(now); an evaluation needs
.snapshot .passed .score .reasons .lane. No other imports, so the same file runs inside the agent.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Callable

HORIZONS = (900, 3600, 14400)  # seconds after first sight
HNAME = {900: "15m", 3600: "1h", 14400: "4h"}
GONE_GRACE_S = 1800  # no data this long after a horizon: the coin is gone (counted as -100%)
RUG_LIQ_RATIO = 0.2  # liquidity fell to 20% of its starting level
COLS = ["time", "mint", "symbol", "chain", "dex", "lane", "passed", "bought", "outcome", "reasons", "score", "kind",
        "age_min", "price0", "liq0", "mcap0", "vol_h1", "txns_h1", "bs_h1", "chg_m5", "chg_h1",
        "ret_15m", "ret_1h", "ret_4h", "max_ret", "min_ret", "min_liq_ratio", "rugged", "gone", "obs"]


def _num(x: Any, nd: int = 4) -> float:
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return 0.0


class ScanLog:
    def __init__(self, csv_path: str, pending_path: str | None = None, horizons: tuple[int, ...] = HORIZONS,
                 max_pending: int = 4000, relog_after_s: float = 6 * 3600, clock: Callable[[], float] = time.time) -> None:
        self.csv_path = Path(csv_path)
        self.pending_path = Path(pending_path) if pending_path else self.csv_path.with_name("scans_pending.json")
        self.horizons, self.max_pending, self.relog_after_s, self.clock = horizons, max_pending, relog_after_s, clock
        self.pending: dict[str, dict] = {}
        self.recent: dict[str, float] = {}  # mint -> when last logged (no duplicate rows inside relog_after_s)
        self._last_follow = 0.0
        self._last_save = 0.0
        self.done_rows = 0
        if self.pending_path.exists():
            try:
                d = json.loads(self.pending_path.read_text(encoding="utf-8"))
                self.pending, self.recent = d.get("pending", {}), d.get("recent", {})
            except (OSError, ValueError):
                pass

    # -- logging -------------------------------------------------------------------------------
    def observe(self, now: float, evals: list[Any], outcomes: dict[str, str] | None = None,
                kinds: dict[str, str] | None = None) -> None:
        """Call after every scan with the evaluations and what happened to each (outcomes: mint -> text)."""
        outcomes, kinds = outcomes or {}, kinds or {}
        for e in evals:
            s = e.snapshot
            if not getattr(s, "price_usd", 0):
                continue
            row = self.pending.get(s.mint)
            if row is not None:
                self._update(row, now, s.price_usd, s.liquidity_usd)
                out = outcomes.get(s.mint)
                if out:
                    row["outcome"], row["bought"] = out, int(out == "bought")
                continue
            if now - self.recent.get(s.mint, 0) < self.relog_after_s or len(self.pending) >= self.max_pending:
                continue
            age = s.age_minutes(self._dt(now)) if hasattr(s, "age_minutes") else None
            txns = (getattr(s, "buys_h1", 0) or 0) + (getattr(s, "sells_h1", 0) or 0)
            bs = (s.buys_h1 / s.sells_h1) if getattr(s, "sells_h1", 0) else float(s.buys_h1 or 0)
            out = outcomes.get(s.mint, "")
            self.pending[s.mint] = {
                "t0": now, "mint": s.mint, "symbol": s.symbol, "chain": getattr(s, "chain", "solana"),
                "dex": getattr(s, "dex_id", ""), "lane": getattr(e, "lane", ""), "passed": int(bool(e.passed)),
                "bought": int(out == "bought"), "outcome": out, "reasons": "; ".join(e.reasons[:3])[:200],
                "score": _num(e.score, 3), "kind": kinds.get(s.mint, ""), "age_min": _num(age, 1) if age is not None else "",
                "price0": s.price_usd, "liq0": _num(s.liquidity_usd, 0), "mcap0": _num(s.market_cap_usd, 0),
                "vol_h1": _num(s.volume_h1, 0), "txns_h1": txns, "bs_h1": _num(bs, 2),
                "chg_m5": _num(s.price_change_m5, 1), "chg_h1": _num(s.price_change_h1, 1),
                "max_price": s.price_usd, "min_price": s.price_usd, "min_liq_ratio": 1.0, "obs": {}}
            self.recent[s.mint] = now
        if len(self.recent) > 20000:
            cut = now - self.relog_after_s
            self.recent = {m: t for m, t in self.recent.items() if t > cut}

    @staticmethod
    def _dt(now: float):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(now, tz=timezone.utc)

    def _update(self, row: dict, now: float, price: float, liq: float) -> None:
        row["max_price"], row["min_price"] = max(row["max_price"], price), min(row["min_price"], price)
        if row["liq0"]:
            row["min_liq_ratio"] = min(row["min_liq_ratio"], liq / row["liq0"])
        for h in self.horizons:
            key = str(h)
            if key not in row["obs"] and now - row["t0"] >= h:
                row["obs"][key] = [now, price, liq]

    # -- follow-ups ----------------------------------------------------------------------------
    def follow_up(self, now: float, fetch: Callable[[list[str]], dict[str, Any]], every_s: float = 60.0,
                  batch: int = 60) -> int:
        """Fill in the price at each horizon for coins that are due (they may have left the scan lists).
        `fetch(mints)` returns {mint: snapshot}. Returns the number of rows finished."""
        if now - self._last_follow < every_s:
            return 0
        self._last_follow = now
        due = [(r["t0"] + min(h for h in self.horizons if str(h) not in r["obs"]), m)
               for m, r in self.pending.items() if any(str(h) not in r["obs"] for h in self.horizons)]
        due = sorted(t for t in due if t[0] <= now)
        mints = [m for _, m in due[:batch]]
        snaps: dict[str, Any] = {}
        if mints:
            try:
                snaps = fetch(mints) or {}
            except Exception:
                snaps = {}
        for m in mints:
            s = snaps.get(m)
            if s is not None and getattr(s, "price_usd", 0):
                self._update(self.pending[m], now, s.price_usd, s.liquidity_usd)
        finished = 0
        for m in list(self.pending):
            r = self.pending[m]
            for h in self.horizons:  # no data well past a horizon: the coin is gone
                if str(h) not in r["obs"] and now - r["t0"] >= h + GONE_GRACE_S:
                    r["obs"][str(h)] = [now, 0.0, 0.0]
                    r["gone"] = 1
            if all(str(h) in r["obs"] for h in self.horizons):
                self._write(self.pending.pop(m))
                finished += 1
        if finished or now - self._last_save > 120:
            self.save(now)
        return finished

    def _ret(self, row: dict, h: int) -> str:
        o = row["obs"].get(str(h))
        return "" if not o or not row["price0"] else f"{(o[1] / row['price0'] - 1) * 100:.1f}"

    def _write(self, row: dict) -> None:
        r15, r1, r4 = (self._ret(row, h) for h in self.horizons[:3])
        rugged = int(row["min_liq_ratio"] <= RUG_LIQ_RATIO or (r1 != "" and float(r1) <= -80) or bool(row.get("gone")))
        out = {**row, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["t0"])), "ret_15m": r15, "ret_1h": r1,
               "ret_4h": r4, "max_ret": f"{(row['max_price'] / row['price0'] - 1) * 100:.1f}" if row["price0"] else "",
               "min_ret": f"{(row['min_price'] / row['price0'] - 1) * 100:.1f}" if row["price0"] else "",
               "min_liq_ratio": round(row["min_liq_ratio"], 3), "rugged": rugged, "gone": int(bool(row.get("gone"))),
               "obs": len(row["obs"])}
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(out)
        self.done_rows += 1

    def save(self, now: float | None = None) -> None:
        self._last_save = now if now is not None else self.clock()
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pending_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pending": self.pending, "recent": self.recent}), encoding="utf-8")
        os.replace(tmp, self.pending_path)


# -- the readout: what the data says about the filters ---------------------------------------------
def _f(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(path: str | Path) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _line(label: str, rows: list[dict]) -> str:
    r1 = [v for v in (_f(r["ret_1h"]) for r in rows) if v is not None]
    mx = [v for v in (_f(r["max_ret"]) for r in rows) if v is not None]
    runners = sum(1 for v in mx if v >= 100) / len(mx) if mx else 0
    rug = sum(1 for r in rows if r["rugged"] == "1") / len(rows)
    med = statistics.median(r1) if r1 else 0
    avg = sum(max(min(v, 300), -100) for v in r1) / len(r1) if r1 else 0  # capped so one moonshot doesn't rule
    return f"  {label:<30} n={len(rows):5}  runners(+100%) {runners:5.1%}  rugged {rug:5.1%}  median 1h {med:+7.1f}%  avg 1h(capped) {avg:+7.1f}%"


def _band(rows: list[dict], col: str, edges: list[float], unit: str = "") -> list[str]:
    out = []
    for lo, hi in zip(edges, edges[1:] + [float("inf")]):
        g = [r for r in rows if (v := _f(r[col])) is not None and lo <= v < hi]
        if len(g) >= 5:
            out.append(_line(f"{col} {lo:g}-{'up' if hi == float('inf') else f'{hi:g}'}{unit}", g))
    return out


def reason_key(reasons: str) -> str:
    first = (reasons or "").split(";")[0].strip()
    for k in ("mcap", "age", "vol1h", "txns1h", "b/s1h", "b/s5m", "chg5m", "chg1h", "vol/liq", "score", "liquidity", "quote"):
        if first.startswith(k):
            return k + (" too high" if "> " in first or "extended" in first else " too low" if k not in ("score",) else "")
    return first[:24] or "(passed filters)"


def build_scan_report(path: str | Path, min_rows: int = 30) -> str:
    rows = load_rows(path)
    if len(rows) < min_rows:
        return (f"Scan log: {len(rows)} finished rows so far (need {min_rows}+). Coins are logged when first scanned and "
                "finished 4 hours later, so it takes about a day to fill in.")
    out = [f"Scan log: {len(rows)} coins scanned, each followed for 4 hours.", "", "All coins:", _line("everything scanned", rows)]
    out += ["", "By what the agent did:"]
    for label, g in (("passed the filters", [r for r in rows if r["passed"] == "1"]),
                     ("rejected by the filters", [r for r in rows if r["passed"] != "1"]),
                     ("bought", [r for r in rows if r["bought"] == "1"]),
                     ("passed but NOT bought", [r for r in rows if r["passed"] == "1" and r["bought"] != "1"])):
        if g:
            out.append(_line(label, g))
    out += ["", "Why coins were rejected (first failing filter): high 'runners' here means that filter blocks winners"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["passed"] != "1":
            groups.setdefault(reason_key(r["reasons"]), []).append(r)
    for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(g) >= 8:
            out.append(_line(k, g))
    out += ["", "Why passed coins were not bought:"]
    skipped: dict[str, list[dict]] = {}
    for r in rows:
        if r["passed"] == "1" and r["bought"] != "1":
            skipped.setdefault((r["outcome"] or "?").split(":")[0][:28], []).append(r)
    for k, g in sorted(skipped.items(), key=lambda kv: -len(kv[1])):
        if len(g) >= 5:
            out.append(_line(k, g))
    out += ["", "By coin features at first sight:"]
    for col, edges, unit in (("age_min", [0, 15, 30, 60, 180, 720], "m"), ("mcap0", [0, 25_000, 50_000, 100_000, 250_000, 1_000_000], ""),
                             ("liq0", [0, 10_000, 25_000, 50_000, 150_000], ""), ("score", [0, 0.4, 0.55, 0.7, 0.85], ""),
                             ("bs_h1", [0, 0.8, 1.0, 1.3, 2.0], ""), ("chg_h1", [-100, 0, 25, 100, 300], "%")):
        out += _band(rows, col, edges, unit)
    return "\n".join(out)
