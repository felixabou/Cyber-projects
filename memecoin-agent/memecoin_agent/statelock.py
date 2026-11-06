"""Single-trader lock: two processes trading from the same state file would double every order."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - Unix uses fcntl above
    msvcrt = None  # type: ignore[assignment]


class StateLocked(RuntimeError):
    pass


class StateLock:
    """Advisory lock next to the state file, released automatically if the process dies."""

    def __init__(self, state_file: str | Path) -> None:
        self.path = Path(state_file).with_suffix(".lock")
        self._fh: IO[str] | None = None
        self._backend: str | None = None

    def acquire(self) -> "StateLock":
        if self._fh is not None:
            return self
        if fcntl is None and msvcrt is None:
            raise StateLocked("No file-locking backend is available; refusing to run two traders on one state file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        backend = "unix" if fcntl is not None else "windows"
        try:
            if backend == "unix":
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fh.seek(0)  # msvcrt.locking locks bytes from the current file position
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            try:
                fh.seek(0)
                holder = fh.read().strip() or "another process"
            except OSError:  # Windows may prohibit reading a byte locked by another process
                holder = "another process"
            finally:
                fh.close()
            raise StateLocked(
                f"{self.path.parent}/ is already in use by {holder}. Stop it first, or point this one at a "
                "different agent.state_file (two traders on one state would double every order)."
            ) from None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid {os.getpid()}")
            fh.flush()
        except BaseException:
            self._unlock(fh, backend)
            raise
        self._fh = fh
        self._backend = backend
        return self

    @staticmethod
    def _unlock(fh: IO[str], backend: str) -> None:
        try:
            if backend == "unix":
                fcntl.flock(fh, fcntl.LOCK_UN)
            else:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()

    def release(self) -> None:
        if self._fh is not None:
            fh, backend = self._fh, self._backend
            self._fh = None
            self._backend = None
            self._unlock(fh, backend)

