from __future__ import annotations

import os

import pytest

from memecoin_agent import statelock


def test_windows_lock_blocks_second_runner_and_releases(tmp_path, monkeypatch):
    """Exercise the Windows branch without requiring a Windows test runner."""
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.held = set()

        def locking(self, fd, operation, length):
            assert length == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0
            key = os.fstat(fd).st_ino
            if operation == self.LK_NBLCK:
                if key in self.held:
                    raise OSError("already locked")
                self.held.add(key)
            else:
                assert operation == self.LK_UNLCK and key in self.held
                self.held.remove(key)

    fake = FakeMsvcrt()
    monkeypatch.setattr(statelock, "fcntl", None)
    monkeypatch.setattr(statelock, "msvcrt", fake)
    path = tmp_path / "state.json"
    first = statelock.StateLock(path).acquire()
    try:
        with pytest.raises(statelock.StateLocked, match="already in use"):
            statelock.StateLock(path).acquire()
    finally:
        first.release()
    statelock.StateLock(path).acquire().release()
    assert not fake.held


def test_state_lock_fails_closed_without_supported_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(statelock, "fcntl", None)
    monkeypatch.setattr(statelock, "msvcrt", None)
    with pytest.raises(statelock.StateLocked, match="No file-locking backend"):
        statelock.StateLock(tmp_path / "state.json").acquire()
