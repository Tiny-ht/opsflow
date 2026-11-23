"""Rebuildable local SQLite search index; ledgers and files remain source of truth."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Iterator

from .config import OpsContext, SOURCE_ALIASES
from .storage import file_lock


SCHEMA_VERSION = 2
ID_FIELDS = (
    "id", "trace_id", "eval_id", "reference_id", "ref_id", "track_id", "study_id",
)
CARD_COLUMNS = (
    "key", "id", "source", "title", "summary", "time", "status", "track_id",
    "evidence_layer", "kind", "path", "line", "read_bytes",
)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _short(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(part for part in value if isinstance(part, str))
    return ""


def _first(record: dict, fields: tuple[str, ...]) -> str:
    for field in fields:
        text = _text(record.get(field))
        if text.strip():
            return text.strip()
    return ""


def _record_time(record: dict, default_tz: tzinfo) -> tuple[str | None, float | None]:
    for name in ("ts", "time", "at", "updated_at", "created_at", "date"):
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return value, parsed.timestamp()
    return None, None


def _metadata(record: dict, source: str, path: str, *, eval_layers: dict[str, str], default_tz: tzinfo) -> dict:
    # Prefer the ledger's own ID field before linked trace_id so eval IDs are never shadowed.
    if source == "trace":
        preferred: tuple[str, ...] = ("id", "trace_id")
    elif source == "research":
        preferred = ("id", "ref_id", "reference_id")
    elif source == "track":
        preferred = ("id", "track_id")
    elif source == "eval" or source in eval_layers:
        preferred = ("id", "eval_id", f"{source}_eval_id")
    else:
        preferred = ("id",)
    identity = _first(record, preferred + ID_FIELDS)
    track_id = _first(record, ("track_id",)) or None
    if source == "track":
        identity = identity or track_id or Path(path).parent.name
        track_id = track_id or identity or None
    if source == "project":
        identity = identity or "project"
    title = _first(record, ("title", "name", "experiment", "run_id")) or identity or Path(path).stem
    summary = _first(record, ("summary", "conclusion", "notes", "goal"))
    if not summary:
        parts = [
            f"{field}={_short(text, 60)}"
            for field in ("stage", "kind", "run_id", "status", "execution_mode")
            if (text := _text(record.get(field)))
        ]
        summary = "; ".join(parts) if parts else "No summary provided; inspect with 'show <key>'."
    time_value, sort_time = _record_time(record, default_tz)
    tags = record.get("tags")
    if tags is None and isinstance(record.get("extra"), dict):
        tags = record["extra"].get("tags")
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    norm_tags = [t.casefold() for t in tags if isinstance(t, str) and t] if isinstance(tags, list) else []
    evidence_layer = _first(record, ("evidence_layer",)).casefold() or None
    if not evidence_layer and source in eval_layers:
        evidence_layer = source.casefold()
    return {
        "id": identity or None,
        "title": _short(title, 100),
        "summary": _short(summary, 240),
        "time": time_value,
        "sort_time": sort_time,
        "status": _first(record, ("status",)) or None,
        "track_id": track_id,
        "evidence_layer": evidence_layer,
        "kind": _first(record, ("kind",)) or None,
        "tags_json": json.dumps(sorted(set(norm_tags)), ensure_ascii=False, separators=(",", ":")),
    }


def _source_files(ctx: OpsContext) -> tuple[dict[str, str], dict[str, Path]]:
    """Return relative-key -> source and relative-key -> absolute Path mappings."""
    ops_root = ctx.ops_dir.resolve()
    sources: dict[str, str] = {}
    paths: dict[str, Path] = {}

    def register(source: str, abs_path: Path) -> None:
        if not abs_path.is_file():
            return
        resolved = abs_path.resolve()
        try:
            rel = resolved.relative_to(ops_root).as_posix()
        except ValueError:
            rel = str(resolved)
        sources[rel] = source
        paths[rel] = resolved

    for source, path in ctx.all_ledger_paths().items():
        register(source, path)
    if ctx.tracks_dir.is_dir():
        for path in sorted(ctx.tracks_dir.glob("*/track.json")):
            register("track", path)
    if ctx.lessons_dir.is_dir():
        for path in sorted(ctx.lessons_dir.glob("**/*.md")):
            register("lesson", path)
    register("project", ctx.project_file)
    return sources, paths


def _signature(path: Path) -> str:
    stat = path.stat()
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])


def _read_snapshot(path: Path) -> tuple[bytes, str]:
    before = _signature(path)
    raw = path.read_bytes()
    after = _signature(path)
    if before != after:
        raise RuntimeError(f"source file changed while reading; retry: {path}")
    return raw, after


@contextmanager
def _index_lock(ctx: OpsContext) -> Iterator[Path]:
    directory = ctx.index_dir
    directory.mkdir(parents=True, exist_ok=True)
    index_path = directory / "ops.sqlite"
    with file_lock(directory / "index"):
        yield index_path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError("index schema changed; run 'opsflow reindex --force'")
    connection.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, source TEXT NOT NULL,
            signature TEXT NOT NULL, sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS records (
            key TEXT PRIMARY KEY, id TEXT, source TEXT NOT NULL,
            title TEXT NOT NULL, summary TEXT NOT NULL, time TEXT, sort_time REAL,
            status TEXT, track_id TEXT, evidence_layer TEXT, kind TEXT, tags_json TEXT NOT NULL,
            path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            line INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
            read_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, search_text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS records_id ON records(id);
        CREATE INDEX IF NOT EXISTS records_track ON records(track_id);
        CREATE INDEX IF NOT EXISTS records_time ON records(sort_time DESC, key);
        CREATE TABLE IF NOT EXISTS errors (
            path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            line INTEGER NOT NULL, message TEXT NOT NULL, read_bytes INTEGER NOT NULL
        );
        PRAGMA user_version=2;
        COMMIT;
    """)


def _chunks(raw: bytes, is_ledger: bool) -> Iterator[tuple[int, int, bytes]]:
    if is_ledger:
        offset = 0
        for line, chunk in enumerate(raw.splitlines(keepends=True), 1):
            if chunk.strip():
                yield line, offset, chunk
            offset += len(chunk)
    else:
        yield 1, 0, raw


def _markdown_record(text: str, path: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = next((line.lstrip("# ") for line in lines if line.startswith("#")), Path(path).stem)
    summary = next((line for line in lines if not line.startswith(("#", "```"))), "Read lesson body on demand.")
    return {"id": path, "title": title, "summary": summary, "text": text}


def _insert_source(
    connection: sqlite3.Connection,
    source: str,
    path: str,
    raw: bytes,
    *,
    is_ledger: bool,
    eval_layers: dict[str, str],
    default_tz: tzinfo,
) -> int:
    occurrences: Counter[str] = Counter()
    added = 0
    for line, offset, chunk in _chunks(raw, is_ledger):
        try:
            text = chunk.decode("utf-8")
            record = _markdown_record(text, path) if source == "lesson" else json.loads(text)
            if not isinstance(record, dict):
                raise ValueError("record must be a JSON object")
        except (UnicodeDecodeError, ValueError) as error:
            connection.execute(
                "INSERT INTO errors VALUES (?, ?, ?, ?)",
                (path, line, _short(str(error), 240), len(chunk)),
            )
            continue
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_key = _digest((path + "\0" + canonical).encode("utf-8"))[:24]
        occurrences[content_key] += 1
        row = _metadata(record, source, path, eval_layers=eval_layers, default_tz=default_tz)
        row.update({
            "key": f"{source}:{content_key}:{occurrences[content_key]}",
            "source": source,
            "path": path,
            "line": line,
            "byte_offset": offset,
            "read_bytes": len(chunk),
            "sha256": _digest(chunk),
            "search_text": canonical.casefold(),
        })
        columns = ",".join(row)
        parameters = ",".join("?" for _ in row)
        connection.execute(f"INSERT INTO records ({columns}) VALUES ({parameters})", tuple(row.values()))
        added += 1
    return added


def _errors(connection: sqlite3.Connection) -> dict:
    total = connection.execute("SELECT count(*) FROM errors").fetchone()[0]
    rows = connection.execute("SELECT * FROM errors ORDER BY path, line LIMIT 100").fetchall()
    return {
        "errors": [dict(row) for row in rows],
        "error_count": total,
        "omitted_error_count": max(0, total - len(rows)),
    }


def _refresh(ctx: OpsContext, connection: sqlite3.Connection, force: bool) -> dict:
    sources, paths = _source_files(ctx)
    ledger_sources = set(ctx.all_ledger_paths())
    eval_layers = ctx.eval_layers
    default_tz = ctx.default_timezone()
    previous = {row["path"]: dict(row) for row in connection.execute("SELECT * FROM files")}
    changed = skipped = added = 0
    removed = sorted(set(previous) - set(sources))
    connection.execute("BEGIN IMMEDIATE")
    try:
        for path in removed:
            connection.execute("DELETE FROM files WHERE path=?", (path,))
        for path, source in sorted(sources.items()):
            abs_path = paths[path]
            signature = _signature(abs_path)
            old = previous.get(path)
            if not force and old and old["signature"] == signature and old["source"] == source:
                skipped += 1
                continue
            raw, signature = _read_snapshot(abs_path)
            digest = _digest(raw)
            if not force and old and old["sha256"] == digest and old["source"] == source:
                connection.execute("UPDATE files SET signature=? WHERE path=?", (signature, path))
                skipped += 1
                continue
            connection.execute("DELETE FROM files WHERE path=?", (path,))
            connection.execute("INSERT INTO files VALUES (?, ?, ?, ?)", (path, source, signature, digest))
            added += _insert_source(
                connection, source, path, raw,
                is_ledger=source in ledger_sources,
                eval_layers=eval_layers,
                default_tz=default_tz,
            )
            changed += 1
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return {
        "files": len(sources), "updated_files": changed, "unchanged_files": skipped,
        "removed_files": len(removed), "indexed_records": added,
        "records": connection.execute("SELECT count(*) FROM records").fetchone()[0],
        **_errors(connection),
    }


def reindex(ctx: OpsContext, force: bool = False) -> dict:
    """Incrementally refresh all sources; force rebuild replaces atomically on success."""
    with _index_lock(ctx) as index_path:
        target = index_path
        if force:
            fd, temporary = tempfile.mkstemp(prefix="rebuild-", suffix=".sqlite", dir=index_path.parent)
            os.close(fd)
            target = Path(temporary)
        connection = None
        try:
            connection = _connect(target)
            _initialize(connection)
            statistics = _refresh(ctx, connection, force)
            connection.close()
            connection = None
            if force:
                os.replace(target, index_path)
            return statistics
        except sqlite3.DatabaseError as error:
            raise RuntimeError(f"local index is unreadable; run 'opsflow reindex --force': {error}") from error
        finally:
            if connection is not None:
                connection.close()
            if force:
                target.unlink(missing_ok=True)


def _card(row: sqlite3.Row) -> dict:
    card = {field: row[field] for field in CARD_COLUMNS}
    for field in ("id", "status", "track_id", "evidence_layer", "kind"):
        if isinstance(card[field], str):
            card[field] = _short(card[field], 200)
    return card


def _time_bound(value: str, default_tz: tzinfo, end: bool = False) -> tuple[float, bool]:
    if not isinstance(value, str):
        raise ValueError("time filter must be an ISO date or datetime string")
    try:
        cleaned = value.strip()
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        exclusive = end and len(cleaned) == 10
        if exclusive:
            parsed += timedelta(days=1)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return parsed.timestamp(), exclusive
    except (ValueError, OverflowError) as error:
        raise ValueError(f"time filter must be a valid ISO date or datetime: {value!r}") from error


def search(
    ctx: OpsContext,
    keyword: str = "",
    source: str = "all",
    track_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 20,
    offset: int = 0,
    evidence_layer: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tag: str | None = None,
) -> dict:
    """Return bounded summary cards ordered by explicit timestamp DESC, unknown last."""
    source = SOURCE_ALIASES.get(source, source)
    valid_sources = {"all", "trace", "eval", "research", "track", "lesson", "project"} | set(ctx.eval_layers)
    if source not in valid_sources:
        raise ValueError(f"unknown source {source!r}; valid sources: {', '.join(sorted(valid_sources))}")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset cannot be negative")
    default_tz = ctx.default_timezone()
    lower = _time_bound(since, default_tz)[0] if since is not None else None
    upper, exclusive = _time_bound(until, default_tz, end=True) if until is not None else (None, False)
    if lower is not None and upper is not None and (lower > upper or (exclusive and lower == upper)):
        raise ValueError("since cannot be later than until")
    if tag is not None and (not isinstance(tag, str) or not tag.strip()):
        raise ValueError("tag must be a non-empty string")
    reindex(ctx)
    clauses: list[str] = []
    values: list[object] = []
    if keyword:
        clauses.append("instr(search_text, ?) > 0")
        values.append(keyword.casefold())
    if source == "eval" and ctx.eval_layers:
        eval_sources = sorted(ctx.eval_source_names())
        placeholders = ",".join("?" for _ in eval_sources)
        clauses.append(f"source IN ({placeholders})")
        values.extend(eval_sources)
    elif source != "all":
        clauses.append("source = ? COLLATE NOCASE")
        values.append(source)
    for field, value in (("track_id", track_id), ("status", status), ("kind", kind), ("evidence_layer", evidence_layer)):
        if value is not None:
            clauses.append(f"{field} = ? COLLATE NOCASE")
            values.append(value)
    if lower is not None:
        clauses.append("sort_time >= ?")
        values.append(lower)
    if upper is not None:
        clauses.append("sort_time < ?" if exclusive else "sort_time <= ?")
        values.append(upper)
    if tag is not None:
        clauses.append("instr(tags_json, ?) > 0")
        values.append(json.dumps(tag.casefold(), ensure_ascii=False))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = _connect(ctx.index_dir / "ops.sqlite")
    try:
        connection.execute("BEGIN")
        total = connection.execute("SELECT count(*) FROM records" + where, values).fetchone()[0]
        rows = connection.execute(
            "SELECT " + ",".join(CARD_COLUMNS) + " FROM records" + where +
            " ORDER BY sort_time IS NULL, sort_time DESC, key LIMIT ? OFFSET ?",
            values + [limit, offset],
        ).fetchall()
        return {
            "records": [_card(row) for row in rows],
            "total": total,
            "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            **_errors(connection),
        }
    finally:
        connection.close()


def get_record(ctx: OpsContext, key: str) -> dict:
    """Read source record directly by unique key or unambiguous business ID."""
    reindex(ctx)
    connection = _connect(ctx.index_dir / "ops.sqlite")
    try:
        rows = connection.execute("SELECT * FROM records WHERE key=?", (key,)).fetchall()
        if not rows:
            rows = connection.execute("SELECT * FROM records WHERE id=? ORDER BY key", (key,)).fetchall()
        if not rows:
            raise KeyError(f"record not found (it may have been updated or removed): {key}")
        if len(rows) > 1:
            choices = ", ".join(row["key"] for row in rows[:10])
            raise ValueError(f"business ID {key!r} matches {len(rows)} records; use a unique key: {choices}")
        row = rows[0]
        raw_path = Path(row["path"])
        path = raw_path if raw_path.is_absolute() else ctx.ops_dir.resolve() / raw_path
        with path.open("rb") as handle:
            handle.seek(row["byte_offset"])
            raw = handle.read(row["read_bytes"])
        if _digest(raw) != row["sha256"]:
            raise RuntimeError(f"source file changed while reading detail; retry: {row['path']}:{row['line']}")
        record = {"text": raw.decode("utf-8")} if row["source"] == "lesson" else json.loads(raw)
        return {"record": record, "meta": {**_card(row), "sha256": row["sha256"]}}
    finally:
        connection.close()
