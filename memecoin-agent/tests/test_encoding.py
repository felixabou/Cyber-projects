"""Every file the agent reads or writes names its encoding: on Windows the default is cp1252 ("charmap"), which
can't hold a coin name like 牛, so one such coin used to break after-sale tracking and could break the trade log."""

from __future__ import annotations

import ast
from pathlib import Path

import memecoin_agent

FILE_CALLS = {"open", "read_text", "write_text"}
NOT_FILES = {"webbrowser", "pf"}  # webbrowser.open(url), portfolio.open(position)


def test_every_file_open_names_its_encoding():
    missing = []
    for path in Path(memecoin_agent.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FILE_CALLS):
                continue
            owner = node.func.value
            name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
            if name in NOT_FILES:
                continue
            mode = next((a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
            if "b" in mode or any(k.arg == "encoding" for k in node.keywords):
                continue
            missing.append(f"{path.name}:{node.lineno}")
    assert not missing, f"file opened without an encoding: {missing}"


def test_trade_log_and_after_sale_files_take_any_coin_name(tmp_path):
    from memecoin_agent.analytics import Analytics
    from memecoin_agent.models import Fill
    from memecoin_agent.portfolio import Portfolio, read_trades_csv

    from .conftest import NOW, make_snap

    snap = make_snap(symbol="牛市", name="牛市 Bull")
    pf = Portfolio(cash_usd=1_000)
    pf.open(snap, Fill(token_amount=1000, usd_value=10, price_usd=0.01), NOW, 0.5, reason="test")
    csv_path = tmp_path / "trades.csv"
    pf.flush_trades(csv_path)
    assert [t.symbol for t in read_trades_csv(csv_path)] == ["牛市"]
    pf.save(tmp_path / "state.json")
    assert Portfolio.load(tmp_path / "state.json", 1_000).positions[snap.mint].symbol == "牛市"
    a = Analytics(tmp_path)
    a.log_entry(snap, "lowcap", 0.5, 10, 0.01, NOW, None)
    assert "牛市" in (tmp_path / "entries.csv").read_text(encoding="utf-8")
