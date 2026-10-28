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
    min_liquidity_usd: float = 12_000
    max_liquidity_usd: float | None = None
    # Low caps are the copy lane's (copy:): bought only when a tracked wallet buys them. On the agent's
    # own trades, buys under $150k won 9 of 29 (-$261); $150k-300k won 10 of 13 (+$167).
    min_market_cap_usd: float = 150_000
    max_market_cap_usd: float = 5_000_000
    min_age_minutes: float = 15  # most Solana rugs happen inside the first hour
    max_age_hours: float = 72
    min_volume_h1_usd: float = 10_000
    min_txns_h1: int = 80
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
    # Early or small buys (younger than this, or a market cap under this) must also pass the wallet scan
    # (launch-block bundles, linked wallets, top holders, creator: limits under `launch:`).
    wallet_scan_below_age_minutes: float = 60
    wallet_scan_below_market_cap_usd: float = 50_000


@dataclass
class TakeProfitLevel:
    at_pct: float
    sell_fraction: float


@dataclass
class ExitConfig:
    # let_it_run: take the initials and half the profit out once, then let the rest run (the keys
    # from hard_stop_pct down). classic: the older stop loss / take profit levels / trailing stop.
    style: str = "let_it_run"
    # -- let_it_run ---------------------------------------------------------------------------------
    hard_stop_pct: float = 50.0  # sell everything at this loss, checked every tick
    # A dip this deep starts reviews every dip_review_minutes. At a review, a dip still between
    # dip_watch_pct and dip_hold_max_pct is held if it is consolidating or accumulating; deeper, or
    # still sliding, it sells. Recovered above dip_watch_pct: the watch ends.
    dip_watch_pct: float | None = 30.0  # null: no dip reviews, only the hard stop
    dip_hold_max_pct: float = 40.0
    dip_review_minutes: float = 30.0
    # Consolidating: since the last review it slipped no more than max_slide and either traded in a
    # range of max_range or buyers outnumbered sellers over the last hour (accumulating).
    consolidation_max_range_pct: float = 25.0
    consolidation_max_slide_pct: float = 10.0
    # Up this much: sell the initial cost plus secure_profit_share of the profit, keep the rest. Replaying
    # the agent's last 42 trades, +25% did best: 24 got their initials out (vs 16 wins before).
    secure_at_pct: float = 25.0
    secure_profit_share: float = 0.5
    # The rest (the runner) is checked every runner_review_minutes and sold in full only once it is
    # runner_drawdown_pct below its highest price since then. No time stop on runners.
    runner_review_minutes: float = 30.0
    runner_drawdown_pct: float = 70.0
    # -- classic ------------------------------------------------------------------------------------
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
    # -- both ---------------------------------------------------------------------------------------
    # Time stop: sell whatever is still held after this long (12 hours); let_it_run runners are exempt.
    max_hold_minutes: float | None = 720.0
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
    # Memes (classify): "lookup" = only LarpCheck's free lookup: a bad verdict someone else got still
    # blocks the buy, but no verdict is required, no check is started and no credit is spent.
    # "full" = same rules as tech tokens. "off" = LarpCheck isn't consulted for memes at all.
    memes: str = "lookup"


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
    # Your plan's rate (free 5, Plus 20, Pro 50): the agent, copy tracker and long-term scanner share it.
    rate_per_s: float = 5.0


@dataclass
class ClassifyConfig:
    """Tech or meme, from the token's name, description and links (data/profile.py). Tech tokens
    (launchpads, AI agents, apps, protocols...) get the full LarpCheck gate; see larpcheck.memes."""

    enabled: bool = True
    extra_tech_words: list[str] = field(default_factory=list)  # added to the built-in list


@dataclass
class AuthenticityConfig:
    """Fake-chart check on every candidate, before any paid check: wash trading and bot churn, from
    DexScreener's numbers and RugCheck's holder count. Limits measured on live tokens: real active
    coins stayed under ~20x liquidity an hour and ~$400 of volume per holder; wash-traded ones ran
    50-200x and $1,000-8,000."""

    enabled: bool = True
    max_turnover_per_hour: float = 50.0  # 24h volume / liquidity, per hour of the pool's life (1-24h)
    max_volume_per_holder_usd: float = 1_500.0  # 24h volume / current holders
    max_trades_per_holder_h1: float = 8.0  # last hour's trades / current holders
    min_holders: int = 100
    reject_holder_correlation: bool = True  # RugCheck's "High holder correlation": wallets moving together
    # Bots spamming tiny trades to fake activity: real coins averaged $17+ a trade on the live sample,
    # the bot-run ones $4-5 (last hour, once it has 200+ trades).
    min_avg_trade_usd: float = 12.0
    # Already ran: a coin up this much in 6h is a late chase (or a pump being painted). null = off.
    max_price_change_h6: float | None = 500.0
    # Painted chart: a coin under a day old whose price rises while an hour's volume is under this share
    # of its market cap (real young coins traded 2.5%+ of their cap an hour on the sample). null = off.
    min_volume_to_mcap_h1: float | None = 0.02
    # Just dumped: the price the agent itself saw over the last 2 hours fell this far from its high.
    max_drawdown_from_high_pct: float | None = 50.0
    # Honeypot settings on the token itself (RugCheck's report): live mint or freeze authority, a
    # permanent delegate, a transfer hook, non-transferable or frozen-by-default accounts.
    reject_honeypot_mint: bool = True
    max_transfer_fee_pct: float = 5.0


@dataclass
class HoneypotConfig:
    """Before a paper buy, ask Jupiter to quote the buy and then selling it straight back (quotes only,
    no wallet). No sell route, or a round trip losing more than execution.max_roundtrip_loss_pct,
    means no buy. Live mode always does this in the executor."""

    sell_route_check: bool = True
    fail_closed: bool = True  # Jupiter unreachable: skip the buy


@dataclass
class LaunchConfig:
    """Launch lane: a token that trades $400k+ within its first 30 minutes is bought without the
    momentum filters, once a wallet scan clears it. RugCheck, GMGN and LarpCheck still apply."""

    enabled: bool = True
    max_age_minutes: float = 30.0
    min_volume_usd: float = 400_000.0
    min_liquidity_usd: float = 15_000.0
    # Skip a launch that is crashing right now (5-minute price change below this); null = off.
    min_price_change_m5: float | None = -20.0
    # A launch that qualified stays buyable this long while its wallet scan and LarpCheck finish.
    buy_window_minutes: float = 15.0
    # Wallet scan: the chain (execution.rpc_url) back to the launch block, plus RugCheck's holder report.
    # % of supply the wallets that bought in the launch block (bundles, plus the creator's own buy) took
    # then, or still hold now: the higher of the two must stay at or under this.
    max_bundle_pct: float = 30.0
    max_cluster_pct: float = 30.0  # % held by linked wallets (RugCheck insider networks / insider holders)
    max_creator_pct: float = 10.0
    max_top10_pct: float = 35.0  # top 10 holders, pools and lockers left out
    max_signature_pages: int = 150  # 1,000 transactions a page when walking back to the launch


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
class CopyConfig:
    """Copy lane: low caps (under filters.min_market_cap_usd) are bought only when a tracked wallet
    buys them, on any chain GMGN covers. Needs a GMGN API key. Other chains are paper-only."""

    enabled: bool = True
    chains: list[str] = field(default_factory=lambda: ["sol", "bsc", "base", "eth", "robinhood"])  # GMGN names
    # Which wallets. Found in GMGN's smart-money and KOL trade feeds and among the top traders of
    # trending tokens, then vetted on their last 30 days:
    min_win_rate: float = 0.10
    max_win_rate: float = 0.30
    min_wallet_age_days: float = 60  # first funded at least this long ago
    min_losing_trades: int = 3  # has taken losses, not a 100%-hit insider wallet
    min_trades_30d: int = 10  # still active
    min_pnl: float | None = 0.0  # 30-day realized profit / cost: wins must outweigh the losses (null = off)
    max_wallets_per_chain: int = 15
    refresh_hours: float = 6.0
    wallets: list[str] = field(default_factory=list)  # always tracked: "chain:address" (plain address = sol)
    # Which buys: a real buy (not a transfer in, not the wallet's own launch) of a low cap.
    min_market_cap_usd: float = 5_000
    min_liquidity_usd: float = 5_000
    min_wallet_buy_usd: float = 50
    max_signal_age_minutes: float = 10
    min_wallets: int = 1  # tracked wallets that must have bought it
    poll_seconds: float = 3.0  # one wallet's activity per poll, within GMGN's rate budget
    position_usd: float | None = None  # null: the usual risk sizing
    state_file: str = "state/copy_wallets.json"
    fail_closed: bool = True  # no honeypot/tax check available on that chain: skip the buy


@dataclass
class LongTermConfig:
    """Long-term lane: coins over a year old that wore down gradually but still trade every day, with
    big holders still in. Bought with their own budget and slots; held without a time stop."""

    enabled: bool = True
    chains: list[str] = field(default_factory=lambda: ["solana", "ethereum", "bsc", "base", "robinhood"])
    watchlist: list[str] = field(default_factory=list)  # "chain:address"; the age rule doesn't apply to these
    discovery: bool = True  # GMGN: tokens over min_age_days old, ranked by 24h volume (needs a GMGN key)
    rescan_hours: float = 6.0
    min_age_days: float = 365
    min_liquidity_usd: float = 20_000
    min_market_cap_usd: float = 50_000
    max_market_cap_usd: float = 10_000_000
    min_volume_h24_usd: float = 2_000
    min_buys_h24: int = 25  # still gets daily buys
    min_active_days: int = 25  # of the last 30 daily candles, days with trading
    min_drawdown_from_high_pct: float = 60  # worn down: this far below its high
    max_daily_drop_pct: float = 40  # gradual: no single day in the last month fell more than this
    max_drop_7d_pct: float = 35  # bottoming: not still sliding hard this week
    min_holders: int = 300
    min_large_holders: int = 5  # holders with at least large_holder_usd (pools left out)
    large_holder_usd: float = 1_000
    position_usd: float = 50
    max_positions: int = 5
    hard_stop_pct: float = 50
    secure_at_pct: float = 100  # initials + half the profit out at 2x; the rest runs


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
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)
    authenticity: AuthenticityConfig = field(default_factory=AuthenticityConfig)
    honeypot: HoneypotConfig = field(default_factory=HoneypotConfig)
    launch: LaunchConfig = field(default_factory=LaunchConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)
    longterm: LongTermConfig = field(default_factory=LongTermConfig)

    def validate(self) -> None:
        if self.execution.mode not in ("paper", "live"):
            raise ValueError(f"execution.mode must be 'paper' or 'live', got {self.execution.mode!r}")
        e = self.exits
        if e.style not in ("let_it_run", "classic"):
            raise ValueError("exits.style must be 'let_it_run' or 'classic'")
        if not 0 < e.hard_stop_pct <= 100 or not 0 < e.runner_drawdown_pct <= 100:
            raise ValueError("exits.hard_stop_pct and exits.runner_drawdown_pct must be between 0 and 100")
        if e.secure_at_pct <= 0 or not 0 <= e.secure_profit_share <= 1:
            raise ValueError("exits.secure_at_pct must be > 0 and exits.secure_profit_share between 0 and 1")
        if e.dip_watch_pct is not None and not 0 < e.dip_watch_pct <= e.dip_hold_max_pct:
            raise ValueError("exits.dip_watch_pct must be > 0 and no deeper than exits.dip_hold_max_pct")
        if min(e.dip_review_minutes, e.runner_review_minutes) <= 0:
            raise ValueError("exits.dip_review_minutes and exits.runner_review_minutes must be > 0")
        if not 0 <= self.copy.min_win_rate <= self.copy.max_win_rate <= 1:
            raise ValueError("copy.min_win_rate and copy.max_win_rate are ratios: 0 <= min <= max <= 1")
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
        if self.larpcheck.memes not in ("lookup", "full", "off"):
            raise ValueError("larpcheck.memes must be 'lookup', 'full' or 'off'")
        if self.launch.max_age_minutes <= 0 or self.launch.buy_window_minutes < 0:
            raise ValueError("launch.max_age_minutes must be > 0 and launch.buy_window_minutes >= 0")
        if self.launch.max_signature_pages < 1:
            raise ValueError("launch.max_signature_pages must be >= 1")
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
    "classify": ClassifyConfig,
    "authenticity": AuthenticityConfig,
    "honeypot": HoneypotConfig,
    "launch": LaunchConfig,
    "export": ExportConfig,
    "telegram": TelegramConfig,
    "copy": CopyConfig,
    "longterm": LongTermConfig,
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
