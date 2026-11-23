#!/usr/bin/env python3
"""Opsflow v0.2 CLI: bounded project memory, track cards, safe ledgers, and search."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import (
    CONFIG_DIR,
    CONFIG_FILE,
    DEFAULT_OWNER,
    OpsContext,
    default_config,
    load_context,
    load_json_file,
    validate_layer_name,
)
from .index import reindex, search
from .records import add_result, append_record
from .storage import atomic_json, atomic_text, new_id, now_iso
from .templates import (
    template_agents_md,
    template_claude_md,
    template_lessons_readme,
    template_monitor_prompt,
    template_ops_gitignore,
    template_ops_readme,
    template_project_json,
    template_track_json,
)
from .tracks import close_track, get_track, list_tracks, set_track
from . import views


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_value(value: str) -> object:
    text = value.strip()
    if not text:
        return ""
    if text[0] in '[{\"' or text in {"true", "false", "null"}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    try:
        return float(text) if "." in text else int(text)
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


def layer_pair(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected LAYER=RELATIVE_PATH")
    name, rel = raw.split("=", 1)
    name, rel = name.strip(), rel.strip()
    if not rel:
        raise argparse.ArgumentTypeError("eval layer path cannot be empty")
    return validate_layer_name(name), rel


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def _extra_fields(pairs: Sequence[tuple[str, object]] | None) -> dict:
    return {key: value for key, value in (pairs or [])}


def _write_text_if_needed(path: Path, text: str, *, overwrite: bool = False) -> bool:
    if path.exists() and not overwrite:
        return False
    atomic_text(path, text)
    return True


def _write_json_if_needed(path: Path, payload: dict, *, overwrite: bool = False) -> bool:
    if path.exists() and not overwrite:
        return False
    atomic_json(path, payload)
    return True


def _touch_jsonl(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    path.touch()
    return True


def handle_init(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve() if args.root else Path.cwd().resolve()
    config_path = Path(args.config).expanduser().resolve() if args.config else root / CONFIG_DIR / CONFIG_FILE
    eval_layers = dict(args.eval_layer or [])
    cfg = default_config(
        project_name=args.name or root.name,
        ops_dir=args.init_ops_dir or args.ops_dir or "ops",
        trace_log=args.trace_log,
        eval_log=args.eval_log,
        research_log=args.research_log,
        eval_layers=eval_layers,
        owner=args.owner,
        tz=args.timezone or "",
    )
    if config_path.exists() and not args.force:
        cfg = load_json_file(config_path)
        if args.name:
            cfg["project_name"] = args.name
    else:
        atomic_json(config_path, cfg)
    ctx = OpsContext(root=root, config_path=config_path, config=cfg)
    touched: list[str] = []
    ctx.tracks_dir.mkdir(parents=True, exist_ok=True)
    ctx.lessons_dir.mkdir(parents=True, exist_ok=True)
    if _write_json_if_needed(ctx.project_file, template_project_json(ctx), overwrite=args.force_docs):
        touched.append(str(ctx.project_file))
    for path, content in (
        (ctx.ops_dir / "README.md", template_ops_readme(ctx)),
        (ctx.lessons_dir / "README.md", template_lessons_readme()),
        (ctx.ops_dir / "templates" / "monitor_prompt.md", template_monitor_prompt()),
        (ctx.ops_dir / ".gitignore", template_ops_gitignore()),
    ):
        if _write_text_if_needed(path, content, overwrite=args.force_docs):
            touched.append(str(path))
    track_tpl = ctx.ops_dir / "templates" / "track.json"
    if _write_json_if_needed(track_tpl, template_track_json(), overwrite=args.force_docs):
        touched.append(str(track_tpl))
    for path in ctx.all_ledger_paths().values():
        if _touch_jsonl(path):
            touched.append(str(path))
    agent_docs = ctx.agent_doc_paths()
    if "agents" in agent_docs and _write_text_if_needed(agent_docs["agents"], template_agents_md(ctx), overwrite=args.force_docs):
        touched.append(str(agent_docs["agents"]))
    if "claude" in agent_docs and _write_text_if_needed(agent_docs["claude"], template_claude_md(), overwrite=args.force_docs):
        touched.append(str(agent_docs["claude"]))
    if not ctx.brief_file.exists() or args.force_docs:
        views.brief(ctx, write=True)
        touched.append(str(ctx.brief_file))
    print(f"Initialized opsflow at {ctx.ops_dir}")
    print(f"Config: {ctx.config_path}")
    if touched:
        print("Created:")
        for item in touched:
            print(f"- {item}")
    else:
        print("No files created; existing docs and ledgers were left in place.")


def _ctx(args: argparse.Namespace) -> OpsContext:
    return load_context(root=args.root, ops_dir=args.ops_dir, config=args.config)


def handle_paths(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    payload = {
        "root": str(ctx.root),
        "config": str(ctx.config_path),
        "ops_dir": str(ctx.ops_dir),
        "project_file": str(ctx.project_file),
        "brief_file": str(ctx.brief_file),
        "tracks_dir": str(ctx.tracks_dir),
        "lessons_dir": str(ctx.lessons_dir),
        "index_dir": str(ctx.index_dir),
        "logs": {key: str(value) for key, value in ctx.all_ledger_paths().items()},
        "eval_layers": dict(ctx.eval_layers),
        "agent_docs": {key: str(value) for key, value in ctx.agent_doc_paths().items()},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def handle_add_trace(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    record: dict = {
        "trace_id": args.trace_id or new_id("trace"),
        "ts": now_iso(ctx.default_timezone()),
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
        "owner": args.owner or ctx.default_owner(),
    }
    if args.track:
        get_track(ctx, args.track)
        record["track_id"] = args.track
    record.update(_extra_fields(args.field))
    append_record(ctx.trace_log_path, record, "trace_id")
    print(record["trace_id"])


def handle_add_eval(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    layer = validate_layer_name(args.layer) if args.layer else None
    target_log = ctx.eval_log_path(layer)
    record: dict = {
        "eval_id": args.eval_id or new_id(f"{layer}_eval" if layer else "eval"),
        "ts": now_iso(ctx.default_timezone()),
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
    if layer is not None:
        record["evidence_layer"] = layer
    elif ctx.eval_layers and len(ctx.eval_layers) == 1:
        record["evidence_layer"] = next(iter(ctx.eval_layers))
    if args.track:
        get_track(ctx, args.track)
        record["track_id"] = args.track
    record.update(_extra_fields(args.field))
    append_record(target_log, record, "eval_id")
    print(record["eval_id"])


def handle_add_result(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    record = add_result(ctx, load_json_file(Path(args.file)))
    print(record["eval_id"])


def handle_add_ref(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    record: dict = {
        "ref_id": args.ref_id or new_id("ref"),
        "ts": now_iso(ctx.default_timezone()),
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
    if args.track:
        get_track(ctx, args.track)
        record["track_id"] = args.track
    record.update(_extra_fields(args.field))
    append_record(ctx.research_log_path, record, "ref_id")
    print(record["ref_id"])


def handle_track(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    if args.track_action == "list":
        cards = list_tracks(ctx)
        if args.status:
            cards = [c for c in cards if c["status"] == args.status]
        if args.json:
            print(views.fit(views.json_text(cards), ctx.budget("query_bytes")), end="")
            return
        if not cards:
            print("No tracks registered.")
            return
        lines = [
            f"{c['track_id']} | {c['status']} | rev={c['revision']} | {c['title']} "
            f"(next={len(c['next_actions'])}, blockers={len(c['blockers'])})"
            for c in cards
        ]
        print(views.fit("\n".join(lines) + "\n", ctx.budget("query_bytes")), end="")
        return
    if args.track_action == "set":
        card = set_track(ctx, load_json_file(Path(args.file)), args.expected_revision)
    else:
        card = close_track(
            ctx, args.track_id, args.conclusion,
            evidence_refs=args.evidence_ref if args.evidence_ref is not None else None,
            expected_revision=args.expected_revision,
        )
    try:
        views.brief(ctx, write=True)
    except (ValueError, OSError) as exc:
        print(f"track card saved, but BRIEF.md was not refreshed: {exc}", file=sys.stderr)
    out = {"track_id": card["track_id"], "revision": card["revision"], "status": card["status"]}
    print(views.json_text(out), end="")


def handle_query(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    budget = ctx.budget("query_bytes") if args.budget_bytes is None else args.budget_bytes
    result = search(
        ctx,
        keyword=args.keyword or "",
        source=args.source,
        track_id=args.track,
        status=args.status,
        kind=args.kind,
        limit=min(args.limit, 100),
        offset=args.offset,
        evidence_layer=args.layer,
        since=args.since,
        until=args.until,
        tag=args.tag,
    )
    print(views.query_output(result, budget, args.json, args.offset), end="")


def handle_brief(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    print(views.brief(ctx, args.budget_bytes, args.write), end="")


def handle_context(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    print(views.context(ctx, args.track, args.budget_bytes, args.json), end="")


def handle_show(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    print(views.show(ctx, args.key, args.field, args.offset, args.limit, args.budget_bytes, args.detail), end="")


def handle_reindex(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    print(views.fit(views.json_text(reindex(ctx, args.force)), 8192), end="")


def handle_check(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    result = views.check(ctx)
    print(views.fit(views.json_text(result), 16384), end="")
    if not result["ok"]:
        raise SystemExit(1)


def _add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help="Project root. Defaults to nearest parent with .opsflow/config.json.")
    parser.add_argument("--ops-dir", help="Override configured ops directory for this command.")
    parser.add_argument("--config", help="Explicit opsflow config path.")


def _add_extra_field_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--field", action="append", type=key_value, help="Extra record field as KEY=VALUE. Repeatable.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded project-ops CLI for track state, ledgers, and search")
    _add_context_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize opsflow v0.2 config, project baseline, docs, and ledgers")
    init.add_argument("--name", help="Project name for templates")
    init.add_argument("--owner", default=DEFAULT_OWNER)
    init.add_argument("--ops-dir", dest="init_ops_dir", help="Ops directory to create or reuse")
    init.add_argument("--trace-log", default="trace/trace_log.jsonl")
    init.add_argument("--eval-log", default="evals/eval_log.jsonl")
    init.add_argument("--eval-layer", action="append", type=layer_pair,
                      help="Configure separate eval ledger as LAYER=RELATIVE_PATH. Repeatable.")
    init.add_argument("--research-log", default="research/reference_intake.jsonl")
    init.add_argument("--timezone", help="Project default timezone offset, e.g. +08:00 or UTC")
    init.add_argument("--force", action="store_true", help="Overwrite .opsflow/config.json if it exists")
    init.add_argument("--force-docs", action="store_true",
                      help="Overwrite generated docs/templates/project.json; ledgers are never truncated")
    init.set_defaults(func=handle_init)

    paths = sub.add_parser("paths", help="Show resolved config, ops, ledger, and agent doc paths")
    paths.set_defaults(func=handle_paths)

    add_trace = sub.add_parser("add-trace", help="Append one trace record safely")
    add_trace.add_argument("--trace-id")
    add_trace.add_argument("--track", help="Registered track_id to associate with this trace")
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
    _add_extra_field_option(add_trace)
    add_trace.set_defaults(func=handle_add_trace)

    add_eval = sub.add_parser("add-eval", help="Append one lightweight eval record safely")
    add_eval.add_argument("--eval-id")
    add_eval.add_argument("--track", help="Registered track_id to associate with this eval")
    add_eval.add_argument("--layer", "--evidence-layer", dest="layer", help="Evidence layer name")
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
    _add_extra_field_option(add_eval)
    add_eval.set_defaults(func=handle_add_eval)

    add_res = sub.add_parser("add-result", help="Record structured eval result from JSON file (auto-externalizes >16 KiB)")
    add_res.add_argument("--file", required=True)
    add_res.set_defaults(func=handle_add_result)

    add_ref = sub.add_parser("add-ref", help="Append one reference/research intake record safely")
    add_ref.add_argument("--ref-id")
    add_ref.add_argument("--track", help="Registered track_id to associate with this reference")
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
    _add_extra_field_option(add_ref)
    add_ref.set_defaults(func=handle_add_ref)

    track = sub.add_parser("track", help="Manage per-track overwrite-in-place state cards")
    track_sub = track.add_subparsers(dest="track_action", required=True)
    t_set = track_sub.add_parser("set", help="Create or update a track card from JSON file")
    t_set.add_argument("--file", required=True)
    t_set.add_argument("--expected-revision", type=nonnegative)
    t_set.set_defaults(func=handle_track)
    t_close = track_sub.add_parser("close", help="Close a track with an explicit conclusion")
    t_close.add_argument("track_id")
    t_close.add_argument("--conclusion", required=True)
    t_close.add_argument("--expected-revision", type=positive, required=True)
    t_close.add_argument("--evidence-ref", action="append", help="Optional replacement evidence_refs (repeatable)")
    t_close.set_defaults(func=handle_track)
    t_list = track_sub.add_parser("list", help="List registered tracks")
    t_list.add_argument("--status", choices=["planned", "active", "paused", "closed"])
    t_list.add_argument("--json", action="store_true")
    t_list.set_defaults(func=handle_track)

    for name, default_limit in (("query", 20), ("latest", 10)):
        q = sub.add_parser(name, help="Search indexed summaries within a strict byte budget")
        q.add_argument("--keyword")
        q.add_argument("--status")
        q.add_argument("--kind")
        q.add_argument("--track")
        q.add_argument("--layer", "--evidence-layer", dest="layer")
        q.add_argument("--since", help="ISO date or datetime (default project timezone)")
        q.add_argument("--until", help="ISO date (inclusive day) or datetime")
        q.add_argument("--tag")
        q.add_argument("--source", default="all", help="trace, eval, research, track, lesson, project, layer name, or all")
        q.add_argument("--limit", type=positive, default=default_limit)
        q.add_argument("--offset", type=nonnegative, default=0)
        q.add_argument("--budget-bytes", type=positive)
        q.add_argument("--json", action="store_true")
        q.set_defaults(func=handle_query)

    brief_p = sub.add_parser("brief", help="Render or write bounded project BRIEF.md")
    brief_p.add_argument("--write", action="store_true")
    brief_p.add_argument("--budget-bytes", type=positive)
    brief_p.set_defaults(func=handle_brief)

    ctx_p = sub.add_parser("context", help="Render bounded context and status observations for one track")
    ctx_p.add_argument("--track", required=True)
    ctx_p.add_argument("--json", action="store_true")
    ctx_p.add_argument("--budget-bytes", type=positive)
    ctx_p.set_defaults(func=handle_context)

    show_p = sub.add_parser("show", help="Expand one record, field slice, or externalized detail")
    show_p.add_argument("key")
    show_p.add_argument("--field", action="append", help="JSON Pointer, e.g. /metrics")
    show_p.add_argument("--offset", type=nonnegative, default=0)
    show_p.add_argument("--limit", type=positive, default=20)
    show_p.add_argument("--detail", action="store_true", help="Load verified externalized detail_ref")
    show_p.add_argument("--json", action="store_true", help="show always outputs valid JSON")
    show_p.add_argument("--budget-bytes", type=positive)
    show_p.set_defaults(func=handle_show)

    reidx = sub.add_parser("reindex", help="Refresh or force-rebuild local SQLite search index")
    reidx.add_argument("--force", action="store_true")
    reidx.set_defaults(func=handle_reindex)

    chk = sub.add_parser("check", help="Validate startup doc budgets, BRIEF.md freshness, refs, and index")
    chk.set_defaults(func=handle_check)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        sys.exit(1)
    except (OSError, ValueError, KeyError, RuntimeError, IndexError, TypeError) as exc:
        parser.exit(2, f"opsflow: {exc}\n")


if __name__ == "__main__":
    main()
