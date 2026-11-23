# Opsflow

[English](README.md)

Opsflow（`v0.2`）是一个零依赖、纯 Python 标准库的项目运维与记忆 CLI，用来让长期工程或研究项目可以被可靠接手，同时**严格控制每次读取的上下文体积**。

相比不断膨胀的手写全局交接文档，Opsflow 将状态、证据与检索明确分层：
- **有界当前态**：`project.json`、自动生成的 `BRIEF.md`（<= 12 KiB），以及每条并行工作线一张覆盖更新的状态卡 `tracks/<track_id>/track.json`（带 `revision` 乐观并发锁）。
- **安全追加式账本**：sidecar 文件互斥锁、原子落盘、`allow_nan=False`、同 ID 幂等重试、损坏行拒绝写入、可配置的评估证据分层账本，以及超过 16 KiB 结果自动外置与 SHA-256 校验。
- **可重建本地检索**：`.index/ops.sqlite` 增量索引，支持有界摘要 `query` 分页与 JSON Pointer 渐进展开 `show`。

## 它管理什么

- `project.json` 与 `BRIEF.md`：项目正式基线、有效约束与未关闭工作线概览（`opsflow brief --write`，默认 12 KiB 上限）。
- `tracks/<track_id>/track.json`：每条工作线一张覆盖更新的状态卡（`opsflow track set/close/list`、`opsflow context --track <id>`，默认 24 KiB 上限）。
- `trace`：决策、代码/配置/数据变更、事故、阻塞与回滚记录。
- `eval`：轻量 `add-eval` 与严格结构化的 `add-result --file`（默认单账本 `evals/eval_log.jsonl` + 可选 `evidence_layer`，也可在 `.opsflow/config.json` 配置多层独立账本）。
- `research`：论文、技术报告、开源仓库、文档与高价值内部报告摄取。
- `lessons/`：精简可复用规则库（总计 <= 64 KiB），替代不断追加的故障流水账。
- `agent docs`：`AGENTS.md` 作为唯一规则源（<= 8 KiB），`CLAUDE.md` 改为薄委托入口。

## 安装

需要 Python 3.10+（仅依赖标准库，无任何第三方依赖）：

```bash
git clone https://github.com/Tiny-ht/opsflow.git
cd opsflow
python3 -m pip install -e .
```

也可以不安装直接运行：

```bash
python3 -m opsflow.cli --help
```

## 快速开始

初始化工作区（开箱即通过 `opsflow check`）：

```bash
opsflow init --name "My Project"
opsflow brief
```

创建并按乐观锁版本更新一条并行工作线（可从 `ops/templates/track.json` 复制起步）：

```bash
opsflow track set --file track.json
opsflow context --track parser
opsflow track set --file track_update.json --expected-revision 1
```

安全写入 trace、轻量 eval 或严格结构化 result：

```bash
opsflow add-trace --track parser --stage build --kind decision --status completed \
  --title "Switch to stdlib" --summary "Removed external parser dependency." --tags parser
opsflow add-eval --track parser --layer offline --run-id run_001 --status completed \
  --score 0.94 --summary "Offline benchmark passed."
opsflow add-result --file result.json
opsflow add-ref --track parser --source-type paper --title "Bounded Contexts" --status extracted
```

先在字节预算内搜索摘要，再按字段/分页渐进展开：

```bash
opsflow query --keyword benchmark --layer offline --since 2026-11-01 --offset 0
opsflow latest --source all --limit 10
opsflow show <key> --field /metrics --offset 0 --limit 10
opsflow show <key> --detail --field /extra/rows --offset 0 --limit 20
```

完成或否决后显式关闭工作线使其退出 `BRIEF.md`，并做一致性检查：

```bash
opsflow track close parser --conclusion "Merged v2 parser; 2x faster." --expected-revision 2
opsflow brief --write
opsflow check
```

## 可选多层评估账本

默认情况下，所有评估记录写入单账本 `evals/eval_log.jsonl`，并可附带可选的 `evidence_layer` 字段。如果需要将不同证据层物理分账（例如 `dev`/`staging`/`prod` 或 `train`/`backtest`/`live`），可在初始化或 `.opsflow/config.json` 中配置 `eval_layers`：

```bash
opsflow init --name "ML System" \
  --eval-layer offline=evals/offline_eval_log.jsonl \
  --eval-layer staging=evals/staging_eval_log.jsonl \
  --eval-layer prod=evals/prod_eval_log.jsonl
```

启用 `eval_layers` 后，`add-eval` 和 `add-result` 必须提供已配置的证据层并路由至对应账本。`query --source eval` 会跨所有评估层（含旧单账本 `eval_log.jsonl`）检索，而 `query --source prod` 或 `query --layer prod` 可精确隔离单一证据层。

## 结构化 `add-result` 示例

`opsflow add-result --file result.json` 要求每项指标显式提供 `value`、`unit` 与 `aggregation`（拒绝裸数值）；超过 16 KiB 的结果会自动外置到 `tracks/<track_id>/results/<eval_id>.json`（无 `track_id` 时放 `evals/results/`），账本保留带 SHA-256 校验的 `detail_ref`、最多 10 项核心指标与可检索 `tags`：

```json
{
  "track_id": "parser",
  "eval_id": "eval_parser_v2",
  "evidence_layer": "offline",
  "status": "completed",
  "execution_mode": "benchmark",
  "summary": "10k 样本基准测试结果。",
  "metrics": {
    "p99_latency": {"value": 12.4, "unit": "ms", "aggregation": "p99_10k_runs"}
  },
  "artifacts": ["reports/parser_v2.md"],
  "supersedes": ["eval_parser_v1"],
  "tags": ["parser", "benchmark"]
}
```

## 生成的目录结构

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

全局参数（`--root`、`--ops-dir`、`--config`）需放在子命令之前。`init` 绝不截断已有账本；已有的 `AGENTS.md`、`CLAUDE.md` 与 `project.json` 默认保留，除非传入 `--force-docs`。

## 从 v0.1 升级说明

- 旧版 v0.1 的 `.opsflow/config.json` 与异构 JSONL 账本可直接被 `query`、`latest`、`show` 与 `reindex` 兼容读取和增量索引。
- 旧全局叙事文件（`current_state.md`、`handoff/CURRENT_HANDOFF.md`、`trace/failure_patterns.md`）和 `handoff` 子命令在 v0.2 已退役；`opsflow check` 会在检测到这些旧文件时明确报错提示清理。

## 设计原则

- 纯标准库、零外部依赖，便于审计和嵌入各类仓库。
- 默认有界读取（`brief` 12 KiB、`context` 24 KiB、`query` 8 KiB、`show` 16 KiB），显式宣告省略与分页，绝不静默截断结论。
- 并发安全写入：sidecar `.lock`、原子落盘、`allow_nan=False`、工作线 `revision` 乐观锁与账本幂等重试。
- `.index/ops.sqlite` 为可随时重建的本地检索缓存，账本与工作线卡片始终是事实来源。
- 严格区分记录时间、文件观察时间与真实进程存活核验。

## 许可证

MIT
