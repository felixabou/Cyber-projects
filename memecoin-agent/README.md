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
   - Discover mints from DexScreener's latest-profiles and boost feeds, plus your `watchlist`.
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

memecoin-agent demo                  # offline synthetic market, no network needed
memecoin-agent scan --safety         # what would it buy right now? (no trading)
cp config.example.yaml config.yaml
memecoin-agent -c config.yaml run    # paper trading loop
memecoin-agent -c config.yaml status # equity, PnL, open positions
```

State is written to `state/state.json` and every fill is appended to
`state/trades.csv`. Restarting picks up where the agent left off.

The `demo` market is synthetic and favourable by construction. It checks that
the plumbing and risk rules work. Its PnL tells you **nothing** about real
performance.

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
- Discovery only sees tokens that bought a DexScreener profile or boost. Raw pump.fun launches won't appear unless you add a faster feed (Birdeye, Helius webhooks, a pump.fun stream) behind the same `MarketData` interface in `memecoin_agent/data/`.

## Layout

```
memecoin_agent/
  agent.py           trading loop
  strategy.py        entry filters, momentum score, exit rules
  risk.py            sizing, exposure caps, daily loss halt, cooldowns
  portfolio.py       ledger, JSON state, CSV journal
  data/dexscreener.py, data/rugcheck.py
  execution/paper.py     simulated fills (fees + x*y=k price impact)
  execution/jupiter.py   live swaps; execution/solana_rpc.py
  simulation.py      offline synthetic market for demo/tests
tests/               pytest suite (HTTP and RPC mocked; real tx signing)
```

Run the tests with `pytest`.
