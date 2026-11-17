"""Paths like a Windows user's (spaces, accents, non-Latin letters) and coin names in any script, end to end.

CI runs the whole suite on Windows too, where the default text encoding is cp1252 ("charmap"); these tests make sure
the files the agent writes and reads there hold any name."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from memecoin_agent.config import load_config
from memecoin_agent.data.larpcheck import load_api_key as larp_key
from memecoin_agent.data.memeos import load_api_key as memeos_key
from memecoin_agent.config import LarpCheckConfig, MemeosConfig
from memecoin_agent.scanlog import ScanLog, load_rows
from memecoin_agent.telemetry import Telemetry

from .conftest import NOW, make_snap

ODD = "Mes Documents/ab162 dossier spécial/牛市 bot"


def test_windows_style_paths_are_understood():
    p = PureWindowsPath(r"C:\Users\ab162\Cyber-projects\memecoin-agent\state\trades.csv")
    assert p.parts[-2:] == ("state", "trades.csv") and p.drive == "C:"
    assert PureWindowsPath(r"C:\Users\ab162") / "state" == PureWindowsPath(r"C:\Users\ab162\state")


def test_config_and_keys_load_from_folders_with_spaces_and_accents(tmp_path, monkeypatch):
    home = tmp_path / ODD
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("# réglages: paper only\nexecution:\n  mode: paper\n", encoding="utf-8")
    assert load_config(home / "config.yaml").execution.mode == "paper"
    # Notepad saves "UTF-8 with BOM": the mark must not end up glued to the key.
    (home / "larp_key").write_text("\ufefflc_secret\n", encoding="utf-8")
    (home / "memeos_key").write_text("\ufeffsk_memeos_x\n", encoding="utf-8")
    monkeypatch.delenv("LARPCHECK_API_KEY", raising=False)
    monkeypatch.delenv("MEMEOS_API_KEY", raising=False)
    assert larp_key(LarpCheckConfig(api_key_file=str(home / "larp_key"))) == "lc_secret"
    assert memeos_key(MemeosConfig(api_key_file=str(home / "memeos_key"))) == "sk_memeos_x"


def test_state_files_in_an_odd_folder_take_non_latin_coin_names(tmp_path):
    state = tmp_path / ODD / "state"
    tel = Telemetry(state)
    tel.begin_scan(1, 0)
    tel.block("filters", "mcap_too_low", mint="M牛", symbol="牛市 ÉLAN 🚀")
    tel.end_scan()
    assert Telemetry(state).rows()[0]["symbol"] == "牛市 ÉLAN 🚀"

    class Ev:
        def __init__(self, s):
            self.snapshot, self.passed, self.reasons, self.lane, self.score = s, False, ["mcap too low"], "", 0.1

    log = ScanLog(str(state / "scans.csv"), clock=lambda: NOW.timestamp())
    snap = make_snap(mint="M牛", symbol="牛市", name="牛市 Ünïcode")
    log.observe(NOW.timestamp(), [Ev(snap)])
    log._write(log.pending.pop("M牛"))
    assert load_rows(state / "scans.csv")[0]["symbol"] == "牛市"
    assert Path(state / "scans.csv").read_bytes().decode("utf-8")  # written as UTF-8, not cp1252
