# Opsflow

[简体中文](README.zh-CN.md)

Opsflow (`v0.2`) is a small, zero-dependency Python CLI for keeping long-running
engineering and research projects resumable **without flooding agent context
windows**.

Instead of appending to ever-growing global handoff files, Opsflow separates:
- **Bounded current state**: `project.json`, generated `BRIEF.md` (<= 12 KiB),
  and one overwrite-in-place `tracks/<track_id>/track.json` card per parallel
  workstream (with `revision` optimistic locking).
- **Safe append-only ledgers**: sidecar file locking, atomic replacement,
  `allow_nan=False`, idempotent retries, corruption refusal, optional multi-layer
  evaluation routing, and automatic externalization for results > 16 KiB.
- **Rebuildable local search**: `.index/ops.sqlite` incremental indexing with
  budgeted `query` summary cards and progressive JSON Pointer `show` expansion.

## What It Manages

- `project.json` & `BRIEF.md`: official baseline, active constraints, and
  bounded open-track overview (`opsflow brief --write`, default 12 KiB).
- `tracks/<track_id>/track.json`: one state card per parallel workstream
  (`opsflow track set/close/list`, `opsflow context --track <id>`, default 24 KiB).
- `trace`: decisions, code/config/data changes, incidents, blockers, and rollbacks.
- `eval`: lightweight `add-eval` and strict structured `add-result --file`
  (single `evals/eval_log.jsonl` by default, or configurable per-layer ledgers
  via `eval_layers` in `.opsflow/config.json`).
- `research`: papers, technical reports, repositories, docs, and internal notes.
- `lessons/`: concise reusable rules (<= 64 KiB total), replacing append-only
  failure logs.
- `agent docs`: single-source `AGENTS.md` (<= 8 KiB) + thin delegating `CLAUDE.md`.

## Install

Requires Python 3.10+ (standard library only, zero runtime dependencies):

```bash
git clone https://github.com/Tiny-ht/opsflow.git
cd opsflow
python3 -m pip install -e .
```

Or run directly without installing:

```bash
python3 -m opsflow.cli --help
```

## Quick Start

Initialize a workspace (passes `opsflow check` out of the box):

```bash
opsflow init --name "My Project"
opsflow brief
```

Create and update a parallel work track (copy `ops/templates/track.json` as a starter):

```bash
opsflow track set --file track.json
opsflow context --track parser
opsflow track set --file track_update.json --expected-revision 1
```

Record traces, lightweight evals, or strict structured results:

```bash
opsflow add-trace --track parser --stage build --kind decision --status completed \
  --title "Switch to stdlib" --summary "Removed external parser dependency." --tags parser
opsflow add-eval --track parser --layer offline --run-id run_001 --status completed \
  --score 0.94 --summary "Offline benchmark passed."
opsflow add-result --file result.json
opsflow add-ref --track parser --source-type paper --title "Bounded Contexts" --status extracted
```

Search summaries within budget and drill down progressively:

```bash
opsflow query --keyword benchmark --layer offline --since 2026-11-01 --offset 0
opsflow latest --source all --limit 10
opsflow show <key> --field /metrics --offset 0 --limit 10
opsflow show <key> --detail --field /extra/rows --offset 0 --limit 20
```

Close finished or rejected tracks so they exit `BRIEF.md`, then verify:

```bash
opsflow track close parser --conclusion "Merged v2 parser; 2x faster." --expected-revision 2
opsflow brief --write
opsflow check
```

## Multi-Layer Evaluation Ledgers (Optional)

By default, all evals are written to `evals/eval_log.jsonl` with an optional
`evidence_layer` field. Projects that need hard separation between evidence
tiers (such as `dev` / `staging` / `prod` or `train` / `backtest` / `live`) can
configure separate ledgers at init time or in `.opsflow/config.json`:

```bash
opsflow init --name "ML System" \
  --eval-layer offline=evals/offline_eval_log.jsonl \
  --eval-layer staging=evals/staging_eval_log.jsonl \
  --eval-layer prod=evals/prod_eval_log.jsonl
```

When `eval_layers` is configured, `add-eval` and `add-result` require a valid
configured layer and route records into that layer's ledger. `query --source eval`
searches across all eval layers (including any legacy `eval_log.jsonl`), while
`query --source prod` or `query --layer prod` isolates one tier.

## Structured `add-result` Schema

`opsflow add-result --file result.json` enforces explicit units and aggregation
on every metric (no bare numbers) and automatically externalizes records larger
than 16 KiB to `tracks/<track_id>/results/<eval_id>.json` (or `evals/results/`)
with a SHA-256 `detail_ref`:

```json
{
  "track_id": "parser",
  "eval_id": "eval_parser_v2",
  "evidence_layer": "offline",
  "status": "completed",
  "execution_mode": "benchmark",
  "summary": "Benchmark on 10k fixtures.",
  "metrics": {
    "p99_latency": {"value": 12.4, "unit": "ms", "aggregation": "p99_10k_runs"}
  },
  "artifacts": ["reports/parser_v2.md"],
  "supersedes": ["eval_parser_v1"],
  "tags": ["parser", "benchmark"]
}
```

## Generated Structure

```text
.opsflow/config.json
AGENTS.md
CLAUDE.md
ops/
  .gitignore
  README.md
  project.json
  BRIEF.md
  tracks/
  lessons/
    README.md
  templates/
    track.json
    monitor_prompt.md
  trace/
    trace_log.jsonl
  evals/
    eval_log.jsonl
  research/
    reference_intake.jsonl
```

Global options (`--root`, `--ops-dir`, `--config`) precede the subcommand.
Existing ledgers are never truncated; existing `AGENTS.md`, `CLAUDE.md`, and
`project.json` are preserved unless `--force-docs` is passed.

## Upgrading From v0.1

- Legacy v0.1 `.opsflow/config.json` and heterogeneous JSONL ledgers remain
  directly readable and indexable by `query`, `latest`, `show`, and `reindex`.
- Global handoff files (`current_state.md`, `handoff/CURRENT_HANDOFF.md`,
  `trace/failure_patterns.md`) and the `handoff` subcommand are retired in v0.2.
  `opsflow check` flags any retired global files still present in `ops/`.

## Design Principles

- Zero third-party dependencies; easy to audit and embed anywhere.
- Bounded reads by default (`brief` 12 KiB, `context` 24 KiB, `query` 8 KiB, `show` 16 KiB) with explicit omission/pagination instead of silent truncation.
- Concurrent-safe writes: sidecar `.lock`, atomic replace, `allow_nan=False`, optimistic `revision` checks on tracks, and idempotent ledger retries.
- Rebuildable local index (`.index/ops.sqlite`) never replaces ledgers or track cards as source of truth.
- Separate record time, file observation time, and real process liveness verification.

## License

MIT
