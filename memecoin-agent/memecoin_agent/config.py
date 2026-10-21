"""Configuration: typed defaults, overridable from a YAML file."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class FilterConfig:
    """Entry filters. A token must pass every one of these to be considered."""

    quote_symbols: list[str] = field(default_factory=lambda: ["SOL", "USDC"])
    min_liquidity_usd: float = 25_000
    max_liquidity_usd: float | None = None
    min_market_cap_usd: float = 50_000
    max_market_cap_usd: float = 5_000_000
    min_age_minutes: float = 20
    max_age_hours: float = 72
    min_volume_h1_usd: float = 20_000
    min_txns_h1: int = 150
    min_buy_sell_ratio_h1: float = 1.15
    min_buy_sell_ratio_m5: float = 1.0
    min_price_change_m5: float = 0.0
    min_price_change_h1: float = 5.0
    # Skip tokens that have already gone parabolic; chasing these is how you buy the top.
    max_price_change_h1: float = 250.0
    min_volume_to_liquidity_h1: float = 0.3
    # Composite momentum score threshold (0..1).
    min_score: float = 0.55
    # RugCheck: fail closed when the report is unavailable.
    require_rugcheck: bool = True
    max_rugcheck_score: float = 50.0
    reject_danger_risks: bool = True
    min_lp_locked_pct: float | None = None
    blacklist: list[str] = field(default_factory=list)
    # Extra mints to evaluate every scan, in addition to discovered ones.
    watchlist: list[str] = field(default_factory=list)


@dataclass
class TakeProfitLevel:
    at_pct: float
    sell_fraction: float


@dataclass
class ExitConfig:
    stop_loss_pct: float = 25.0
    take_profit_levels: list[TakeProfitLevel] = field(
        default_factory=lambda: [
            TakeProfitLevel(at_pct=50.0, sell_fraction=0.4),
            TakeProfitLevel(at_pct=120.0, sell_fraction=0.5),
        ]
    )
    # Trailing stop arms once the position is up `trailing_activation_pct` from entry,
    # then exits if price falls `trailing_stop_pct` from its peak.
    trailing_activation_pct: float = 30.0
    trailing_stop_pct: float = 20.0
    max_hold_minutes: float = 240.0
    # A sharp liquidity drop versus entry is the classic rug signature: get out at once.
    liquidity_drop_exit_pct: float = 40.0


@dataclass
class RiskConfig:
    starting_balance_usd: float = 1_000.0
    position_size_pct: float = 5.0
    max_position_usd: float = 100.0
    min_position_usd: float = 10.0
    max_open_positions: int = 5
    max_daily_loss_pct: float = 15.0
    token_cooldown_minutes: float = 180.0
    max_entries_per_hour: int = 6


@dataclass
class ExecutionConfig:
    mode: str = "paper"  # "paper" or "live"
    slippage_bps: int = 300
    # Paper mode: flat per-swap cost (DEX fee + priority fee) and a price-impact model.
    paper_fee_pct: float = 0.5
    paper_price_impact: bool = True
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_base_url: str = "https://lite-api.jup.ag/swap/v1"
    jupiter_api_key: str | None = None
    priority_fee_max_lamports: int = 500_000
    min_sol_reserve: float = 0.05
    confirm_timeout_s: float = 60.0
    # Live mode honeypot guard: quote buy then sell back; refuse if the round trip loses more.
    max_roundtrip_loss_pct: float = 12.0


@dataclass
class AgentConfig:
    tick_seconds: float = 15.0
    scan_every_ticks: int = 4
    max_candidates_per_scan: int = 60
    state_file: str = "state/state.json"
    trades_csv: str = "state/trades.csv"
    log_file: str | None = "state/agent.log"
    # If this file exists the agent opens no new positions (exits still run).
    kill_switch_file: str = "STOP"


@dataclass
class Config:
    filters: FilterConfig = field(default_factory=FilterConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    def validate(self) -> None:
        if self.execution.mode not in ("paper", "live"):
            raise ValueError(f"execution.mode must be 'paper' or 'live', got {self.execution.mode!r}")
        if not 0 < self.exits.stop_loss_pct < 100:
            raise ValueError("exits.stop_loss_pct must be between 0 and 100")
        if not 0 < self.exits.trailing_stop_pct < 100:
            raise ValueError("exits.trailing_stop_pct must be between 0 and 100")
        for lvl in self.exits.take_profit_levels:
            if not 0 < lvl.sell_fraction <= 1:
                raise ValueError("take_profit_levels[].sell_fraction must be in (0, 1]")
        if self.risk.max_open_positions < 1:
            raise ValueError("risk.max_open_positions must be >= 1")
        if self.risk.min_position_usd > self.risk.max_position_usd:
            raise ValueError("risk.min_position_usd cannot exceed risk.max_position_usd")
        if self.agent.scan_every_ticks < 1:
            raise ValueError("agent.scan_every_ticks must be >= 1")


_SECTIONS: dict[str, type] = {
    "filters": FilterConfig,
    "exits": ExitConfig,
    "risk": RiskConfig,
    "execution": ExecutionConfig,
    "agent": AgentConfig,
}


def _build_section(cls: type, data: dict[str, Any], name: str) -> Any:
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown config key(s) under '{name}': {', '.join(sorted(unknown))}")
    if cls is ExitConfig and "take_profit_levels" in data:
        data = {**data, "take_profit_levels": [TakeProfitLevel(**lvl) for lvl in data["take_profit_levels"] or []]}
    return cls(**data)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        cfg = Config()
    else:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError("Config file must contain a mapping at the top level")
        unknown = set(raw) - set(_SECTIONS)
        if unknown:
            raise ValueError(f"Unknown config section(s): {', '.join(sorted(unknown))}")
        cfg = Config(**{name: _build_section(cls, raw.get(name) or {}, name) for name, cls in _SECTIONS.items()})
    cfg.validate()
    return cfg
