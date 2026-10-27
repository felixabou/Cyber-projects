# Backlog

Planned work, not built yet.

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
