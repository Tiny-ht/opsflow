# AI Agent Instructions

This repository contains Opsflow, a generic CLI for autonomous project memory.
When changing this repository, keep the tool generic and avoid references to
any specific downstream project.

## Working Rules

- Prefer small, inspectable changes; the package is intentionally dependency-free.
- Run `python3 -m py_compile opsflow/cli.py opsflow/__init__.py` after code edits.
- Update both `README.md` and `README.zh-CN.md` when public behavior changes.
- If you change generated templates, smoke-test `opsflow init` in a temporary directory.
- Do not stage unrelated local files from this workspace.

## Opsflow Behavior To Preserve

- JSONL ledgers are append-only by convention.
- `init` creates missing files but must not truncate existing ledgers.
- Existing `AGENTS.md` and `CLAUDE.md` files in target projects must not be
  overwritten unless the caller passes `--force-docs`.
- Global options such as `--root`, `--ops-dir`, and `--config` are passed before
  the subcommand.

## Useful Checks

```bash
python3 -m py_compile opsflow/cli.py opsflow/__init__.py
python3 -m opsflow.cli --help
python3 -m opsflow.cli --root "$(mktemp -d)" init --name Smoke
```
