# AI Agent Instructions

This repository contains **Opsflow**, a generic, dependency-free CLI for bounded
project memory. Keep the tool generic and avoid references to any downstream
project.

## Working Rules

- Keep changes small, inspectable, and strictly Python standard library only.
- Run the test suite after edits:
  ```bash
  python3 -m unittest discover -s tests -v
  ```
- Keep this file under 8 KiB; `CLAUDE.md` delegates here instead of duplicating rules.
- Update both `README.md` and `README.zh-CN.md` when public behavior changes.
- Smoke-test `opsflow init` and `opsflow check` in a temporary directory when changing templates.
- Do not stage unrelated local files.

## Opsflow Invariants To Preserve

- Ledgers are append-only with sidecar locking, atomic replacement, `allow_nan=False`,
  idempotent retries, and corruption refusal.
- `init` creates missing files and never truncates existing ledgers.
- Existing `AGENTS.md`, `CLAUDE.md`, and `project.json` in target projects are not
  overwritten unless `--force-docs` is passed.
- Global options (`--root`, `--ops-dir`, `--config`) precede the subcommand.
- Output views (`brief`, `context`, `query`, `show`) enforce UTF-8 byte budgets without
  silently truncating conclusions.
- `.index/ops.sqlite` is rebuildable local cache; ledgers and track cards remain source of truth.

## Useful Checks

```bash
python3 -m unittest discover -s tests -v
python3 -m opsflow.cli --help
tmp="$(mktemp -d)" && python3 -m opsflow.cli --root "$tmp" init --name Smoke && python3 -m opsflow.cli --root "$tmp" check
```
