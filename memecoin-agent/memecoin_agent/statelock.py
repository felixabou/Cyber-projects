"""Single-trader lock: two processes trading from the same state file would double every order."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no advisory locks, run one trader at a time
    fcntl = None  # type: ignore[assignment]


class StateLocked(RuntimeError):
    pass


class StateLock:
    """Advisory lock next to the state file, released automatically if the process dies."""

    def __init__(self, state_file: str | Path) -> None:
        self.path = Path(state_file).with_suffix(".lock")
        self._fh: IO[str] | None = None

    def acquire(self) -> "StateLock":
        if fcntl is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "another process"
            fh.close()
            raise StateLocked(
                f"{self.path.parent}/ is already in use by {holder}. Stop it first, or point this one at a "
                "different agent.state_file (two traders on one state would double every order)."
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()}")
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)  # type: ignore[union-attr]
            self._fh.close()
            self._fh = None
