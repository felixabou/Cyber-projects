"""Telegram bot: trade alerts, the hourly trade CSVs and remote control, over the Bot API.

Long polling (getUpdates) works from anywhere, a laptop or a cloud container: no public URL and
no inbound port. Only chats in telegram.allowed_chat_ids / TELEGRAM_CHAT_ID can use it; anyone
else who messages the bot gets their own chat id back (to set up access) and nothing more.

Messages go out as plain text (no Markdown/HTML parsing): token names come from public APIs and
are untrusted. The bot token is part of every request URL, so errors are logged with it masked.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import queue
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import TelegramConfig
from .models import TradeRecord, utcnow

log = logging.getLogger(__name__)

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHATS = "TELEGRAM_CHAT_ID"
API = "https://api.telegram.org"
MAX_TEXT = 4000  # Telegram allows 4096 characters per message
MAX_CAPTION = 1000  # ...and 1024 per document caption

COMMANDS = [
    ("status", "Equity, PnL and what the agent is doing"),
    ("positions", "Open positions"),
    ("trades", "The last 10 trades"),
    ("csv", "Download the full trade history (CSV)"),
    ("pause", "Stop opening new positions (exits keep running)"),
    ("resume", "Allow new positions again"),
    ("scan", "Scan for entries now"),
    ("close", "Sell one position: /close SYMBOL"),
    ("closeall", "Sell every position: /closeall yes"),
    ("buy", "Paper-buy any token: /buy ADDRESS USD [chain] [manual]"),
    ("sell", "Sell part of a position: /sell SYMBOL 50"),
    ("agent", "Who trades a position: /agent SYMBOL on|off"),
    ("help", "List the commands"),
]
CONTROLS = {"pause", "resume", "scan", "close", "closeall", "buy", "sell", "agent"}
CHAIN_WORDS = {"sol": "solana", "solana": "solana", "eth": "ethereum", "ethereum": "ethereum", "bsc": "bsc",
               "bnb": "bsc", "base": "base", "arb": "arbitrum", "arbitrum": "arbitrum", "robinhood": "robinhood"}


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


def load_token(cfg: TelegramConfig) -> str | None:
    token = os.environ.get(ENV_TOKEN, "").strip()
    if token:
        return token
    if cfg.token_file:
        path = Path(cfg.token_file).expanduser()
        if path.exists():
            return path.read_text().strip() or None
    return None


def allowed_chats(cfg: TelegramConfig) -> set[int]:
    chats = {int(c) for c in cfg.allowed_chat_ids}
    for part in os.environ.get(ENV_CHATS, "").replace(";", ",").split(","):
        if part.strip():
            try:
                chats.add(int(part.strip()))
            except ValueError:
                log.warning("Ignoring %s entry %r: not a chat id", ENV_CHATS, part.strip())
    return chats


# -- formatting ------------------------------------------------------------------------
def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Telegram-sized parts of a long message, split at blank lines (between positions or trades)."""
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        joined = f"{current}\n\n{block}" if current else block
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            parts.append(current)
        while len(block) > limit:  # one block too long on its own: cut it
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


def one_line(s: Any) -> str:
    return " ".join(str(s or "?").split())  # untrusted names: no line breaks or padding tricks


def usd(x: float, sign: bool = False) -> str:
    prefix = "-" if x < 0 else ("+" if sign else "")
    return f"{prefix}${abs(x):,.2f}"


def pct(x: float) -> str:
    return f"{x:+.1f}%"


def price(p: float) -> str:
    if p <= 0:
        return "$0"
    decimals = min(12, max(2, 3 - math.floor(math.log10(p))))  # ~4 significant digits on tiny prices
    return f"${p:,.{decimals}f}"


def duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 48 * 60:
        return f"{minutes // 60:.0f}h{minutes % 60:02.0f}m"
    return f"{minutes / 1440:.1f}d"


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    secs = (utcnow() - datetime.fromisoformat(iso)).total_seconds()
    return f"{secs:.0f}s ago" if secs < 120 else f"{duration(secs / 60)} ago"


def format_trade(t: TradeRecord, mode: str) -> str:
    tag = " (paper)" if mode == "paper" else ""
    if t.side == "buy":
        return (f"🟢 BUY {one_line(t.symbol)} {usd(t.usd_value)} at {price(t.price_usd)}{tag}\n"
                f"{clip(t.reason, 300)}\nhttps://dexscreener.com/solana/{t.mint}")
    return (f"🔴 SELL {one_line(t.symbol)} {usd(t.usd_value)}, realized {usd(t.realized_pnl_usd, sign=True)}{tag}\n"
            f"{clip(t.reason, 700)}")


def format_status(v: dict[str, Any]) -> str:
    if not v:
        return "The agent is starting up."
    eq, base = v["equity"], v.get("baseline_equity") or v["equity"]
    r, s = v["risk"], v["session"]
    dust = sum(1 for p in v["positions"] if p.get("dust"))
    if v.get("kill_switch"):
        entries = "off (kill switch file present)"
    elif v.get("entries_paused"):
        entries = "paused"
    elif v.get("entry_block"):
        entries = f"blocked: {v['entry_block']}"
    else:
        entries = "open"
    lines = [
        f"{v['mode'].upper()} trading · equity {usd(eq)} ({pct((eq / base - 1) * 100 if base else 0)} since start)",
        f"Cash {usd(v['cash'])} · in positions {usd(v['positions_value'])}",
        f"Today {usd(v['today_pnl'], sign=True)} · realized all-time {usd(v['realized_pnl'], sign=True)}",
        f"Positions {r['open_positions']}/{r['max_open_positions']}" + (f" (+{dust} dust)" if dust else ""),
        f"New entries: {entries}",
    ]
    if s["round_trips"]:
        lines.append(f"Closed this session: {s['round_trips']} trades, {s['win_rate_pct']:.0f}% won, "
                     f"{usd(s['pnl_usd'], sign=True)}")
    larp = v.get("larpcheck")
    if larp and larp.get("on_unchecked") == "check":
        lines.append(f"LarpCheck: {larp['checks_left_today']} checks left today")
    lines.append(f"Last tick {ago(v.get('last_tick_at'))}" + (f" · {v['tick_errors']} tick errors" if v.get("tick_errors") else ""))
    return "\n".join(lines)


def format_positions(v: dict[str, Any]) -> str:
    positions = v.get("positions") or []
    if not positions:
        return "No open positions."
    total = sum(p["value_usd"] for p in positions)
    blocks = [f"{len(positions)} open position(s), worth {usd(total)}"]
    for p in positions:
        tags = [p["chain"]] if p.get("chain") not in (None, "solana") else []
        tags += [x for x in (p.get("lane") if p.get("lane") in ("copy", "longterm", "manual") else None,
                             "dust" if p.get("dust") else None,
                             p.get("stage") if p.get("stage") in ("runner", "dip watch") else None,
                             "you trade it" if p.get("managed") is False else None) if x]
        line = (f"{one_line(p['symbol'])}  {usd(p['value_usd'])}  {pct(p['pnl_pct'])}  held {duration(p['held_minutes'])}"
                + (f"  · {', '.join(tags)}" if tags else ""))
        if p.get("review_note"):
            line += f"\n{clip(one_line(p['review_note']), 160)}"
        larp = p.get("larp") or {}
        if larp.get("verdict"):
            line += f"\nLarpCheck {larp['verdict']}" + (f", coin {larp['coin_stamp']}" if larp.get("coin_stamp") else "")
        if p.get("url"):
            line += f"\n{p['url']}"
        blocks.append(line)
    return "\n\n".join(blocks)


def format_trades(v: dict[str, Any], n: int = 10) -> str:
    trades = (v.get("trades") or [])[:n]  # newest first
    if not trades:
        return "No trades yet."
    lines = [f"Last {len(trades)} trade(s), newest first (UTC):"]
    for t in trades:
        when = datetime.fromisoformat(t["time"]).strftime("%b %d %H:%M") if t.get("time") else "?"
        pnl = f" · {usd(t['realized_pnl_usd'], sign=True)}" if t["side"] == "sell" else ""
        lines.append(f"{when} {t['side'].upper()} {one_line(t['symbol'])} {usd(t['usd_value'])}{pnl}\n"
                     f"   {clip(one_line(t['reason']), 140)}")
    return "\n".join(lines)


def help_text(controls: bool) -> str:
    rows = [f"/{c} - {d}" for c, d in COMMANDS if controls or c not in CONTROLS]
    return "\n".join(["memecoin-agent commands:", *rows,
                      "", "Trades are announced here as they happen, and each hour's trades arrive as a CSV."])


# -- Bot API client ----------------------------------------------------------------------
class TelegramClient:
    def __init__(self, token: str, api: str = API, client: httpx.Client | None = None) -> None:
        self.token = token
        self.base = f"{api.rstrip('/')}/bot{token}"
        # getUpdates holds the request open for its long-poll timeout, so reads may take a while.
        self.http = client or httpx.Client(timeout=httpx.Timeout(20.0, read=60.0))

    def redact(self, text: str) -> str:
        return text.replace(self.token, "<bot-token>")

    def call(self, method: str, params: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}/{method}"
        try:
            resp = (self.http.post(url, data=params or {}, files=files) if files
                    else self.http.post(url, json=params or {}))
        except httpx.HTTPError as exc:
            raise TelegramError(self.redact(f"{method}: {type(exc).__name__}: {exc}")) from None
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}, non-JSON response", code=resp.status_code) from None
        if not isinstance(body, dict) or not body.get("ok"):
            body = body if isinstance(body, dict) else {}
            code = body.get("error_code") or resp.status_code
            raise TelegramError(self.redact(f"{method}: {code} {body.get('description', '')}".strip()), code=code,
                                retry_after=(body.get("parameters") or {}).get("retry_after"))
        return body.get("result")

    def send_message(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": clip(text, MAX_TEXT),
                                  "link_preview_options": {"is_disabled": True}})

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        self.call("sendDocument", {"chat_id": str(chat_id), "caption": clip(caption, MAX_CAPTION)},
                  files={"document": (filename, content, "text/csv")})


# -- the bot -------------------------------------------------------------------------------
class TelegramBot:
    """Polls for commands on one thread and sends from another; `notify` (called by the AgentRunner
    on the agent thread) only queues messages, so Telegram never slows the trading loop."""

    def __init__(self, cfg: TelegramConfig, client: TelegramClient, runner: Any, chats: set[int],
                 poll_timeout: int = 25) -> None:
        self.cfg = cfg
        self.client = client
        self.runner = runner
        self.chats = set(chats)
        self.poll_timeout = poll_timeout
        self.username: str | None = None
        self.state = "starting"
        self.outbox: queue.Queue[tuple[str, int, Any]] = queue.Queue(maxsize=500)
        self._ready = False
        self._offset: int | None = None
        self._told: set[int] = set()  # strangers already told their chat id
        self._warned: dict[str, float] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ------------------------------------------------------------------------
    def start(self) -> bool:
        """Check the token and start the bot threads. False (and no threads) if Telegram rejects the token."""
        try:
            self._setup()
        except TelegramError as exc:
            if self._fatal(exc):
                return False
            log.warning("Telegram unreachable at start (%s); the bot keeps retrying", exc)
        for target, name in ((self._poll_loop, "telegram-poll"), (self._send_loop, "telegram-send")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if not self.chats:
            log.warning("Telegram bot %s is on, but no chat is allowed to use it yet: send the bot a message and "
                        "it replies with your chat id; set %s to that id and restart",
                        self.username or "", ENV_CHATS)
        return True

    def _setup(self) -> None:
        me = self.client.call("getMe") or {}
        self.username = f"@{me['username']}" if me.get("username") else None
        # Drop commands sent while the agent was off: a stale /closeall must not fire on a restart.
        self.client.call("deleteWebhook", {"drop_pending_updates": True})
        try:
            self.client.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS
                                                            if self.cfg.allow_controls or c not in CONTROLS]})
        except TelegramError as exc:
            log.debug("setMyCommands failed: %s", exc)
        self._ready = True
        self.state = "ok"
        log.info("Telegram bot %s on: %d allowed chat(s)", self.username or "", len(self.chats))
        a = self.runner.agent
        self.broadcast(f"Agent started ({a.cfg.execution.mode.upper()}). Equity {usd(a.pf.equity_usd())}, "
                       f"{len(a.pf.positions)} open position(s). Send /help for commands.")

    def stop(self, timeout: float = 10.0) -> None:
        if self._threads and self.state != "token rejected":
            self.broadcast("Agent stopped: positions aren't managed until it starts again.")
        self._stop.set()
        deadline = time.monotonic() + timeout
        for t in self._threads:  # the sender flushes what's queued first; the poller is a daemon
            if t.name == "telegram-send":
                t.join(max(0.0, deadline - time.monotonic()))

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "bot": self.username, "chats": len(self.chats)}

    def _fatal(self, exc: TelegramError) -> bool:
        if exc.code in (401, 404):  # Unauthorized / Not Found: the token is wrong or revoked
            if self.state != "token rejected":
                log.error("Telegram rejected the bot token (%s): the bot is off; the agent keeps trading", exc)
            self.state = "token rejected"
            self._stop.set()
            return True
        return False

    def _warn(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._warned.get(key, -1e9) >= 600:  # at most every 10 minutes per kind
            self._warned[key] = now
            log.warning(msg, *args)

    # -- outgoing -------------------------------------------------------------------------
    def _queue(self, item: tuple[str, int, Any]) -> None:
        try:
            self.outbox.put_nowait(item)
        except queue.Full:
            self._warn("full", "Telegram outbox full; dropping messages")

    def send(self, chat: int, text: str) -> None:
        for part in split_text(text):
            self._queue(("text", chat, part))

    def broadcast(self, text: str) -> None:
        for chat in sorted(self.chats):
            self.send(chat, text)

    def broadcast_file(self, filename: str, content: bytes, caption: str) -> None:
        for chat in sorted(self.chats):
            self._queue(("doc", chat, (filename, content, caption)))

    def notify(self, trades: list[TradeRecord], exports: list[Any]) -> None:
        """New trades and hourly exports (AgentRunner listener; runs on the agent thread)."""
        if self.state == "token rejected" or not self.chats:
            return
        if trades and self.cfg.notify_trades:
            mode = self.runner.agent.cfg.execution.mode
            self.broadcast("\n\n".join(format_trade(t, mode) for t in trades))
        if self.cfg.send_exports:
            for e in exports:
                self.broadcast_file(e.path.name, e.path.read_bytes(), e.summary())

    def _send_loop(self) -> None:
        while not (self._stop.is_set() and self.outbox.empty()):
            try:
                item = self.outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            self._deliver(item)

    def _deliver(self, item: tuple[str, int, Any]) -> None:
        kind, chat, payload = item
        for attempt in range(3):
            try:
                if kind == "text":
                    self.client.send_message(chat, payload)
                else:
                    self.client.send_document(chat, *payload)
                return
            except TelegramError as exc:
                if self._fatal(exc):
                    return
                if exc.code in (400, 403):  # chat not found / the bot was blocked: retrying won't help
                    self._warn(f"chat{chat}", "Telegram: can't message chat %s: %s", chat, exc)
                    return
                if self._stop.is_set() or attempt == 2:
                    self._warn("send", "Telegram: dropped a message: %s", exc)
                    return
                time.sleep(min(float(exc.retry_after or 2 ** attempt), 30.0))

    # -- incoming -------------------------------------------------------------------------
    def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self._ready:
                    self._setup()
                params: dict[str, Any] = {"timeout": self.poll_timeout, "allowed_updates": ["message"]}
                if self._offset is not None:
                    params["offset"] = self._offset
                updates = self.client.call("getUpdates", params) or []
                self.state, backoff = "ok", 1.0
            except TelegramError as exc:
                if self._fatal(exc):
                    return
                if exc.code == 409:  # another getUpdates consumer: the same token used somewhere else
                    self.state = "conflict"
                    self._warn("conflict", "Telegram: another program is reading this bot's messages (is the agent "
                               "also running somewhere else with the same bot token?); retrying")
                    wait = 30.0
                else:
                    self.state = "unreachable"
                    self._warn("poll", "Telegram poll failed: %s", exc)
                    wait = float(exc.retry_after or backoff)
                    backoff = min(backoff * 2, 60.0)
                self._stop.wait(wait)
                continue
            for update in updates:
                self._offset = int(update["update_id"]) + 1
                try:
                    self._handle(update)
                except Exception:
                    log.exception("Telegram command failed")

    def _handle(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if chat is None:
            return
        if chat not in self.chats:
            if chat not in self._told and len(self._told) < 1000:
                self._told.add(chat)
                who = (msg.get("from") or {}).get("username")
                log.info("Telegram: message from chat %s%s, which isn't allowed to use the bot", chat,
                         f" (@{one_line(who)})" if who else "")
                self.send(chat, f"This bot runs a private trading agent and only answers its owner.\n"
                                f"Your chat id is {chat}. If this is your agent, set {ENV_CHATS}={chat} "
                                f"(or add it to telegram.allowed_chat_ids) and restart it.")
            return
        reply = self.respond(chat, str(msg.get("text") or ""))
        if reply:
            self.send(chat, reply)

    def respond(self, chat: int, text: str) -> str | None:
        text = text.strip()
        if not text.startswith("/"):
            return help_text(self.cfg.allow_controls)
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.split("@", 1)[0].lower(), arg.strip()
        view = self.runner.view
        if cmd in CONTROLS and not self.cfg.allow_controls:
            return "Controls are off for this bot (telegram.allow_controls: false)."
        if cmd in ("start", "help"):
            return help_text(self.cfg.allow_controls)
        if cmd == "status":
            return format_status(view)
        if cmd == "positions":
            return format_positions(view)
        if cmd == "trades":
            return format_trades(view)
        if cmd == "csv":
            return self._send_csv(chat)
        if cmd == "pause":
            return self._control("pause", "Paused: no new positions. Exits keep running.")
        if cmd == "resume":
            return self._control("resume", "Resumed: the agent can open positions again.")
        if cmd == "scan":
            reply = self._control("scan_now")
            cands = (self.runner.view.get("scan") or {}).get("candidates") or []
            bought = [one_line(c["symbol"]) for c in cands if c.get("outcome") == "bought"]
            return (f"{reply}: {sum(1 for c in cands if c.get('passed'))} passed the filters"
                    + (f", bought {', '.join(bought)}" if bought else ", nothing bought") + ".")
        if cmd == "close":
            return self._close(arg, view)
        if cmd == "buy":
            return self._buy(arg)
        if cmd == "sell":
            return self._sell(arg, view)
        if cmd == "agent":
            return self._agent(arg, view)
        if cmd == "closeall":
            n = len(view.get("positions") or [])
            if not n:
                return "No open positions."
            if arg.lower() not in ("yes", "confirm"):
                return f"This sells all {n} open position(s), dust included. Send /closeall yes to confirm."
            return self._control("close_all")
        return f"Unknown command /{one_line(cmd)}.\n\n" + help_text(self.cfg.allow_controls)

    def _control(self, action: str, done: str | None = None, **kw: Any) -> str:
        try:
            result = self.runner.submit(action, source="Telegram", **kw) or {}
        except (TimeoutError, FutureTimeout):  # distinct classes before Python 3.11
            return "The agent is busy; the request is queued and will still run."
        except Exception as exc:
            return f"Failed: {exc}"
        return done or result.get("message") or "Done."

    def _match(self, arg: str, view: dict[str, Any], usage: str) -> tuple[dict[str, Any] | None, str]:
        positions = view.get("positions") or []
        if not arg:
            names = ", ".join(one_line(p["symbol"]) for p in positions) or "none"
            return None, f"Which one? Send {usage} (or the token's mint address). Open: {names}"
        key = arg.lstrip("$")
        matches = ([p for p in positions if p["mint"].lower() == key.lower()]
                   or [p for p in positions if one_line(p["symbol"]).lower() == key.lower()])
        if not matches:
            return None, f"No open position matching {one_line(arg)}."
        if len(matches) > 1:
            return None, "More than one position has that symbol; use the mint:\n" + "\n".join(
                f"{one_line(p['symbol'])} {p['mint']}" for p in matches)
        return matches[0], ""

    def _close(self, arg: str, view: dict[str, Any]) -> str:
        pos, why = self._match(arg, view, "/close SYMBOL")
        return self._control("close", mint=pos["mint"]) if pos else why

    def _buy(self, arg: str) -> str:
        parts = arg.split()
        usage = ("Send /buy ADDRESS USD [chain] [manual], e.g. /buy 0xded8...79cc 50 robinhood\n"
                 "chain: sol (default for Solana addresses), eth, bsc, base, arbitrum, robinhood. "
                 "Add 'manual' if only you should sell it.")
        if len(parts) < 2:
            return usage
        address, amount, rest = parts[0], parts[1].lstrip("$"), [w.lower() for w in parts[2:]]
        try:
            usd_amount = float(amount)
        except ValueError:
            return usage
        chain = next((CHAIN_WORDS[w] for w in rest if w in CHAIN_WORDS), None)
        if chain is None:
            if address.lower().startswith("0x"):
                return "Which chain is that address on? Add it: eth, bsc, base, arbitrum or robinhood."
            chain = "solana"
        return self._control("manual_buy", chain=chain, address=address, usd=usd_amount, managed="manual" not in rest)

    def _sell(self, arg: str, view: dict[str, Any]) -> str:
        key, _, amount = arg.rpartition(" ")
        try:
            fraction = float(amount.rstrip("%")) / 100
        except ValueError:
            return "Send /sell SYMBOL PERCENT, e.g. /sell ROBINCAT 50"
        pos, why = self._match(key.strip(), view, "/sell SYMBOL PERCENT")
        return self._control("sell", mint=pos["mint"], fraction=fraction) if pos else why

    def _agent(self, arg: str, view: dict[str, Any]) -> str:
        key, _, switch = arg.rpartition(" ")
        if switch.lower() not in ("on", "off"):
            return "Send /agent SYMBOL on (the agent's exits apply) or /agent SYMBOL off (only you sell it)"
        pos, why = self._match(key.strip(), view, "/agent SYMBOL on|off")
        return self._control("set_managed", mint=pos["mint"], managed=switch.lower() == "on") if pos else why

    def _send_csv(self, chat: int) -> str | None:
        path = Path(self.runner.agent.cfg.agent.trades_csv)
        try:
            data = path.read_bytes()
        except OSError:
            return "No trades yet."
        data = data[: data.rfind(b"\n") + 1]  # the agent may be appending a row right now
        rows = sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8", "replace")))) - 1  # minus the header
        if rows < 1:
            return "No trades yet."
        self._queue(("doc", chat, ("trades.csv", data, f"Full trade history: {rows} trades")))
        return None
