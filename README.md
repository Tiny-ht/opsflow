# Opsflow

[简体中文](README.zh-CN.md)

Opsflow is a small, dependency-free CLI for keeping long-running project work
resumable. It gives you append-only ledgers for trace, eval, and research
records, plus handoff and current-state templates that make it easier for the
next operator to continue from the real state of the project.

The project name, owner, ops directory, and ledger filenames are configurable,
so the same workflow can fit many different repositories and teams.

## What It Manages

- `trace`: decisions, code changes, failures, blockers, and direction changes
- `eval`: real feedback from tests, users, benchmarks, platforms, or reviews
- `research`: papers, reports, repositories, docs, and reference intake
- `handoff`: current-state and handoff documents for fast context transfer

## Install

For local development:

```bash
git clone https://github.com/Tiny-ht/opsflow.git
cd opsflow
python3 -m pip install -e .
```

You can also run it directly without installing:

```bash
python3 -m opsflow.cli --help
```

## Quick Start

Initialize an ops workspace:

```bash
opsflow init --name "My Project"
```

Add a trace record:

```bash
opsflow add-trace \
  --stage build \
  --kind code_change \
  --status succeeded \
  --title "Add parser" \
  --summary "Parser now handles empty input." \
  --tags parser,smoke
```

Add an eval record:

```bash
opsflow add-eval \
  --run-id run_001 \
  --status succeeded \
  --score 0.91 \
  --summary "Smoke eval passed."
```

Query recent records:

```bash
opsflow latest --source all --limit 5
opsflow query --keyword timeout --source all
```

Draft a handoff:

```bash
opsflow handoff draft --phase build --focus "Parser smoke tests"
```

## Generated Structure

By default, `opsflow init` creates:

```text
.opsflow/config.json
ops/
  README.md
  current_state.md
  roadmap.md
  handoff/
    CURRENT_HANDOFF.md
    HANDOFF_TEMPLATE.md
  trace/
    README.md
    failure_patterns.md
    trace_log.jsonl
  evals/
    README.md
    eval_log.jsonl
    user_signal_template.md
  research/
    README.md
    reference_intake.jsonl
    reference_note_template.md
```

The JSONL ledgers are append-only by convention. `init` creates missing files,
but does not truncate existing logs.

## Initialize An Existing Ops Directory

For a repository that already has an ops folder or custom ledger names, point
Opsflow at those paths:

```bash
opsflow --root /path/to/project init \
  --name "My Existing Project" \
  --ops-dir project_ops \
  --eval-log evals/platform_eval_log.jsonl
```

Global options such as `--root`, `--ops-dir`, and `--config` are passed before
the subcommand.

## Commands

```bash
opsflow paths
opsflow add-trace --stage plan --kind hypothesis --status planned --title "..." --summary "..."
opsflow add-eval --run-id run_002 --status failed --summary "Timed out" --resource timeout
opsflow add-ref --source-type paper --title "..." --status queued --scenario-match medium
opsflow query --keyword timeout --source all
opsflow latest --source trace --limit 10
opsflow handoff show
```

All add commands support repeatable extra fields:

```bash
opsflow add-trace \
  --stage deploy \
  --kind decision \
  --status succeeded \
  --title "Ship candidate" \
  --summary "Candidate passed smoke checks." \
  --field commit=abc123 \
  --field rollout_percent=10
```

## Design Principles

- Keep the tool dependency-free and easy to inspect.
- Prefer structured JSONL records over fragile chat-history memory.
- Keep current state in one place, and history in append-only ledgers.
- Make handoff docs useful even when the next operator has no prior context.

## License

MIT
