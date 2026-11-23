"""One overwrite-in-place state card per parallel work track."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import OpsContext
from .storage import atomic_json, file_lock, now_iso


STATUSES = frozenset({"planned", "active", "paused", "closed"})
TEXT_LIMITS = {
    "title": 300,
    "phase": 120,
    "summary": 2000,
    "conclusion": 4000,
    "verified_at": 80,
}
LIST_LIMITS = {
    "next_actions": (20, 2000),
    "blockers": (20, 2000),
    "constraints": (30, 2000),
    "evidence_refs": (100, 4096),
    "status_refs": (30, 4096),
}
GENERATED_FIELDS = {"schema_version", "revision", "updated_at"}
FIELDS = set(TEXT_LIMITS) | set(LIST_LIMITS) | GENERATED_FIELDS | {"track_id", "status"}
TRACK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")


def validate_track_id(track_id: object) -> str:
    if not isinstance(track_id, str) or not TRACK_ID_PATTERN.fullmatch(track_id):
        raise ValueError(
            "track_id must be a 1-160 char single directory name using letters, digits, '_', '.', or '-'"
        )
    return track_id


def track_path(ctx: OpsContext, track_id: str) -> Path:
    return ctx.tracks_dir / validate_track_id(track_id) / "track.json"


def _validate(card: dict) -> None:
    validate_track_id(card.get("track_id"))
    if card.get("schema_version") != 1:
        raise ValueError("track.json schema_version must be 1")
    if card.get("status") not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(STATUSES))}")
    for field, limit in TEXT_LIMITS.items():
        value = card.get(field)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{field} must be a string of at most {limit} characters")
    if not card["title"].strip():
        raise ValueError("title cannot be empty")
    for field, (count, length) in LIST_LIMITS.items():
        value = card.get(field)
        if not isinstance(value, list) or len(value) > count:
            raise ValueError(f"{field} must be a list of at most {count} strings")
        if any(not isinstance(item, str) or not item.strip() or len(item) > length for item in value):
            raise ValueError(f"each item in {field} must be a non-empty string of at most {length} characters")
    if card["status"] == "closed":
        if not card["conclusion"].strip():
            raise ValueError("closed track requires a non-empty conclusion")
        if card["next_actions"] or card["blockers"]:
            raise ValueError("closed track cannot retain next_actions or blockers")
    if card["verified_at"]:
        try:
            datetime.fromisoformat(card["verified_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("verified_at must be an ISO datetime or empty string") from exc
    json.dumps(card, allow_nan=False)


def get_track(ctx: OpsContext, track_id: str) -> dict:
    path = track_path(ctx, track_id)
    card = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(card, dict) or card.get("track_id") != track_id:
        raise ValueError(f"track card identity mismatch: {path}")
    _validate(card)
    if type(card.get("revision")) is not int or card["revision"] < 1:
        raise ValueError(f"invalid track revision: {path}")
    return card


def list_tracks(ctx: OpsContext) -> list[dict]:
    if not ctx.tracks_dir.is_dir():
        return []
    return [get_track(ctx, path.parent.name) for path in sorted(ctx.tracks_dir.glob("*/track.json"))]


def set_track(ctx: OpsContext, payload: dict, expected_revision: int | None = None) -> dict:
    """Apply a partial or full update with optimistic revision concurrency and idempotent retry."""
    if not isinstance(payload, dict):
        raise ValueError("track payload must be a JSON object")
    unknown = set(payload) - FIELDS
    if unknown:
        raise ValueError(f"unknown track fields: {', '.join(sorted(unknown))}")
    track_id = validate_track_id(payload.get("track_id"))
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError("expected_revision must be a non-negative integer")
    if "schema_version" in payload and payload["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    path = track_path(ctx, track_id)
    with file_lock(path):
        current = get_track(ctx, track_id) if path.exists() else None
        card = current.copy() if current else {
            "schema_version": 1,
            "track_id": track_id,
            "status": "planned",
            **{field: "" for field in TEXT_LIMITS},
            **{field: [] for field in LIST_LIMITS},
        }
        card.update({key: value for key, value in payload.items() if key not in GENERATED_FIELDS})
        _validate(card)
        if current:
            if all(card.get(key) == current.get(key) for key in FIELDS - GENERATED_FIELDS):
                return current
            if expected_revision != current["revision"]:
                raise ValueError(
                    f"track revision conflict: expected_revision={expected_revision}, current={current['revision']}"
                )
            if current["status"] == "closed" and card["status"] != "closed" and payload.get("status") != "active":
                raise ValueError("reopening a closed track requires explicit status='active' and current revision")
        elif expected_revision not in (None, 0):
            raise ValueError("track does not exist yet; omit expected_revision or pass 0")
        card["revision"] = current["revision"] + 1 if current else 1
        card["updated_at"] = now_iso(ctx.default_timezone())
        atomic_json(path, card)
        return card


def close_track(
    ctx: OpsContext,
    track_id: str,
    conclusion: str,
    evidence_refs: list[str] | None = None,
    expected_revision: int | None = None,
) -> dict:
    if not isinstance(conclusion, str) or not conclusion.strip():
        raise ValueError("closing a track requires a non-empty conclusion")
    get_track(ctx, track_id)
    payload: dict = {
        "track_id": track_id,
        "status": "closed",
        "conclusion": conclusion,
        "next_actions": [],
        "blockers": [],
    }
    if evidence_refs is not None:
        payload["evidence_refs"] = evidence_refs
    return set_track(ctx, payload, expected_revision)
