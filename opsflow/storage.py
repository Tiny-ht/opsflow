"""Single-host mutual exclusion, atomic persistence, and stable record IDs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


def now_iso(tz: tzinfo | None = None) -> str:
    if tz is not None:
        return datetime.now(tz).isoformat(timespec="seconds")
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}_{uuid.uuid4().hex}" if prefix else uuid.uuid4().hex


def stable_id(value: object, prefix: str = "") -> str:
    """Return a deterministic SHA-256 ID independent of dict key order."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{prefix}_{digest}" if prefix else digest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Lock a fixed sidecar file so atomic target replacement never splits locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_text(path: Path, text: str) -> None:
    """Replace path atomically via a same-directory temp file and fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
            if hasattr(os, "fchmod"):
                mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
                os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: object) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_text(path, text)
