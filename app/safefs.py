"""Filesystem helpers for private state files and atomic replacement."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path, mode: int = 0o700) -> None:
    """Create a real directory and restrict it to the current user."""
    if path.is_symlink():
        raise OSError(f"refusing symlinked directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not path.is_dir():
        raise OSError(f"not a directory: {path}")
    try:
        path.chmod(mode)
    except OSError:
        if os.name != "nt":
            raise


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write bytes to a sibling temp file, fsync, then atomically replace path."""
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            path.chmod(mode)
        except OSError:
            if os.name != "nt":
                raise
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)
