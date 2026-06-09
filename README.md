# Opsflow

Opsflow is a small, dependency-free CLI for project operations that need to
survive long handoffs:

- append-only `trace` records for decisions, changes, failures, and blockers
- structured `eval` records for real test, user, benchmark, or platform feedback
- `research` intake records for papers, reports, repos, docs, and reference notes
- handoff/current-state templates so the next operator can resume quickly

It is extracted from the TAAC ops pattern, but the paths, project name, owner,
and ledger filenames are configurable.

## Quick Start

```bash
python3 -m pip install -e .
opsflow init --name "My Project"
opsflow add-trace \
  --stage build \
  --kind code_change \
  --status succeeded \
  --title "Add parser" \
  --summary "Parser now handles empty input." \
  --tags parser,smoke
opsflow add-eval \
  --run-id run_001 \
  --status succeeded \
  --score 0.91 \
  --summary "Smoke eval passed."
opsflow latest --source all --limit 5
opsflow handoff draft
```

You can also run it without installing:

```bash
python3 -m opsflow.cli init --name "My Project"
python3 -m opsflow.cli add-trace \
  --stage build \
  --kind code_change \
  --status succeeded \
  --title "Add parser" \
  --summary "Parser now handles empty input." \
  --tags parser,smoke
python3 -m opsflow.cli add-eval \
  --run-id run_001 \
  --status succeeded \
  --score 0.91 \
  --summary "Smoke eval passed."
python3 -m opsflow.cli latest --source all --limit 5
python3 -m opsflow.cli handoff draft
```

After installing the package, use `opsflow` instead of `python3 -m opsflow.cli`.

## Initialize An Existing TAAC-Style Directory

For a repo that already has a TAAC-like ops folder and an `online_eval_log.jsonl`
name, point Opsflow at those paths:

```bash
python3 -m opsflow.cli --root /Users/xiaoxiaoxiaohoutian/Desktop/taac init \
  --name TAAC2026 \
  --ops-dir taac2026_ops \
  --eval-log evals/online_eval_log.jsonl
```

The init command creates missing docs and ledgers, but it does not truncate
existing JSONL logs.

## Commands

```bash
python3 -m opsflow.cli paths
python3 -m opsflow.cli add-trace --stage plan --kind hypothesis --status planned --title "..." --summary "..."
python3 -m opsflow.cli add-eval --run-id run_002 --status failed --summary "Timed out" --resource timeout
python3 -m opsflow.cli add-ref --source-type paper --title "..." --status queued --scenario-match medium
python3 -m opsflow.cli query --keyword timeout --source all
python3 -m opsflow.cli latest --source trace --limit 10
python3 -m opsflow.cli handoff show
```

All add commands support repeatable extra fields:

```bash
python3 -m opsflow.cli add-trace \
  --stage deploy \
  --kind decision \
  --status succeeded \
  --title "Ship candidate" \
  --summary "Candidate passed smoke checks." \
  --field commit=abc123 \
  --field rollout_percent=10
```
