"""Configuration discovery and path resolution for opsflow v2 (v1 read-compatible)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path


CONFIG_DIR = ".opsflow"
CONFIG_FILE = "config.json"
DEFAULT_OWNER = "agent"
LAYER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

DEFAULT_BUDGETS: dict[str, int] = {
    "brief_bytes": 12 * 1024,
    "context_bytes": 24 * 1024,
    "query_bytes": 8 * 1024,
    "show_bytes": 16 * 1024,
    "result_bytes": 16 * 1024,
    "agent_doc_bytes": 8 * 1024,
    "lessons_bytes": 64 * 1024,
}

DEFAULT_RETIRED_DOCS: tuple[str, ...] = (
    "current_state.md",
    "handoff/CURRENT_HANDOFF.md",
    "trace/failure_patterns.md",
)

SOURCE_ALIASES: dict[str, str] = {
    "traces": "trace",
    "evals": "eval",
    "references": "research",
    "refs": "research",
    "tracks": "track",
    "lessons": "lesson",
}


def default_agent_docs() -> dict[str, str]:
    return {"agents": "AGENTS.md", "claude": "CLAUDE.md"}


def validate_layer_name(name: str) -> str:
    reserved = {"trace", "research", "track", "lesson", "project", "all"}
    if not isinstance(name, str) or not LAYER_PATTERN.fullmatch(name) or name.casefold() in reserved:
        raise ValueError(
            "layer name must be 1-64 alphanumeric/underscore/hyphen chars and not a reserved source name"
        )
    return name


def parse_timezone(spec: str | None) -> tzinfo:
    if not spec:
        return datetime.now().astimezone().tzinfo or timezone.utc
    text = spec.strip()
    if text.upper() in {"UTC", "Z"}:
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if not match:
        raise ValueError(f"invalid timezone offset (expected UTC or +/-HH:MM): {spec!r}")
    sign = 1 if match.group(1) == "+" else -1
    hours, minutes = int(match.group(2)), int(match.group(3))
    if hours > 23 or minutes > 59:
        raise ValueError(f"invalid timezone offset: {spec!r}")
    return timezone(timedelta(seconds=sign * (hours * 3600 + minutes * 60)))


def default_config(
    *,
    project_name: str,
    ops_dir: str = "ops",
    trace_log: str = "trace/trace_log.jsonl",
    eval_log: str = "evals/eval_log.jsonl",
    research_log: str = "research/reference_intake.jsonl",
    eval_layers: dict[str, str] | None = None,
    layer_modes: dict[str, list[str]] | None = None,
    owner: str = DEFAULT_OWNER,
    tz: str = "",
) -> dict:
    layers: dict[str, str] = {}
    for name, rel in (eval_layers or {}).items():
        layers[validate_layer_name(name)] = rel
    logs = {"trace": trace_log, "research": research_log}
    if not layers:
        logs["eval"] = eval_log
    elif eval_log:
        logs["eval"] = eval_log
    return {
        "version": 2,
        "project_name": project_name,
        "ops_dir": ops_dir,
        "tracks_dir": "tracks",
        "lessons_dir": "lessons",
        "project_file": "project.json",
        "brief_file": "BRIEF.md",
        "logs": logs,
        "eval_layers": layers,
        "layer_modes": dict(layer_modes or {}),
        "agent_docs": default_agent_docs(),
        "retired_docs": list(DEFAULT_RETIRED_DOCS),
        "budgets": dict(DEFAULT_BUDGETS),
        "defaults": {"owner": owner, "timezone": tz},
    }


def discover_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in [start, *start.parents]:
        if (candidate / CONFIG_DIR / CONFIG_FILE).is_file():
            return candidate
    return start


def load_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


@dataclass(frozen=True)
class OpsContext:
    root: Path
    config_path: Path
    config: dict

    @property
    def project_name(self) -> str:
        return str(self.config.get("project_name") or self.root.name)

    @property
    def ops_dir(self) -> Path:
        raw = Path(str(self.config.get("ops_dir", "ops")))
        return raw if raw.is_absolute() else self.root / raw

    @property
    def tracks_dir(self) -> Path:
        return self.rel_path(str(self.config.get("tracks_dir", "tracks")))

    @property
    def lessons_dir(self) -> Path:
        return self.rel_path(str(self.config.get("lessons_dir", "lessons")))

    @property
    def project_file(self) -> Path:
        return self.rel_path(str(self.config.get("project_file", "project.json")))

    @property
    def brief_file(self) -> Path:
        return self.rel_path(str(self.config.get("brief_file", "BRIEF.md")))

    @property
    def index_dir(self) -> Path:
        return self.ops_dir / ".index"

    @property
    def eval_layers(self) -> dict[str, str]:
        raw = self.config.get("eval_layers") or {}
        if not isinstance(raw, dict):
            raise ValueError("config.eval_layers must be a mapping of layer -> relative path")
        return {validate_layer_name(k): str(v) for k, v in raw.items()}

    @property
    def layer_modes(self) -> dict[str, list[str]]:
        raw = self.config.get("layer_modes") or {}
        if not isinstance(raw, dict):
            raise ValueError("config.layer_modes must be a mapping of layer -> list of modes")
        return {str(k): [str(m) for m in v] for k, v in raw.items() if isinstance(v, list)}

    def rel_path(self, value: str) -> Path:
        raw = Path(value)
        return raw if raw.is_absolute() else self.ops_dir / raw

    def root_path(self, value: str) -> Path:
        raw = Path(value)
        return raw if raw.is_absolute() else self.root / raw

    def default_owner(self) -> str:
        return str(self.config.get("defaults", {}).get("owner") or DEFAULT_OWNER)

    def default_timezone(self) -> tzinfo:
        return parse_timezone(self.config.get("defaults", {}).get("timezone"))

    def budget(self, name: str) -> int:
        raw = self.config.get("budgets", {}).get(name, DEFAULT_BUDGETS[name])
        if type(raw) is not int or raw < 512:
            raise ValueError(f"budget {name} must be an integer >= 512")
        return raw

    @property
    def trace_log_path(self) -> Path:
        return self.rel_path(str(self.config.get("logs", {}).get("trace", "trace/trace_log.jsonl")))

    @property
    def research_log_path(self) -> Path:
        return self.rel_path(str(self.config.get("logs", {}).get("research", "research/reference_intake.jsonl")))

    def eval_log_path(self, layer: str | None = None) -> Path:
        layers = self.eval_layers
        if layers:
            if layer is None:
                if len(layers) == 1:
                    layer = next(iter(layers))
                else:
                    raise ValueError(
                        f"evidence_layer is required when eval_layers are configured: {', '.join(sorted(layers))}"
                    )
            if layer not in layers:
                raise ValueError(
                    f"unknown evidence_layer {layer!r}; configured eval_layers: {', '.join(sorted(layers))}"
                )
            return self.rel_path(layers[layer])
        return self.rel_path(str(self.config.get("logs", {}).get("eval", "evals/eval_log.jsonl")))

    def all_ledger_paths(self) -> dict[str, Path]:
        paths: dict[str, Path] = {"trace": self.trace_log_path}
        layers = self.eval_layers
        if layers:
            layer_resolved: set[Path] = set()
            for name, rel in layers.items():
                resolved = self.rel_path(rel)
                paths[name] = resolved
                layer_resolved.add(resolved.resolve())
            raw_eval = self.config.get("logs", {}).get("eval")
            if raw_eval:
                legacy_eval = self.rel_path(str(raw_eval))
                if legacy_eval.is_file() and legacy_eval.resolve() not in layer_resolved:
                    paths["eval"] = legacy_eval
        else:
            paths["eval"] = self.eval_log_path(None)
        paths["research"] = self.research_log_path
        return paths

    def eval_source_names(self) -> set[str]:
        return {"eval"} | set(self.eval_layers)

    def agent_doc_paths(self) -> dict[str, Path]:
        raw = self.config.get("agent_docs", default_agent_docs())
        return {str(k): self.root_path(str(v)) for k, v in raw.items()}

    def retired_doc_paths(self) -> list[tuple[str, Path]]:
        raw = self.config.get("retired_docs", list(DEFAULT_RETIRED_DOCS))
        return [(str(item), self.rel_path(str(item))) for item in raw]

    def resolve_ref(self, reference: str) -> Path:
        path = Path(reference)
        if path.is_absolute():
            return path
        in_ops = self.ops_dir / path
        if in_ops.exists():
            return in_ops
        in_root = self.root / path
        if in_root.exists():
            return in_root
        try:
            ops_rel = self.ops_dir.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            ops_rel = ""
        if ops_rel and (reference == ops_rel or reference.startswith(ops_rel + "/")):
            return in_root
        return in_ops

    def resolve_detail_ref(self, reference: str) -> Path:
        ops_root = self.ops_dir.resolve()
        candidate = (ops_root / Path(reference)).resolve()
        if not candidate.is_relative_to(ops_root):
            raise ValueError(f"detail_ref path escapes ops directory: {reference!r}")
        return candidate


def load_context(*, root: str | Path | None = None, ops_dir: str | None = None, config: str | Path | None = None) -> OpsContext:
    base = Path(root).expanduser().resolve() if root else discover_root(Path.cwd())
    if config:
        config_path = Path(config).expanduser().resolve()
        cfg = load_json_file(config_path)
        if not root and config_path.parent.name == CONFIG_DIR:
            base = config_path.parents[1]
    else:
        config_path = base / CONFIG_DIR / CONFIG_FILE
        if config_path.exists():
            cfg = load_json_file(config_path)
        else:
            cfg = default_config(project_name=base.name, ops_dir=ops_dir or "ops")
    if ops_dir:
        cfg = dict(cfg)
        cfg["ops_dir"] = ops_dir
    return OpsContext(root=base, config_path=config_path, config=cfg)
