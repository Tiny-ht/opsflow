"""Safe append-only ledger writes and bounded structured evaluation results."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .config import OpsContext, validate_layer_name
from .storage import atomic_json, atomic_text, file_lock, now_iso, sha256_file, stable_id
from .tracks import validate_track_id


RECORD_TIME_FIELDS = frozenset({"ts", "recorded_at", "created_at", "updated_at"})
EVAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
KNOWN_LOG_IDS = {
    "trace_log.jsonl": "trace_id",
    "eval_log.jsonl": "eval_id",
    "reference_intake.jsonl": "ref_id",
}
FALLBACK_ID_FIELDS = ("eval_id", "trace_id", "ref_id", "record_id", "event_id", "id")


def _json_line(record: dict) -> str:
    if not isinstance(record, dict):
        raise ValueError("ledger record must be a JSON object")
    return json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def _identity(record: dict) -> str:
    return stable_id({key: value for key, value in record.items() if key not in RECORD_TIME_FIELDS})


def _resolve_id_field(path: Path, record: dict, id_field: str | None) -> str:
    if id_field:
        return id_field
    known = KNOWN_LOG_IDS.get(path.name)
    if known and known in record:
        return known
    if path.name.endswith("_eval_log.jsonl") and "eval_id" in record:
        return "eval_id"
    candidates = [key for key in FALLBACK_ID_FIELDS if key in record]
    if len(candidates) != 1:
        raise ValueError("cannot determine record ID field; pass id_field explicitly")
    return candidates[0]


def append_record(path: Path, record: dict, id_field: str | None = None) -> bool:
    """Append record safely. Returns True if inserted, False on idempotent retry."""
    path = Path(path)
    line = _json_line(record)
    field = _resolve_id_field(path, record, id_field)
    record_id = record.get(field)
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError(f"{field} must be a non-empty string")
    with file_lock(path):
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        matches = []
        for number, old_line in enumerate(content.splitlines(), 1):
            if not old_line.strip():
                continue
            try:
                old = json.loads(old_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt ledger line at {path}:{number}; fix before appending") from exc
            if not isinstance(old, dict):
                raise ValueError(f"ledger line is not a JSON object at {path}:{number}")
            if old.get(field) == record_id:
                matches.append(old)
        if len(matches) > 1:
            raise ValueError(f"duplicate ID already exists: {field}={record_id} ({len(matches)} rows)")
        if matches:
            if _identity(matches[0]) == _identity(record):
                return False
            raise ValueError(f"conflicting content for existing ID: {field}={record_id}")
        sep = "\n" if content and not content.endswith("\n") else ""
        atomic_text(path, content + sep + line)
        return True


def _text(payload: dict, field: str, limit: int, default: str = "") -> str:
    value = payload.get(field, default)
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{field} must be a string of at most {limit} characters")
    return value


def _metrics(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("metrics must be a JSON object keyed by metric name")
    for name, metric in value.items():
        if not isinstance(name, str) or not name or len(name) > 160:
            raise ValueError("metric name must be a 1-160 character string")
        if not isinstance(metric, dict):
            raise ValueError(
                f"metric {name!r} must be an object with value, unit, and aggregation (bare numbers are rejected)"
            )
        if "value" not in metric:
            raise ValueError(f"metric {name!r} is missing 'value'; put breakdowns in 'extra'")
        number = metric.get("value")
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number)):
            raise ValueError(f"metric {name!r}.value must be a finite number or null")
        for field in ("unit", "aggregation"):
            text = metric.get(field)
            if not isinstance(text, str) or not text.strip() or len(text) > 300:
                raise ValueError(f"metric {name!r}.{field} must be a non-empty string of at most 300 characters")
    return value


def _validate_result_routing(ctx: OpsContext, payload: dict) -> tuple[str | None, Path, str]:
    layers = ctx.eval_layers
    raw_layer = payload.get("evidence_layer")
    if layers:
        if raw_layer is None and len(layers) == 1:
            layer = next(iter(layers))
        elif isinstance(raw_layer, str) and raw_layer in layers:
            layer = raw_layer
        else:
            raise ValueError(f"evidence_layer must be one of configured eval_layers: {', '.join(sorted(layers))}")
        target = ctx.eval_log_path(layer)
    else:
        layer = validate_layer_name(raw_layer) if raw_layer is not None else None
        target = ctx.eval_log_path(None)
    mode = _text(payload, "execution_mode", 100)
    if layer and layer in ctx.layer_modes:
        allowed = ctx.layer_modes[layer]
        if mode not in allowed:
            raise ValueError(f"execution_mode={mode!r} is not allowed for layer {layer!r}: {', '.join(allowed)}")
    return layer, target, mode


def add_result(ctx: OpsContext, payload: dict) -> dict:
    """Record a validated evaluation result; auto-externalize records larger than result_bytes."""
    _json_line(payload)
    if "schema_version" in payload and payload["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    layer, target_log, mode = _validate_result_routing(ctx, payload)
    track_id = validate_track_id(payload["track_id"]) if payload.get("track_id") is not None else None
    layer_id_key = f"{layer}_eval_id" if layer else None
    if layer_id_key and payload.get("eval_id") and payload.get(layer_id_key) and payload["eval_id"] != payload[layer_id_key]:
        raise ValueError(f"eval_id conflicts with {layer_id_key}")
    record_id = payload.get("eval_id") or (payload.get(layer_id_key) if layer_id_key else None)
    if record_id is not None and (not isinstance(record_id, str) or not EVAL_ID_PATTERN.fullmatch(record_id)):
        raise ValueError("eval_id must be a safe 1-200 character filename identifier")
    status = payload.get("status", "completed")
    if status not in {"completed", "failed"}:
        raise ValueError("add-result status must be 'completed' or 'failed'")
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list) or any(not isinstance(item, (str, dict)) for item in artifacts):
        raise ValueError("artifacts must be a list of path strings or artifact objects")
    supersedes = payload.get("supersedes", [])
    if not isinstance(supersedes, list) or any(not isinstance(item, str) or not item for item in supersedes):
        raise ValueError("supersedes must be a list of non-empty record ID strings")
    metrics = _metrics(payload.get("metrics", {}))
    raw_extra = payload.get("extra", {})
    if not isinstance(raw_extra, dict):
        raise ValueError("extra must be a JSON object")
    extra = dict(raw_extra)
    extra_tags = extra.pop("tags", None)
    tags = payload.get("tags", extra_tags if extra_tags is not None else [])
    if extra_tags is not None and "tags" in payload and tags != extra_tags:
        raise ValueError("top-level tags differ from extra.tags")
    if not isinstance(tags, list) or len(tags) > 30 or any(
        not isinstance(tag, str) or not tag.strip() or len(tag) > 64 for tag in tags
    ):
        raise ValueError("tags must be a list of at most 30 non-empty strings (<= 64 chars each)")
    record: dict = {
        "schema_version": 1,
        "eval_id": record_id,
        "run_id": _text(payload, "run_id", 500),
        "ts": _text(payload, "ts", 100, now_iso(ctx.default_timezone())),
        "status": status,
        "summary": _text(payload, "summary", 2000),
        "metrics": metrics,
        "artifacts": artifacts,
        "supersedes": supersedes,
        "tags": tags,
    }
    if track_id is not None:
        record["track_id"] = track_id
    if layer is not None:
        record["evidence_layer"] = layer
    if mode:
        record["execution_mode"] = mode
    for field in RECORD_TIME_FIELDS - {"ts"}:
        if field in payload:
            record[field] = _text(payload, field, 100)
    ignored = {"extra"} | ({layer_id_key} if layer_id_key else set())
    for key, value in payload.items():
        if key not in record and key not in ignored:
            if key in extra and extra[key] != value:
                raise ValueError(f"field {key!r} conflicts between top-level and extra")
            extra[key] = value
    if extra:
        record["extra"] = extra
    if record_id is None:
        prefix = f"{layer}_eval" if layer else "eval"
        digest = _identity({k: v for k, v in record.items() if k != "eval_id"})
        record_id = f"{prefix}_{digest}"
        record["eval_id"] = record_id
    max_bytes = ctx.budget("result_bytes")
    if len(_json_line(record).encode("utf-8")) <= max_bytes:
        append_record(target_log, record, "eval_id")
        return record
    return _externalize_result(ctx, target_log, record, track_id, record_id, metrics, artifacts, max_bytes)


def _externalize_result(
    ctx: OpsContext,
    target_log: Path,
    record: dict,
    track_id: str | None,
    record_id: str,
    metrics: dict,
    artifacts: list,
    max_bytes: int,
) -> dict:
    if track_id is not None:
        full_path = ctx.tracks_dir / track_id / "results" / f"{record_id}.json"
    else:
        full_path = ctx.ops_dir / "evals" / "results" / f"{record_id}.json"
    with file_lock(full_path):
        if full_path.exists():
            old = json.loads(full_path.read_text(encoding="utf-8"))
            if not isinstance(old, dict) or _identity(old) != _identity(record):
                raise ValueError(f"conflicting content for externalized result: {full_path}")
            record = old
        else:
            atomic_json(full_path, record)
        compact = {k: v for k, v in record.items() if k not in {"metrics", "artifacts", "extra"}}
        compact["metrics"] = {}
        compact["detail_ref"] = {
            "path": full_path.resolve().relative_to(ctx.ops_dir.resolve()).as_posix(),
            "sha256": sha256_file(full_path),
            "size_bytes": full_path.stat().st_size,
        }
        compact["artifacts"] = [compact["detail_ref"]]
        compact["metric_count"] = len(metrics)
        compact["artifact_count"] = len(artifacts)
        for name, metric in list(metrics.items())[:10]:
            candidate = {**compact, "metrics": {**compact["metrics"], name: metric}}
            if len(_json_line(candidate).encode("utf-8")) <= max_bytes:
                compact = candidate
        if len(_json_line(compact).encode("utf-8")) > max_bytes:
            raise ValueError(
                f"compact result reference exceeds {max_bytes} bytes; shorten summary or supersedes"
            )
        append_record(target_log, compact, "eval_id")
        return compact
