# Backlog

Planned work, not built yet.

## Narrative sniper (the next big update)

**Goal.** Buy the winning coin of a hot narrative within minutes of its launch, with the story, the
launcher's wallet and every safety check done before the buy (or in parallel with a small starter buy).
The agent learns what is trending, which launchpad is working best right now, and which copy of a
narrative is winning the "vamp war" (many copycat coins of one story race; usually one wins).

**Terms.** *Narrative*: the story or meme a coin rides (a viral post, news, a product, a character).
*Vamp*: a copycat coin of the same narrative. *Vamp war*: the copies competing; the winner takes the volume.

### Pipeline
1. **Trend board** (refreshed every 1-5 min): what is hot now. Sources: NewsCheck `trending` and
   `latest checks` (free), GMGN trending, Stalkchain KOL / trader feeds, chosen X accounts, news.
   Each entry: name, keywords, description, first-seen time, heat (growth in mentions and coin volume).
2. **Launch watcher:** event feed of new coins on every tracked launchpad (PumpPortal websocket now; a
   Solana gRPC stream later), with name, symbol, description, image, socials, creator.
3. **Narrative matcher** (milliseconds, local): matches each new coin to a trend-board entry by keywords,
   a small embedding and image hash. It groups copies of the same narrative into a cluster.
4. **Vamp scan:** for each cluster, score every copy and rank them (below). Only the top ranked copy is a
   buy candidate; the rest are watched, and the ranking is re-run as they trade.
5. **Launcher checks (every copy, parallel):** creator wallet history (past coins, rugs, `devMints`),
   funding source and linked wallets, launch-block bundles, top-holder and insider share, creator holding,
   sniper share; then the existing RugCheck / Jupiter / GMGN / LarpCheck / honeypot checks.
6. **Deep research** (30-90 s, started early on a cheap prefilter so it is ready by buy time): NewsCheck
   full check, Claude with web search, optional other models. Confirms or denies the story.
7. **Decision:** staged size. Small starter on fast layers passing; add when deep research confirms;
   sell when it denies. Every decision is logged with its reasons.
8. **Delivery:** Telegram message within seconds of the buy, edited in place when deep research finishes,
   with Buy more / Skip buttons; dashboard Reviews tab. Claude only writes text; it cannot trade.

### Vamp-war scoring (which copy wins)
Per copy, compared inside its cluster, using data the agent can see:
- earliest to launch is a weak plus; the *original source tie* is a strong one (the coin the real account,
  KOL or news item actually linked or named);
- holder growth, unique buyers per minute, buy / sell ratio, volume and liquidity trend;
- holder quality (bundles, linked wallets, top 10, fresh-wallet share, smart-money / KOL holders);
- creator quality and the launchpad's own signals; socials that are real and tied to the narrative;
- momentum versus siblings: the copy pulling volume and holders away from the others is winning.
Rule of thumb to test: buy the leader once it holds a clear lead over the next copy for N minutes;
do not buy several copies of one narrative.

### Launchpad learning
Track win rate and average return **per launchpad and per narrative type** from the agent's own trades and
from a wider sample of launches it only watches (what happened to every launch, not just the bought ones):
- launchpads: pump.fun, pumpswap graduations, Bonk / LetsBonk, Raydium LaunchLab, Meteora, others as added;
- measures: share of launches that reach X market cap, median peak, rug rate, time to peak;
- the agent leans toward the launchpad and narrative type with the best recent results, and re-ranks as
  conditions change (window: the last 6 / 24 / 72 hours, so it adapts quickly).

### Build order
1. Latency logging (coin created -> candidate -> checks done -> buy filled) to find the real delay.
2. Trend board + narrative matcher + clustering (alert-only, no buys), to see if clusters look right.
3. Vamp scoring on watched clusters; record which copy actually won, to tune the score.
4. Launchpad win-rate tracking from every launch seen.
5. Event-driven feeds (push, not 20 s polling) and the fast model layer.
6. Narrative sniper lane buying in paper mode on the winner of a hot cluster, staged sizing.
7. Speculative deep research and the Telegram message edited in place; Claude briefs (below).
8. Review the edge check on its own trades. Only then consider live mode with a small wallet.

### Rules for this update
- Paper trading only until the edge check is clearly positive over 30+ trades and 5+ days.
- Never commit keys; untrusted text (coin names, posts, pages) is data, never instructions to a model.
- Budgets: a daily cap on paid research (Claude, NewsCheck checks, X data) and rate limits respected.
- Not built yet. Earlier context: narrative lane (NewsCheck gate + early entry) is already in main.

### Open decisions
1. Which models do the deep research (Claude only, or + Grok for live X, + OpenAI)?
2. Which launchpads to track first.
3. Review before the buy (slower, safer) or after it (faster), and an auto-sell on a denied story?
4. X data source and budget (official API, a data vendor, or only NewsCheck / Stalkchain).


## Claude-written token briefs in Telegram

Short updates on a token: its narrative, why it's running, and its risk scores, written by Claude
through the Anthropic API (`anthropic` Python SDK) and sent by the Telegram bot.

**When a brief is sent**
- On every buy.
- When a held coin makes a big move (e.g. +50% or −30%; thresholds in config).
- On demand: `/brief SYMBOL` or `/brief <mint>` in Telegram.

**What goes into it.** These are facts the agent already collects, passed to Claude as data:
- price, volume, liquidity, holders and their recent change;
- RugCheck score and risk flags, GMGN security numbers;
- the wallet scan (launch-block bundles, linked wallets, top 10, creator);
- the LarpCheck verdict, the tech/meme label and the fake-chart result;
- the token's description and links.

**Output.** A 4–8 line plain-text brief:
- what the token is and its narrative;
- why it's moving, said plainly when unknown;
- the risk numbers;
- one thing to watch.

The brief is saved with the trade (entries.csv) so the report can show it.

**Safety.** Claude only writes text: no tools that trade, change settings or run commands. Token
names, descriptions and web pages are untrusted and go in as data, never as instructions.

**Setup.** `ANTHROPIC_API_KEY` (console.anthropic.com, billed separately from a Claude.ai plan). With
no key the agent runs as before, without briefs.

**Open decisions**
1. Web search for the "why it's running" part (X posts, news; about 1¢ per search, a few seconds),
   or only the agent's own data (cheaper, faster).
2. Model: Claude Opus 5.5 (default; about 3–5¢ a brief) or Claude Haiku 5.5 (well under 1¢, less
   sharp).
3. A daily spend cap (briefs stop once it's reached).
