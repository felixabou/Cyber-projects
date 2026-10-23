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
    # Time stop: sell whatever is still held after this long (12 hours).
    max_hold_minutes: float = 720.0
    # A sharp liquidity drop versus entry is the classic rug signature: get out at once.
    liquidity_drop_exit_pct: float = 40.0


@dataclass
class RiskConfig:
    starting_balance_usd: float = 1_000.0
    position_size_pct: float = 5.0
    max_position_usd: float = 100.0
    min_position_usd: float = 10.0
    max_open_positions: int = 20  # dust doesn't count
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
    tick_seconds: float = 10.0  # exits are checked every tick
    scan_every_ticks: int = 2  # discovery scan every 20s
    max_candidates_per_scan: int = 150
    state_file: str = "state/state.json"
    trades_csv: str = "state/trades.csv"
    log_file: str | None = "state/agent.log"
    # If this file exists the agent opens no new positions (exits still run).
    kill_switch_file: str = "STOP"


@dataclass
class FeedConfig:
    """Real-time launch feed (PumpPortal websocket) merged into discovery."""

    # Pump.fun graduations: tokens that just got a real AMM pool. Low volume, high signal.
    pumpportal_migrations: bool = True
    # Every new pump.fun token (thousands a day, mostly still on the bonding curve).
    # Only useful if you loosen the liquidity/age filters to trade bonding-curve tokens.
    pumpportal_new_tokens: bool = False
    pumpportal_url: str = "wss://pumpportal.fun/api/data"
    # Mints are kept until older than filters.max_age_hours, or evicted once their
    # pool is clearly dead (30+ min after the event) so they stop using scan budget.
    # Lower this if you enable new_tokens to trade bonding-curve tokens.
    evict_below_liquidity_usd: float = 5_000.0
    max_buffered_mints: int = 2_000
    buffer_file: str | None = "state/feed.json"


@dataclass
class RecorderConfig:
    dir: str = "recordings"
    interval_s: float = 30.0
    discover_every_ticks: int = 2
    # Keep sampling each discovered token this long, so backtests see what happened after entry.
    track_hours: float = 12.0
    max_tracked: int = 400
    record_safety: bool = True
    # Record from inside `run`/`ui`, sharing the agent's data clients and launch-feed connection.
    record_while_trading: bool = False


@dataclass
class LarpCheckConfig:
    """LarpCheck (larpcheck.app) verdicts as a last gate before a buy, after the filters and RugCheck."""

    enabled: bool = True
    url: str = "https://larpcheck.app/mcp"
    # The LARPCHECK_API_KEY environment variable wins over this file.
    api_key_file: str | None = "~/.config/larpcheck/api_key"
    reject_verdicts: list[str] = field(default_factory=lambda: ["LARP", "LIKELY LARP", "SCAM"])
    # Coin stamps are OK / NEUTRAL / SKETCHY / RIGGED (how fair the coin is for a buyer).
    reject_coin_stamps: list[str] = field(default_factory=lambda: ["RIGGED"])
    min_coin_score: float | None = None
    # Never buy a coin LarpCheck hasn't checked: no verdict means no buy, whatever the reason
    # (unchecked and no credits left, a check that didn't finish, LarpCheck down, no API key).
    require_verdict: bool = True
    # A coin nobody has checked yet: "check" (start one, spending a daily credit, and buy only once
    # it passes), "reject", or "allow" (free; other checks decide; only with require_verdict: false).
    on_unchecked: str = "check"
    max_checks_per_day: int = 4
    # How long to hold off a buy while a check is running before falling back to on_unchecked.
    pending_max_minutes: float = 5.0
    # LarpCheck unreachable: false = let the other checks decide, true = skip the buy.
    fail_closed: bool = False
    # Held coins: re-read LarpCheck's verdict every few minutes (free); when it flags one, read the
    # reasoning and sell. Coins rated one of dust_verdicts sell exit_sell_fraction and keep the rest
    # as dust; any other flagged coin (LARP, SCAM, RIGGED on another verdict) sells in full.
    exit_on_flag: bool = True
    exit_verdicts: list[str] = field(default_factory=lambda: ["LIKELY LARP", "LARP", "SCAM"])
    exit_coin_stamps: list[str] = field(default_factory=lambda: ["RIGGED"])
    dust_verdicts: list[str] = field(default_factory=lambda: ["LIKELY LARP", "LIKELY REAL"])
    # ...and only if the coin stamp is one of these and no check has ever stamped it RIGGED.
    dust_coin_stamps: list[str] = field(default_factory=lambda: ["OK"])
    exit_sell_fraction: float = 0.8
    # A held coin rated one of dust_verdicts whose LarpCheck view then changes (a new check, or a
    # different verdict or coin stamp) is sold the same way.
    sell_on_change: bool = True
    held_recheck_minutes: float = 5.0
    # Opening a full report of a check someone else ran costs a credit on the free plan.
    max_report_opens_per_day: int = 1


@dataclass
class GmgnConfig:
    """GMGN OpenAPI (gmgn.ai): extra token discovery (trending) and a pre-buy safety check.
    Read-only data endpoints only need the API key; nothing here trades."""

    enabled: bool = True
    host: str = "https://openapi.gmgn.ai"
    # The GMGN_API_KEY environment variable wins; otherwise a GMGN_API_KEY=... line in this file
    # (the file `gmgn-cli config --apply` writes).
    api_key_file: str | None = "~/.config/gmgn/.env"

    discovery: bool = True
    trending_interval: str = "1h"  # 1m / 5m / 1h / 6h / 24h
    trending_limit: int = 100
    trending_order_by: str = "volume"
    # Server-side tags; GMGN's SOL defaults are renounced + frozen, sent explicitly here.
    trending_filters: list[str] = field(default_factory=lambda: ["renounced", "frozen", "not_wash_trading"])
    discover_every_s: float = 120.0

    safety: bool = True
    max_rug_ratio: float | None = 0.3
    reject_wash_trading: bool = True
    max_top10_holder_rate: float | None = 0.35
    max_dev_team_hold_rate: float | None = 0.10
    max_creator_hold_rate: float | None = 0.10
    max_insider_hold_rate: float | None = 0.15
    max_bundler_rate: float | None = 0.30
    max_rat_trader_rate: float | None = 0.30
    max_sell_tax: float | None = 0.10
    max_fresh_wallet_rate: float | None = None
    max_creator_launches: int | None = None  # creator's previous launches (serial deployers)
    min_smart_wallets: int = 0  # require this many smart-money holders (0 = off)
    # GMGN unreachable / rate limited: false = the other checks decide, true = skip the buy.
    fail_closed: bool = False


@dataclass
class ExportConfig:
    """Trade CSVs saved on a schedule for downloading later: one file per period (UTC clock hours by
    default) that had trades, e.g. exports/trades_2026-10-22_1300-1400_UTC.csv."""

    enabled: bool = True
    dir: str = "exports"
    every_minutes: int = 60


@dataclass
class TelegramConfig:
    """Telegram bot: trade alerts, the hourly CSVs and remote control from your phone.
    On whenever a bot token is configured (TELEGRAM_BOT_TOKEN, or token_file)."""

    enabled: bool = True
    token_file: str | None = "~/.config/memecoin-agent/telegram_token"
    # Only these chats can use the bot; the TELEGRAM_CHAT_ID environment variable (comma-separated)
    # adds to the list. Message the bot from a chat that isn't listed and it replies with its id.
    allowed_chat_ids: list[int] = field(default_factory=list)
    notify_trades: bool = True
    send_exports: bool = True  # post each hourly trade CSV to the chat
    # /pause, /resume, /scan, /close and /closeall. Off = the bot only reports.
    allow_controls: bool = True


@dataclass
class Config:
    filters: FilterConfig = field(default_factory=FilterConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    feeds: FeedConfig = field(default_factory=FeedConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    larpcheck: LarpCheckConfig = field(default_factory=LarpCheckConfig)
    gmgn: GmgnConfig = field(default_factory=GmgnConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

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
        if self.gmgn.trending_interval not in ("1m", "5m", "1h", "6h", "24h"):
            raise ValueError("gmgn.trending_interval must be 1m, 5m, 1h, 6h or 24h")
        if not 0 < self.larpcheck.exit_sell_fraction <= 1:
            raise ValueError("larpcheck.exit_sell_fraction must be in (0, 1]")
        if self.larpcheck.on_unchecked not in ("allow", "check", "reject"):
            raise ValueError("larpcheck.on_unchecked must be 'allow', 'check' or 'reject'")
        if self.larpcheck.require_verdict and self.larpcheck.on_unchecked == "allow":
            raise ValueError("larpcheck.on_unchecked: allow contradicts require_verdict: true "
                             "(use 'check' or 'reject', or set require_verdict: false)")
        if self.recorder.interval_s <= 0 or self.recorder.discover_every_ticks < 1:
            raise ValueError("recorder.interval_s must be > 0 and recorder.discover_every_ticks >= 1")
        if not 0 < self.export.every_minutes <= 1440 or 1440 % self.export.every_minutes:
            raise ValueError("export.every_minutes must divide a day evenly (e.g. 15, 30, 60, 120, 1440)")


_SECTIONS: dict[str, type] = {
    "filters": FilterConfig,
    "exits": ExitConfig,
    "risk": RiskConfig,
    "execution": ExecutionConfig,
    "agent": AgentConfig,
    "feeds": FeedConfig,
    "recorder": RecorderConfig,
    "larpcheck": LarpCheckConfig,
    "gmgn": GmgnConfig,
    "export": ExportConfig,
    "telegram": TelegramConfig,
}


def _build_section(cls: type, data: dict[str, Any], name: str) -> Any:
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown config key(s) under '{name}': {', '.join(sorted(unknown))}")
    if cls is ExitConfig and "take_profit_levels" in data:
        data = {**data, "take_profit_levels": [TakeProfitLevel(**lvl) for lvl in data["take_profit_levels"] or []]}
    return cls(**data)


def apply_override(cfg: Config, assignment: str) -> None:
    """Apply a `section.key=value` override (value parsed as YAML), e.g. `filters.min_score=0.6`."""
    dotted, sep, raw_value = assignment.partition("=")
    section, _, key = dotted.strip().partition(".")
    if not sep or section not in _SECTIONS or not key:
        raise ValueError(f"Override must look like section.key=value, got {assignment!r}")
    current = dataclasses.asdict(getattr(cfg, section))
    if key not in current:
        raise ValueError(f"Unknown config key '{section}.{key}'")
    current[key] = yaml.safe_load(raw_value)
    setattr(cfg, section, _build_section(_SECTIONS[section], current, section))
    cfg.validate()


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
