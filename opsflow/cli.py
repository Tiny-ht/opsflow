#!/usr/bin/env python3
"""Generic ops ledger and handoff helper.

Opsflow is intentionally small and dependency-free. It keeps append-only JSONL
ledgers next to human-readable handoff/state docs so long-running work can be
resumed without relying on chat history.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


CONFIG_DIR = ".opsflow"
CONFIG_FILE = "config.json"
DEFAULT_OWNER = "codex"
SOURCE_ALIASES = {
    "traces": "trace",
    "eval": "eval",
    "evals": "eval",
    "references": "research",
    "refs": "research",
}


@dataclass(frozen=True)
class OpsContext:
    root: Path
    config_path: Path
    config: dict

    @property
    def ops_dir(self) -> Path:
        raw = Path(str(self.config["ops_dir"]))
        return raw if raw.is_absolute() else self.root / raw

    def rel_path(self, value: str) -> Path:
        raw = Path(value)
        return raw if raw.is_absolute() else self.ops_dir / raw

    def log_paths(self) -> dict[str, Path]:
        return {key: self.rel_path(value) for key, value in self.config["logs"].items()}

    def doc_paths(self) -> dict[str, Path]:
        return {key: self.rel_path(value) for key, value in self.config["docs"].items()}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, record: dict) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
    return rows


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def auto_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')}"


def parse_value(value: str) -> object:
    text = value.strip()
    if not text:
        return ""
    if text[0] in "[{\"" or text in {"true", "false", "null"}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return value


def key_value(raw: str) -> tuple[str, object]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError("field key cannot be empty")
    return key, parse_value(value)


def extra_fields(pairs: Sequence[tuple[str, object]] | None) -> dict:
    fields: dict = {}
    for key, value in pairs or []:
        fields[key] = value
    return fields


def print_records(records: Iterable[dict], *, jsonl: bool = False) -> None:
    for row in records:
        if jsonl:
            print(json.dumps(row, ensure_ascii=False))
        else:
            print(json.dumps(row, ensure_ascii=False, indent=2))


def default_config(
    *,
    project_name: str,
    ops_dir: str,
    trace_log: str,
    eval_log: str,
    research_log: str,
    owner: str,
) -> dict:
    return {
        "version": 1,
        "project_name": project_name,
        "ops_dir": ops_dir,
        "logs": {
            "trace": trace_log,
            "eval": eval_log,
            "research": research_log,
        },
        "docs": {
            "readme": "README.md",
            "current_state": "current_state.md",
            "roadmap": "roadmap.md",
            "handoff": "handoff/CURRENT_HANDOFF.md",
            "handoff_template": "handoff/HANDOFF_TEMPLATE.md",
            "trace_readme": "trace/README.md",
            "failure_patterns": "trace/failure_patterns.md",
            "eval_readme": "evals/README.md",
            "eval_signal_template": "evals/user_signal_template.md",
            "research_readme": "research/README.md",
            "reference_note_template": "research/reference_note_template.md",
        },
        "defaults": {
            "owner": owner,
        },
    }


def discover_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in [start, *start.parents]:
        if (candidate / CONFIG_DIR / CONFIG_FILE).is_file():
            return candidate
    return start


def load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(f"Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid config: {path}: {exc}") from exc


def load_context(args: argparse.Namespace) -> OpsContext:
    if args.root:
        root = Path(args.root).expanduser().resolve()
    else:
        root = discover_root(Path.cwd())

    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        config = load_json(config_path)
        root = config_path.parents[1] if config_path.parent.name == CONFIG_DIR else root
    else:
        config_path = root / CONFIG_DIR / CONFIG_FILE
        if config_path.exists():
            config = load_json(config_path)
        else:
            ops_dir = args.ops_dir or "ops"
            config = default_config(
                project_name=root.name,
                ops_dir=ops_dir,
                trace_log="trace/trace_log.jsonl",
                eval_log="evals/eval_log.jsonl",
                research_log="research/reference_intake.jsonl",
                owner=DEFAULT_OWNER,
            )

    if args.ops_dir:
        config = dict(config)
        config["ops_dir"] = args.ops_dir

    return OpsContext(root=root, config_path=config_path, config=config)


def normalize_source(source: str) -> str:
    source = SOURCE_ALIASES.get(source, source)
    if source not in {"trace", "eval", "research", "all"}:
        raise SystemExit("source must be one of: trace, eval, research, all")
    return source


def selected_sources(ctx: OpsContext, source: str) -> list[tuple[str, Path]]:
    paths = ctx.log_paths()
    source = normalize_source(source)
    if source == "all":
        return [(name, paths[name]) for name in ("trace", "eval", "research")]
    return [(source, paths[source])]


def latest_id(ctx: OpsContext, source: str) -> str:
    rows = read_jsonl(ctx.log_paths()[source])
    if not rows:
        return ""
    row = rows[-1]
    return str(
        row.get(f"{source}_id")
        or row.get("eval_id")
        or row.get("ref_id")
        or row.get("trace_id")
        or ""
    )


def write_text(path: Path, text: str, *, overwrite: bool = False) -> bool:
    if path.exists() and not overwrite:
        return False
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")
    return True


def touch_jsonl(path: Path) -> bool:
    ensure_parent(path)
    if path.exists():
        return False
    path.touch()
    return True


def template_readme(project_name: str, *, trace_log: str, eval_log: str, research_log: str) -> str:
    return f"""# {project_name} Ops

This directory keeps the project resumable:

- `current_state.md`: the single current-state snapshot
- `roadmap.md`: phases, gates, and exit criteria
- `handoff/CURRENT_HANDOFF.md`: the document a new operator should read first
- `{trace_log}`: append-only event ledger
- `trace/failure_patterns.md`: reusable failure-mode index
- `{eval_log}`: structured evaluation and external-result ledger
- `{research_log}`: reference, paper, report, and repo intake ledger

## Workflow

1. Record planned or running work in `trace`.
2. Record important code, data, platform, or decision outcomes in `trace`.
3. Record real evaluation feedback in `eval`.
4. Record external references before they influence the plan.
5. Refresh `current_state.md` and `handoff/CURRENT_HANDOFF.md` before handoff.

## Common Commands

```bash
opsflow latest --source trace --limit 5
opsflow query --keyword blocked --source all
opsflow add-trace --stage build --kind code_change --status succeeded --title "Add parser" --summary "Parser now handles empty input."
opsflow add-eval --run-id run_001 --status succeeded --score 0.91 --summary "Smoke eval passed."
opsflow handoff draft
```
"""


def template_current_state(project_name: str) -> str:
    return f"""# {project_name} Current State

## Snapshot

- date: {today()}
- phase:
- owner:
- current_focus:

## Stable Facts

-

## Unstable Or Unproven

-

## Active Blockers

-

## Latest Important Records

- latest_trace_id:
- latest_eval_id:
- latest_ref_id:

## Next Actions

1.
2.
3.
"""


def template_roadmap(project_name: str) -> str:
    return f"""# {project_name} Roadmap

## Phases

1. Discovery
2. Baseline
3. Iteration
4. Validation
5. Handoff

## Gates

- Each phase has an explicit entry condition.
- Each important decision has a trace record.
- Each evaluation result is linked to a parent trace or run.
- Each adopted reference has an intake record.

## Exit Criteria

-
"""


def template_handoff(project_name: str, *, trace_log: str, eval_log: str) -> str:
    return f"""# {project_name} Handoff

## Snapshot

- date:
- phase:
- owner:
- current_focus:

## What Is Stable

-

## What Is Not Stable

-

## Latest Important Records

- latest_trace_id:
- latest_eval_id:
- latest_ref_id:

## Current Working Hypothesis

-

## Known Risks

-

## Exact Next 3 Actions

1.
2.
3.

## What The Next Operator Should Read First

1. current_state.md
2. {trace_log}
3. {eval_log}

## Open Questions Waiting For The Owner

-

## Update Rule

- Refresh this file whenever real evaluation feedback or a direction-changing decision arrives.
"""


def template_trace_readme() -> str:
    return """# Trace Protocol

`trace_log.jsonl` is an append-only event ledger, not a summary document.

Record a trace when you:

- start a hypothesis
- begin or finish a meaningful code/data/doc change
- start a platform run or external process
- read a result
- confirm a failure, no-gain result, rollback, or blocker
- make a direction-changing decision

Recommended fields:

- `trace_id`
- `ts`
- `stage`
- `kind`
- `status`
- `title`
- `summary`
- `hypothesis`
- `inputs`
- `artifacts`
- `metrics`
- `parent`
- `next_action`
- `tags`
- `owner`
"""


def template_eval_readme() -> str:
    return """# Eval Protocol

`eval_log.jsonl` records real feedback from tests, users, offline metrics,
platform runs, reviews, or other external checks.

Recommended fields:

- `eval_id`
- `ts`
- `run_id`
- `parent_run`
- `status`
- `score`
- `rank`
- `runtime`
- `resource`
- `summary`
- `raw_signal`
- `confidence`
- `next_action`
- `tags`

Rules:

- Link each eval to a parent run, trace, commit, or artifact when possible.
- Do not treat a result as comparable if the parent context is unknown.
- Convert failures and no-gain streaks into trace records and failure patterns.
"""


def template_user_signal() -> str:
    return """# Eval Signal Template

- run_id:
- parent_run:
- status:
- score:
- rank:
- runtime:
- resource:
- summary:
- raw_signal:
- confidence:
- next_action:
- tags:
"""


def template_research_readme() -> str:
    return """# Research Intake

Use `reference_intake.jsonl` for papers, technical reports, docs, repositories,
blog posts, internal notes, and other references that may influence decisions.

Recommended fields:

- `ref_id`
- `ts`
- `source_type`
- `title`
- `org_or_author`
- `url_or_path`
- `scenario_match`
- `status`
- `transferable_module`
- `non_transferable_assumptions`
- `engineering_cost`
- `validation_plan`
- `notes`
- `related_hypotheses`
- `tags`

Status suggestions: `queued`, `reading`, `extracted`, `adopted`, `rejected`.
"""


def template_reference_note() -> str:
    return """# Reference Note

## Source

- title:
- source_type:
- org_or_author:
- url_or_path:

## Why It Matters

-

## Transferable Ideas

-

## Assumptions That May Not Transfer

-

## Validation Plan

-
"""


def template_failure_patterns() -> str:
    return """# Failure Patterns

Add recurring failure modes here after they appear in trace or eval records.

## Pattern Template

- name:
- symptom:
- trigger:
- suspected_cause:
- evidence:
- avoidance_or_fix:
- related_records:
"""


def init_templates(ctx: OpsContext, *, overwrite_docs: bool = False) -> list[str]:
    project_name = str(ctx.config["project_name"])
    logs = ctx.config["logs"]
    docs = ctx.doc_paths()
    templates = {
        docs["readme"]: template_readme(
            project_name,
            trace_log=logs["trace"],
            eval_log=logs["eval"],
            research_log=logs["research"],
        ),
        docs["current_state"]: template_current_state(project_name),
        docs["roadmap"]: template_roadmap(project_name),
        docs["handoff"]: template_handoff(project_name, trace_log=logs["trace"], eval_log=logs["eval"]),
        docs["handoff_template"]: template_handoff(project_name, trace_log=logs["trace"], eval_log=logs["eval"]),
        docs["trace_readme"]: template_trace_readme(),
        docs["failure_patterns"]: template_failure_patterns(),
        docs["eval_readme"]: template_eval_readme(),
        docs["eval_signal_template"]: template_user_signal(),
        docs["research_readme"]: template_research_readme(),
        docs["reference_note_template"]: template_reference_note(),
    }

    touched: list[str] = []
    for path, text in templates.items():
        if write_text(path, text, overwrite=overwrite_docs):
            touched.append(str(path))

    for path in ctx.log_paths().values():
        if touch_jsonl(path):
            touched.append(str(path))

    return touched


def handle_init(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve() if args.root else Path.cwd().resolve()
    config_path = Path(args.config).expanduser().resolve() if args.config else root / CONFIG_DIR / CONFIG_FILE
    project_name = args.name or root.name
    config = default_config(
        project_name=project_name,
        ops_dir=args.init_ops_dir or args.ops_dir or "ops",
        trace_log=args.trace_log,
        eval_log=args.eval_log,
        research_log=args.research_log,
        owner=args.owner,
    )

    if config_path.exists() and not args.force:
        config = load_json(config_path)
        if args.name:
            config["project_name"] = args.name
    else:
        ensure_parent(config_path)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ctx = OpsContext(root=root, config_path=config_path, config=config)
    touched = init_templates(ctx, overwrite_docs=args.force_docs)
    print(f"Initialized opsflow at {ctx.ops_dir}")
    print(f"Config: {ctx.config_path}")
    if touched:
        print("Created:")
        for item in touched:
            print(f"- {item}")
    else:
        print("No files created; existing docs and ledgers were left in place.")


def handle_paths(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    payload = {
        "root": str(ctx.root),
        "config": str(ctx.config_path),
        "ops_dir": str(ctx.ops_dir),
        "logs": {key: str(value) for key, value in ctx.log_paths().items()},
        "docs": {key: str(value) for key, value in ctx.doc_paths().items()},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def handle_add_trace(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    record = {
        "trace_id": args.trace_id or auto_id("trace"),
        "ts": now_iso(),
        "stage": args.stage,
        "kind": args.kind,
        "status": args.status,
        "title": args.title,
        "summary": args.summary,
        "hypothesis": args.hypothesis or "",
        "inputs": split_csv(args.inputs),
        "artifacts": split_csv(args.artifacts),
        "metrics": args.metrics or "",
        "parent": args.parent or "",
        "next_action": args.next_action or "",
        "tags": split_csv(args.tags),
        "owner": args.owner or ctx.config.get("defaults", {}).get("owner", DEFAULT_OWNER),
    }
    record.update(extra_fields(args.field))
    append_jsonl(ctx.log_paths()["trace"], record)
    print(record["trace_id"])


def handle_add_eval(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    record = {
        "eval_id": args.eval_id or auto_id("eval"),
        "ts": now_iso(),
        "run_id": args.run_id,
        "parent_run": args.parent_run or "",
        "status": args.status,
        "score": args.score,
        "rank": args.rank,
        "runtime": args.runtime or "",
        "resource": args.resource or "",
        "summary": args.summary,
        "raw_signal": args.raw_signal or "",
        "confidence": args.confidence or "",
        "next_action": args.next_action or "",
        "tags": split_csv(args.tags),
    }
    record.update(extra_fields(args.field))
    append_jsonl(ctx.log_paths()["eval"], record)
    print(record["eval_id"])


def handle_add_ref(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    record = {
        "ref_id": args.ref_id or auto_id("ref"),
        "ts": now_iso(),
        "source_type": args.source_type,
        "title": args.title,
        "org_or_author": args.org_or_author or "",
        "url_or_path": args.url_or_path or "",
        "scenario_match": args.scenario_match or "",
        "status": args.status,
        "transferable_module": args.transferable_module or "",
        "non_transferable_assumptions": args.non_transferable_assumptions or "",
        "engineering_cost": args.engineering_cost or "",
        "validation_plan": args.validation_plan or "",
        "notes": args.notes or "",
        "related_hypotheses": split_csv(args.related_hypotheses),
        "tags": split_csv(args.tags),
    }
    record.update(extra_fields(args.field))
    append_jsonl(ctx.log_paths()["research"], record)
    print(record["ref_id"])


def row_matches(row: dict, args: argparse.Namespace, *, source_name: str) -> bool:
    keyword = (args.keyword or "").lower()
    if keyword and keyword not in json.dumps(row, ensure_ascii=False).lower():
        return False
    if args.status and str(row.get("status", "")).lower() != args.status.lower():
        return False
    if args.kind and source_name == "trace" and str(row.get("kind", "")).lower() != args.kind.lower():
        return False
    if args.tag and args.tag not in row.get("tags", []):
        return False
    return True


def handle_query(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    matched: list[dict] = []
    for source_name, path in selected_sources(ctx, args.source):
        for row in read_jsonl(path):
            if not row_matches(row, args, source_name=source_name):
                continue
            row = dict(row)
            row["_source"] = source_name
            matched.append(row)

    matched.sort(key=lambda row: str(row.get("ts", "")))
    matched = matched[-args.limit :]
    if matched:
        print_records(matched, jsonl=args.jsonl)
    else:
        print("No matching records.")


def handle_latest(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    source = normalize_source(args.source)
    if source == "all":
        rows: list[dict] = []
        for source_name, path in selected_sources(ctx, "all"):
            for row in read_jsonl(path)[-args.limit :]:
                row = dict(row)
                row["_source"] = source_name
                rows.append(row)
        rows.sort(key=lambda row: str(row.get("ts", "")))
        rows = rows[-args.limit :]
    else:
        rows = read_jsonl(ctx.log_paths()[source])[-args.limit :]

    if rows:
        print_records(rows, jsonl=args.jsonl)
    else:
        print("No records yet.")


def draft_handoff(ctx: OpsContext, args: argparse.Namespace) -> str:
    project_name = str(ctx.config["project_name"])
    logs = ctx.config["logs"]
    phase = args.phase or ""
    owner = args.owner or ctx.config.get("defaults", {}).get("owner", DEFAULT_OWNER)
    focus = args.focus or ""
    latest_trace = latest_id(ctx, "trace")
    latest_eval = latest_id(ctx, "eval")
    latest_ref = latest_id(ctx, "research")

    return f"""# {project_name} Handoff

## Snapshot

- date: {today()}
- phase: {phase}
- owner: {owner}
- current_focus: {focus}

## What Is Stable

-

## What Is Not Stable

-

## Latest Important Records

- latest_trace_id: {latest_trace}
- latest_eval_id: {latest_eval}
- latest_ref_id: {latest_ref}

## Current Working Hypothesis

-

## Known Risks

-

## Exact Next 3 Actions

1.
2.
3.

## What The Next Operator Should Read First

1. current_state.md
2. {logs["trace"]}
3. {logs["eval"]}

## Open Questions Waiting For The Owner

-
"""


def handle_handoff(args: argparse.Namespace) -> None:
    ctx = load_context(args)
    docs = ctx.doc_paths()
    if args.handoff_command == "path":
        print(docs["handoff"])
        return
    if args.handoff_command == "show":
        path = docs["handoff"]
        if not path.exists():
            raise SystemExit(f"Handoff file does not exist: {path}")
        print(path.read_text(encoding="utf-8"))
        return
    if args.handoff_command == "template":
        path = docs["handoff_template"]
        if path.exists():
            print(path.read_text(encoding="utf-8"))
        else:
            logs = ctx.config["logs"]
            print(template_handoff(str(ctx.config["project_name"]), trace_log=logs["trace"], eval_log=logs["eval"]))
        return
    if args.handoff_command == "draft":
        text = draft_handoff(ctx, args)
        if args.write:
            if not write_text(docs["handoff"], text, overwrite=args.force):
                raise SystemExit(f"{docs['handoff']} already exists; pass --force to overwrite it")
            print(docs["handoff"])
        else:
            print(text)
        return
    raise SystemExit("handoff command required")


def add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help="Project root. Defaults to nearest parent with .opsflow/config.json.")
    parser.add_argument("--ops-dir", help="Override configured ops directory for this command.")
    parser.add_argument("--config", help="Explicit opsflow config path.")


def add_extra_field_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--field", action="append", type=key_value, help="Extra record field as KEY=VALUE. Repeatable.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic trace/eval/handoff ops helper")
    add_context_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create opsflow config, docs, and ledgers")
    init.add_argument("--name", help="Project name for templates")
    init.add_argument("--owner", default=DEFAULT_OWNER)
    init.add_argument("--ops-dir", dest="init_ops_dir", help="Ops directory to create or reuse")
    init.add_argument("--trace-log", default="trace/trace_log.jsonl")
    init.add_argument("--eval-log", default="evals/eval_log.jsonl")
    init.add_argument("--research-log", default="research/reference_intake.jsonl")
    init.add_argument("--force", action="store_true", help="Overwrite config if it exists")
    init.add_argument("--force-docs", action="store_true", help="Overwrite generated docs/templates; ledgers are never truncated")
    init.set_defaults(func=handle_init)

    paths = sub.add_parser("paths", help="Show resolved config, docs, and ledger paths")
    paths.set_defaults(func=handle_paths)

    add_trace = sub.add_parser("add-trace", help="Append one trace record")
    add_trace.add_argument("--trace-id")
    add_trace.add_argument("--stage", required=True)
    add_trace.add_argument("--kind", required=True)
    add_trace.add_argument("--status", required=True)
    add_trace.add_argument("--title", required=True)
    add_trace.add_argument("--summary", required=True)
    add_trace.add_argument("--hypothesis")
    add_trace.add_argument("--inputs", help="Comma-separated")
    add_trace.add_argument("--artifacts", help="Comma-separated")
    add_trace.add_argument("--metrics")
    add_trace.add_argument("--parent")
    add_trace.add_argument("--next-action")
    add_trace.add_argument("--tags", help="Comma-separated")
    add_trace.add_argument("--owner")
    add_extra_field_option(add_trace)
    add_trace.set_defaults(func=handle_add_trace)

    add_eval = sub.add_parser("add-eval", help="Append one eval record")
    add_eval.add_argument("--eval-id")
    add_eval.add_argument("--run-id", required=True)
    add_eval.add_argument("--parent-run")
    add_eval.add_argument("--status", required=True)
    add_eval.add_argument("--score", type=float)
    add_eval.add_argument("--rank")
    add_eval.add_argument("--runtime")
    add_eval.add_argument("--resource")
    add_eval.add_argument("--summary", required=True)
    add_eval.add_argument("--raw-signal")
    add_eval.add_argument("--confidence")
    add_eval.add_argument("--next-action")
    add_eval.add_argument("--tags", help="Comma-separated")
    add_extra_field_option(add_eval)
    add_eval.set_defaults(func=handle_add_eval)

    add_ref = sub.add_parser("add-ref", help="Append one reference/research intake record")
    add_ref.add_argument("--ref-id")
    add_ref.add_argument("--source-type", required=True)
    add_ref.add_argument("--title", required=True)
    add_ref.add_argument("--org-or-author")
    add_ref.add_argument("--url-or-path")
    add_ref.add_argument("--scenario-match")
    add_ref.add_argument("--status", required=True)
    add_ref.add_argument("--transferable-module")
    add_ref.add_argument("--non-transferable-assumptions")
    add_ref.add_argument("--engineering-cost")
    add_ref.add_argument("--validation-plan")
    add_ref.add_argument("--notes")
    add_ref.add_argument("--related-hypotheses", help="Comma-separated")
    add_ref.add_argument("--tags", help="Comma-separated")
    add_extra_field_option(add_ref)
    add_ref.set_defaults(func=handle_add_ref)

    query = sub.add_parser("query", help="Search records")
    query.add_argument("--keyword")
    query.add_argument("--status")
    query.add_argument("--kind")
    query.add_argument("--tag")
    query.add_argument("--source", default="all", help="trace, eval, research, or all")
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--jsonl", action="store_true", help="Print compact JSONL")
    query.set_defaults(func=handle_query)

    latest = sub.add_parser("latest", help="Show latest records")
    latest.add_argument("--source", required=True, help="trace, eval, research, or all")
    latest.add_argument("--limit", type=int, default=10)
    latest.add_argument("--jsonl", action="store_true", help="Print compact JSONL")
    latest.set_defaults(func=handle_latest)

    handoff = sub.add_parser("handoff", help="Show or draft handoff docs")
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_sub.add_parser("path", help="Print current handoff path").set_defaults(func=handle_handoff)
    handoff_sub.add_parser("show", help="Print current handoff").set_defaults(func=handle_handoff)
    handoff_sub.add_parser("template", help="Print handoff template").set_defaults(func=handle_handoff)
    draft = handoff_sub.add_parser("draft", help="Print a fresh handoff draft seeded with latest record IDs")
    draft.add_argument("--phase")
    draft.add_argument("--owner")
    draft.add_argument("--focus")
    draft.add_argument("--write", action="store_true", help="Write draft to CURRENT_HANDOFF.md")
    draft.add_argument("--force", action="store_true", help="Overwrite CURRENT_HANDOFF.md when used with --write")
    draft.set_defaults(func=handle_handoff)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        sys.exit(1)


if __name__ == "__main__":
    main()
