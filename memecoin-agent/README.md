# memecoin-agent

An automated momentum trading agent for Solana memecoins. It finds new tokens on
DexScreener, screens out obvious rugs with RugCheck, enters tokens with real
liquidity and steady buy pressure, and exits on fixed rules. It **paper trades by
default**. Live trading goes through Jupiter and has to be switched on explicitly.

> **Read this first.** Most memecoins go to zero, and most automated memecoin
> strategies lose money once fees, slippage, MEV and rugs are counted. This is a
> framework with sensible guardrails, not a proven edge. Paper trade for at least
> a week of live market data before risking anything. If you go live, use a
> dedicated hot wallet that holds only what you can afford to lose.

## How it works

Each tick (15s by default):

1. **Mark & exit.** Re-price every open position and apply the exit rules, in this order:
   - **Liquidity pulled.** Liquidity falls ≥40% below its level at entry, which is the classic rug signature. Sell everything at once.
   - **Stop loss.** −25% from the effective entry price.
   - **Trailing stop.** Once the position has been +30% at its peak, exit if price falls 20% from that peak.
   - **Time stop.** Exit after 240 minutes.
   - **Staged take-profit.** Sell 40% at +50%, then 50% of what's left at +120%. A trailing stop rides the remainder.
2. **Scan & enter** (every 4th tick):
   - Discover mints from three sources:
     - **Pump.fun graduations, in real time.** A background websocket to PumpPortal sees each token the moment it moves from the bonding curve to a real AMM pool. These go first in the scan.
     - DexScreener's latest-profiles and boost feeds.
     - Your `watchlist`.
   - Keep tokens that pass every filter:
     - liquidity, market cap and pair-age windows;
     - 1h volume, transaction count and volume/liquidity turnover;
     - buy/sell ratio over 5m and 1h;
     - positive 5m/1h momentum, but not already parabolic (>250% in 1h).
   - Rank the survivors by a 0–1 momentum score.
   - Run RugCheck on each one. A danger-level risk (live mint or freeze authority, unlocked LP, concentrated holders…) or a high risk score rejects the token. If RugCheck can't be reached, the token is rejected.
   - **Live only:** quote the buy, then quote selling the tokens straight back. Skip the token if that round trip loses more than 12%. This catches honeypots and thin routes.
   - Size the position: 5% of equity, capped at $100.
3. **Risk limits:**
   - at most 5 open positions;
   - at most 6 entries per hour;
   - a 3-hour cooldown per token after an exit;
   - new entries stop for the rest of the day once realized plus unrealized losses reach 15% of the day's starting equity;
   - a kill-switch file (`touch STOP`) stops new entries, while exits keep running.

Every parameter is in [`config.example.yaml`](config.example.yaml).

## Quick start

```bash
cd memecoin-agent
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

memecoin-agent ui --synthetic --record   # try everything offline on a simulated market
cp config.example.yaml config.yaml
memecoin-agent -c config.yaml --record   # paper trade + record data + dashboard, one process
```

The bare command opens the dashboard (`memecoin-agent` is short for
`memecoin-agent ui`). One process runs the whole session:

```
                ┌──────────────── memecoin-agent (ui | run) ────────────────┐
 PumpPortal ws ─┤► launch feed ─┐                                          │
 DexScreener  ──┤► market data ─┼─► agent: exits → scan → RugCheck → buy ──┼─► paper fills / Jupiter swaps
 RugCheck     ──┤►──────────────┘        │                                 │
                │                 recorder (--record) ──► recordings/ ─────┼─► backtests (dashboard or CLI)
                │                 dashboard (ui only) ◄── views / controls │
                └───────────────────────────────────────────────────────────┘
```

- The trading loop, launch feed, recorder and dashboard share one set of data connections. The recorder samples right after each trading tick, inside the idle time, so it never delays an exit.
- `run` is the same engine without the web server. Use it on a server you only SSH into.
- A lock file stops a second trading process from starting on the same `state/`. That would double every order. A running dashboard therefore also blocks `close-all`; use its **Close all** button instead.

Other commands:

```bash
memecoin-agent -c config.yaml run --record   # same session, headless
memecoin-agent -c config.yaml status         # equity, PnL, open positions (read-only, safe anytime)
memecoin-agent -c config.yaml scan --safety  # what would it buy right now? (no trading)
memecoin-agent -c config.yaml backtest       # replay recordings (also in the dashboard)
memecoin-agent -c config.yaml record         # record only, without trading
memecoin-agent demo                          # synthetic market, text summary
```

State is written to `state/state.json` and every fill is appended to
`state/trades.csv`. Restarting picks up where the agent left off, including the
launch-feed buffer (`state/feed.json`) and the equity chart (`state/equity.csv`).

The `demo` market is synthetic and favourable by construction. It checks that
the plumbing and risk rules work. Its PnL tells you **nothing** about real
performance.

## Dashboard

`memecoin-agent -c config.yaml ui` runs the same trading loop as `run`, plus a
local web dashboard at <http://127.0.0.1:8787>. It opens in your browser
automatically; `--no-browser` and `--port` change that.

![Dashboard overview](docs/dashboard.png)

- **Overview**:
  - equity, today's PnL, realized and unrealized PnL, cash and session win rate;
  - an equity chart (1H / 24H / 7D / All) with a hover readout and a table view;
  - open positions, with each one's current exit price, take-profit progress and liquidity change since entry, and a **Close** button;
  - how close you are to each risk limit;
  - recent trades.
- **Scanner**: every token from the last scan, with its score, key metrics and verdict. The verdict is *Bought*, *Passed filters*, *Unsafe: …* (the RugCheck reason) or the exact filters it failed. This is the place to tune your filters.
- **Trades**: the trade journal, with transaction links in live mode and a CSV download.
- **Activity**: the agent's log, live, filterable to trades or to warnings.
- **Backtest**: pick a recording, enter `section.key=value` overrides, and get the report, equity curve, exit breakdown and every round trip. It runs on a copy of the config and never touches the live agent.
- **Settings**: the active config, read-only, with API keys masked.

Header controls:

- **Pause entries** stops new buys; exits keep running.
- **Close all** sells everything, after a confirmation.
- **Scan now** runs a scan immediately.

The dashboard reflects the live process, so stopping `ui` stops trading. Equity
history is saved to `state/equity.csv`, so the chart survives restarts.

**Security.** The dashboard can sell your positions, so:

- It listens on 127.0.0.1 only.
- Every API call needs a random per-session token that only the served page holds. Other websites can't trigger actions or read your data.
- It rejects requests addressed to any other hostname (blocking DNS-rebinding attacks).
- Other sites can't embed it in a frame (blocking clickjacking).

To reach it from another device, set `MEMECOIN_UI_PASSWORD` and pass `--host 0.0.0.0`. It refuses to bind publicly without a password. Put it behind HTTPS (for example, an SSH tunnel or a reverse proxy) before exposing it beyond your LAN.

## Going live

1. `pip install -e '.[live]'` (adds `solders` for transaction signing).
2. Create a fresh wallet just for the bot, fund it with SOL, and export its key:
   - `SOLANA_PRIVATE_KEY`: a base58 string (Phantom export) or a JSON byte array, **or**
   - `SOLANA_KEYPAIR_PATH`: path to a `solana-keygen` JSON file.
3. In `config.yaml`:
   - set `execution.mode: live`;
   - set `execution.rpc_url` to a private RPC (Helius, Triton, QuickNode…). The public endpoint is rate-limited and slow.
4. `MEMECOIN_AGENT_CONFIRM_LIVE=yes memecoin-agent -c config.yaml run`

In live mode:

- Buys spend SOL. Cash is read from the wallet balance every tick, and `min_sol_reserve` (0.05 SOL) is always left for fees.
- Each swap is quoted by Jupiter, signed locally (the key never leaves the machine) and sent through your RPC.
- Fills are measured from on-chain balance changes, not from the quote.
- `memecoin-agent -c config.yaml close-all` sells everything immediately.

Known live-mode limitations:

- If a buy times out and lands later, the tokens sit in the wallet without being tracked. Check the wallet if you see `did not change ... balance` errors.
- The `status` command's "return vs start" figure compares against `starting_balance_usd`, not your real deposit.
- The launch feed only sees graduations that happen while the agent (or recorder) is running. Downtime leaves gaps, which DexScreener's feeds partly cover.

## Backtesting

Backtests replay real recorded market data through the exact production code
(agent, strategy, risk rules and paper fill model). So first, record. The easiest
way is to record while you paper trade: pass `--record`, set
`recorder.record_while_trading: true`, or click **Start recording** on the
dashboard's Backtest tab. To record without trading, use the standalone recorder:

```bash
memecoin-agent -c config.yaml record              # runs until Ctrl-C; --hours N to stop
```

The recorder polls discovery (DexScreener plus the launch feed) every 30 seconds.
It samples every discovered token for 12 hours, so a backtest sees what happened
after an entry, including for tokens your current filters would skip. It also
stores each token's RugCheck verdict. Output goes to gzipped hourly files in
`recordings/`. Run it on an always-on machine for a few days: hours of data are
noise.

Then replay it, changing parameters freely:

```bash
memecoin-agent -c config.yaml backtest
memecoin-agent -c config.yaml backtest --set filters.min_score=0.65 --set exits.stop_loss_pct=15
memecoin-agent backtest recordings/ --trades-out bt_trades.csv
```

```
Backtest 2026-01-01 00:00 -> 2026-01-01 23:59 UTC (24.0h, 283,215 records)
  Return          +93.50%   ($1,000.00 -> $1,934.99)
  Max drawdown    16.43%
  Round trips     68  (open at end: 0)
  Win rate        60.3%   avg win +55.9%   avg loss -32.1%
  Profit factor   2.58
  ...
  Exits           trailing stop: 31, stop loss: 22, time stop: 7, end of backtest: 5, liquidity pulled: 3
```

(That output is from the *synthetic* market, `record --synthetic`. Synthetic
sessions always write to `recordings-synthetic/`, so they never mix with real data. Use it to try the
workflow offline; its numbers mean nothing.)

**How faithful the replay is.** At simulated time T the agent sees only data
recorded at or before T:

- Discovery returns what discovery returned at the last recorded poll.
- A token that stopped being sampled looks unavailable, as it would live.
- Tokens with no recorded RugCheck verdict are rejected, as live (fail closed). `--assume-safe` overrides this.
- Open positions are sold at the last price when the replay ends. `--keep-open` leaves them open instead.

**What a backtest can't capture:**

- Price moves between 30-second samples. Stops fill at the next sample, which flatters them on fast dumps and rugs.
- Real slippage, MEV sandwiches and failed transactions.
- Your own buys moving the price. The fill model includes x*y=k price impact, but not other traders reacting.
- Tokens discovered 12h+ before they'd have been bought (outside `track_hours`).

Treat backtest results as an upper bound. Be wary of tuning parameters until one
recording looks great: hold some days back and check the result holds on them.

## Layout

```
memecoin_agent/
  agent.py           trading loop
  strategy.py        entry filters, momentum score, exit rules
  risk.py            sizing, exposure caps, daily loss halt, cooldowns
  portfolio.py       ledger, JSON state, CSV journal
  data/dexscreener.py, data/rugcheck.py
  data/pumpportal.py real-time pump.fun graduation/launch websocket feed
  execution/paper.py     simulated fills (fees + x*y=k price impact)
  execution/jupiter.py   live swaps; execution/solana_rpc.py
  recording.py       market-data recorder (gzipped JSONL)
  backtest.py        point-in-time replay + performance report
  simulation.py      offline synthetic market for demo/tests
  ui/runner.py       the session engine for run + ui: agent loop, recorder, controls, views
  statelock.py       one trader per state/ directory
  ui/server.py       stdlib HTTP server + auth; ui/static/ is the page (vanilla JS, no build)
tests/               pytest suite (HTTP and RPC mocked; real tx signing)
```

Run the tests with `pytest`.
