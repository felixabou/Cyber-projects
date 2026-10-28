"""Telegram bot against a mock of the Bot API: access control, commands, alerts, files and failures."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, TelegramConfig
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import TradeRecord
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.simulation import SimClock
from memecoin_agent.telegram_bot import (
    TelegramBot,
    TelegramClient,
    TelegramError,
    allowed_chats,
    format_positions,
    format_trade,
    load_token,
    price,
    split_text,
)
from memecoin_agent.ui.runner import AgentRunner, EquityHistory

from .conftest import NOW, make_snap
from .test_agent import FakeMarket, FakeSafety
from .test_ui import _wait

TOKEN = "123456:SECRET-token"
OWNER, STRANGER = 111, 999


class FakeTelegram:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.pending: list[dict] = []
        self.next_id = 1
        self.fail: dict[str, tuple[int, str]] = {}  # method -> (error_code, description), once
        self.always_fail: dict[str, tuple[int, str]] = {}

    def message(self, chat: int, text: str, username: str = "someone") -> None:
        with self.lock:
            self.pending.append({"update_id": self.next_id, "message": {
                "message_id": self.next_id, "chat": {"id": chat, "type": "private"},
                "from": {"id": chat, "username": username}, "text": text}})
            self.next_id += 1

    def sent(self, chat: int | None = None) -> list[str]:
        with self.lock:
            return [p["text"] for m, p in self.calls if m == "sendMessage" and (chat is None or p["chat_id"] == chat)]

    def docs(self) -> list[dict]:
        with self.lock:
            return [p for m, p in self.calls if m == "sendDocument"]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        prefix = f"/bot{TOKEN}/"
        if not req.url.path.startswith(prefix):
            return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})
        method = req.url.path[len(prefix):]
        if req.headers.get("content-type", "").startswith("multipart/"):
            body = req.read()
            params = {"raw": body, "chat_id": OWNER if f'\r\n\r\n{OWNER}\r\n'.encode() in body else None}
        else:
            params = json.loads(req.content or b"{}")
        with self.lock:
            self.calls.append((method, params))
            err = self.fail.pop(method, None) or self.always_fail.get(method)
        if err:
            return httpx.Response(200 if err[0] == 409 else err[0],
                                  json={"ok": False, "error_code": err[0], "description": err[1],
                                        **({"parameters": {"retry_after": 0}} if err[0] == 429 else {})})
        if method == "getMe":
            return ok({"id": 1, "is_bot": True, "username": "agent_test_bot"})
        if method == "getUpdates":
            offset = params.get("offset", 0)
            with self.lock:
                self.pending = [u for u in self.pending if u["update_id"] >= offset]
                updates = list(self.pending)
            if not updates:
                time.sleep(0.02)  # stands in for the long poll
            return ok(updates)
        if method == "sendMessage":
            return ok({"message_id": 1})
        return ok(True)


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


class FakeRunner:
    def __init__(self, cfg, view=None):
        self.agent = Agent(cfg, FakeMarket([]), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                           clock=SimClock(NOW), persist=False)
        self.view = view or {}
        self.submitted: list[tuple[str, dict]] = []

    def submit(self, action, **kw):
        self.submitted.append((action, kw))
        return {"ok": True, "message": f"did {action}"}


def make_bot(fake, runner, cfg=None, chats=(OWNER,)):
    client = TelegramClient(TOKEN, client=httpx.Client(transport=httpx.MockTransport(fake)))
    return TelegramBot(cfg or TelegramConfig(), client, runner, set(chats), poll_timeout=0)


POSITIONS = [
    {"mint": "MINT_A", "symbol": "PEPE", "value_usd": 61.2, "pnl_pct": 22.4, "held_minutes": 95, "dust": False,
     "url": "https://dexscreener.com/solana/pairA", "larp": {"verdict": "LIKELY REAL", "coin_stamp": "OK"}},
    {"mint": "MINT_B", "symbol": "DUST", "value_usd": 3.1, "pnl_pct": -40.0, "held_minutes": 300, "dust": True,
     "url": None, "larp": None},
]


def test_strangers_only_learn_their_chat_id_and_controls_need_an_allowed_chat():
    fake = FakeTelegram()
    runner = FakeRunner(Config())
    bot = make_bot(fake, runner)
    assert bot.start()
    try:
        fake.message(STRANGER, "/closeall yes", username="mallory")
        fake.message(STRANGER, "/pause")
        fake.message(OWNER, "/pause")
        _wait(lambda: fake.sent(OWNER)[1:])  # the start message, then the reply
    finally:
        bot.stop(timeout=2)
    to_stranger = fake.sent(STRANGER)
    assert len(to_stranger) == 1 and f"Your chat id is {STRANGER}" in to_stranger[0]
    assert runner.submitted == [("pause", {"source": "Telegram"})]  # only the owner's command ran
    assert fake.sent(OWNER)[0].startswith("Agent started (PAPER)")
    assert "Paused" in fake.sent(OWNER)[1] and fake.sent(OWNER)[-1].startswith("Agent stopped")
    methods = [m for m, _ in fake.calls]
    assert methods[:3] == ["getMe", "deleteWebhook", "setMyCommands"]
    assert fake.calls[1][1] == {"drop_pending_updates": True}  # stale commands don't fire on restart


def test_commands():
    view = {
        "mode": "paper", "equity": 1050.0, "baseline_equity": 1000.0, "cash": 985.7, "positions_value": 64.3,
        "today_pnl": 12.5, "realized_pnl": 30.0, "positions": POSITIONS, "entries_paused": False,
        "kill_switch": False, "entry_block": None, "tick_errors": 0, "last_tick_at": None,
        "risk": {"open_positions": 1, "max_open_positions": 5},
        "session": {"round_trips": 2, "wins": 1, "win_rate_pct": 50.0, "pnl_usd": 4.0},
        "larpcheck": {"on_unchecked": "check", "checks_left_today": 3},
        "trades": [{"time": "2026-10-22T13:05:00+00:00", "side": "sell", "symbol": "PEPE", "usd_value": 20.0,
                    "realized_pnl_usd": 5.0, "reason": "take profit +50%"}],
        "scan": {"candidates": [{"symbol": "NEW", "passed": True, "outcome": "bought"}, {"symbol": "X", "passed": False}]},
    }
    runner = FakeRunner(Config(), view)
    bot = make_bot(FakeTelegram(), runner)
    status = bot.respond(OWNER, "/status")
    assert "PAPER trading · equity $1,050.00 (+5.0% since start)" in status
    assert "Positions 1/5 (+1 dust)" in status and "New entries: open" in status and "3 checks left" in status
    pos = bot.respond(OWNER, "/positions@agent_test_bot")
    assert "PEPE  $61.20  +22.4%  held 1h35m" in pos and "LarpCheck LIKELY REAL, coin OK" in pos and "· dust" in pos
    assert "Oct 22 13:05 SELL PEPE $20.00 · +$5.00" in bot.respond(OWNER, "/trades")
    assert bot.respond(OWNER, "/scan") == "did scan_now: 1 passed the filters, bought NEW."
    assert "Send /closeall yes" in bot.respond(OWNER, "/closeall") and runner.submitted[-1][0] == "scan_now"
    assert bot.respond(OWNER, "/closeall yes") == "did close_all"
    assert bot.respond(OWNER, "/close $pepe") == "did close" and runner.submitted[-1] == (
        "close", {"source": "Telegram", "mint": "MINT_A"})
    assert "No open position matching NOPE" in bot.respond(OWNER, "/close NOPE")
    assert "Open: PEPE, DUST" in bot.respond(OWNER, "/close")
    assert "/status" in bot.respond(OWNER, "hello") and "Unknown command /foo" in bot.respond(OWNER, "/foo")
    quiet = make_bot(FakeTelegram(), runner, cfg=TelegramConfig(allow_controls=False))
    assert "Controls are off" in quiet.respond(OWNER, "/closeall yes") and "/pause" not in quiet.respond(OWNER, "/help")


def test_alerts_and_csv_downloads(tmp_path):
    fake = FakeTelegram()
    cfg = Config()
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    runner = FakeRunner(cfg)
    runner.started_at = NOW - timedelta(hours=2)  # this session
    bot = make_bot(fake, runner)
    t0 = NOW - timedelta(days=3)  # before this session and the last 6 hours
    buy = TradeRecord(t0, "buy", "MINT_A", "PEPE\n/evil", 50.0, 50.0, 0.0000123456, 0.25, "entry score 0.71")
    sell = TradeRecord(t0, "sell", "MINT_A", "PEPE", 20.0, 61.0, 0.000015, 0.3, "LarpCheck: LIKELY LARP", 11.0)
    recent = TradeRecord(NOW - timedelta(minutes=30), "buy", "MINT_B", "BONK", 10.0, 40.0, 0.004, 0.2, "copy")
    assert bot.respond(OWNER, "/csv") == "No trades yet."
    Portfolio(cash_usd=0, trades=[buy, sell, recent]).flush_trades(cfg.agent.trades_csv)
    with open(cfg.agent.trades_csv, "a") as fh:
        fh.write("2026-10-22T13:06:00+00:00,buy,PARTIAL")  # a row the agent is still writing
    assert bot.respond(OWNER, "/csv") is None  # all
    assert bot.respond(OWNER, "/csv session") is None
    assert bot.respond(OWNER, "/csv 6") is None
    assert bot.respond(OWNER, "/csv 48").startswith("Send /csv session, /csv all, or /csv HOURS")
    assert bot.start()
    try:
        bot.notify([buy, sell])
        _wait(lambda: len(fake.docs()) == 3 and len(fake.sent(OWNER)) >= 2)
    finally:
        bot.stop(timeout=2)
    alert = fake.sent(OWNER)[1]
    assert alert.startswith("🟢 BUY PEPE /evil $50.00 at $0.00001235 (paper)")
    assert "https://dexscreener.com/solana/MINT_A" in alert and "🔴 SELL PEPE $61.00, realized +$11.00" in alert
    every, session, hours = fake.docs()
    assert b'filename="trades_all_to_' in every["raw"] and b"PARTIAL" not in every["raw"]
    assert b"All trades: 3 (2 buys, 1 sells), realized +$11.00" in every["raw"]
    assert b'filename="trades_session_' in session["raw"] and b"This session's trades" in session["raw"]
    assert b"MINT_B" in session["raw"] and b"MINT_A" not in session["raw"]
    assert b"Trades in the last 6 hours: 1" in hours["raw"]


def test_rejected_token_turns_the_bot_off():
    fake = FakeTelegram()
    fake.always_fail["getMe"] = (401, "Unauthorized")
    bot = make_bot(fake, FakeRunner(Config()))
    assert not bot.start() and bot.status()["state"] == "token rejected" and not bot._threads
    bot.notify([TradeRecord(NOW, "buy", "M", "S", 1, 1, 1, 0, "r")])
    assert bot.outbox.empty()
    bot.stop(timeout=1)


def test_conflict_and_flaky_network_keep_polling():
    fake = FakeTelegram()
    fake.fail["getUpdates"] = (409, "Conflict: terminated by other getUpdates request")
    runner = FakeRunner(Config())
    bot = make_bot(fake, runner)
    bot._stop.wait = lambda timeout: bot._stop.is_set()  # don't sit out the back-off in a test
    assert bot.start()
    try:
        _wait(lambda: sum(1 for m, _ in fake.calls if m == "getUpdates") >= 3)
        fake.message(OWNER, "/resume")
        _wait(lambda: runner.submitted)
    finally:
        bot.stop(timeout=2)
    assert bot.status() == {"state": "ok", "bot": "@agent_test_bot", "chats": 1}


def test_errors_never_carry_the_token():
    def boom(req):
        raise httpx.ConnectError(f"connection refused for {req.url}")

    client = TelegramClient(TOKEN, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as exc:
        client.call("getMe")
    assert TOKEN not in str(exc.value) and "<bot-token>" in str(exc.value)


def test_token_and_chat_loading(monkeypatch, tmp_path):
    cfg = TelegramConfig(token_file=str(tmp_path / "token"), allowed_chat_ids=[5])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert load_token(cfg) is None and allowed_chats(cfg) == {5}
    (tmp_path / "token").write_text(" from-file \n")
    assert load_token(cfg) == "from-file"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, -100222,oops")
    assert load_token(cfg) == "from-env" and allowed_chats(cfg) == {5, 111, -100222}

    from memecoin_agent import cli

    full = Config()
    full.telegram = cfg
    assert isinstance(cli.build_telegram(full, FakeRunner(full)), TelegramBot)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    (tmp_path / "token").unlink()
    assert cli.build_telegram(full, FakeRunner(full)) is None


def test_twenty_positions_split_across_messages_instead_of_being_cut():
    positions = [{**POSITIONS[0], "mint": f"MINT_{i}", "symbol": f"LONGNAMEDTOKEN{i:02d}" * 2,
                  "url": f"https://dexscreener.com/solana/{'P' * 44}{i}"} for i in range(20)]
    text = format_positions({"positions": positions})
    parts = split_text(text, 1500)
    assert len(parts) > 1 and all(len(p) <= 1500 for p in parts)
    assert "\n\n".join(parts) == text  # nothing lost, nothing cut mid-position
    assert split_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5] and split_text("") == []
    bot = make_bot(FakeTelegram(), FakeRunner(Config()))
    bot.send(OWNER, text + "\n\n" + "y" * 5000)
    assert bot.outbox.qsize() >= 3


def test_price_and_trade_formatting():
    assert price(1.5) == "$1.500" and price(0.0004567) == "$0.0004567" and price(1234.5) == "$1,234.50"
    t = TradeRecord(NOW, "sell", "M", "S", 1, 9.5, 1, 0, "stop loss -25%", -3.25)
    assert format_trade(t, "live") == "🔴 SELL S $9.50, realized -$3.25\nstop loss -25%"


def test_end_to_end_with_the_real_runner(tmp_path):
    """A buy on a real AgentRunner tick reaches the chat, and /pause from the chat reaches the agent."""
    cfg = Config()
    cfg.agent.tick_seconds = 0.02
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    clock = SimClock(NOW)
    agent = Agent(cfg, FakeMarket([make_snap(mint="GOOD")]), FakeSafety(), PaperExecutor(cfg.execution),
                  Portfolio(cash_usd=1000), clock=clock, persist=False)
    runner = AgentRunner(agent, equity=EquityHistory(), advance=lambda s: clock.advance(15))
    fake = FakeTelegram()
    bot = make_bot(fake, runner)
    runner.listeners.append(bot)
    runner.start()
    assert bot.start()
    try:
        _wait(lambda: any("BUY DOGE2" in m for m in fake.sent(OWNER)))
        fake.message(OWNER, "/pause")
        _wait(lambda: agent.entries_paused)
        _wait(lambda: any(m.startswith("Paused") for m in fake.sent(OWNER)))
    finally:
        runner.stop()
        bot.stop(timeout=2)
    assert Path(cfg.agent.state_file).exists() is False  # persist=False: nothing written by this test
