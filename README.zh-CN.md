# Opsflow

[English](README.md)

Opsflow 是一个小型、零依赖的项目运维 CLI，用来让长期项目可以被可靠接手和恢复。它提供追加式的 `trace`、`eval`、`research` 账本，以及 handoff / current-state 文档模板，让下一个接手的人能从真实状态继续工作，而不是从聊天记录里猜。

项目名、owner、ops 目录、账本文件名都可以配置，因此同一套工作流可以适配不同仓库和团队。

## 它管理什么

- `trace`：决策、代码改动、失败、blocker、方向变化
- `eval`：测试、用户反馈、benchmark、平台评测、review 等真实反馈
- `research`：论文、技术报告、开源仓库、文档、参考资料摄取
- `handoff`：当前状态和交接文档，帮助快速转移上下文

## 安装

本地开发安装：

```bash
git clone https://github.com/Tiny-ht/opsflow.git
cd opsflow
python3 -m pip install -e .
```

也可以不安装，直接运行：

```bash
python3 -m opsflow.cli --help
```

## 快速开始

初始化一个 ops 工作区：

```bash
opsflow init --name "My Project"
```

追加一条 trace 记录：

```bash
opsflow add-trace \
  --stage build \
  --kind code_change \
  --status succeeded \
  --title "Add parser" \
  --summary "Parser now handles empty input." \
  --tags parser,smoke
```

追加一条 eval 记录：

```bash
opsflow add-eval \
  --run-id run_001 \
  --status succeeded \
  --score 0.91 \
  --summary "Smoke eval passed."
```

查询最近记录：

```bash
opsflow latest --source all --limit 5
opsflow query --keyword timeout --source all
```

生成一份 handoff 草稿：

```bash
opsflow handoff draft --phase build --focus "Parser smoke tests"
```

## 生成的目录结构

默认情况下，`opsflow init` 会创建：

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

JSONL 账本按约定是 append-only。`init` 会创建缺失文件，但不会清空已有日志。

## 初始化已有的 ops 目录

如果某个仓库已经有 ops 目录，或者想使用自定义账本文件名，可以直接指向这些路径：

```bash
opsflow --root /path/to/project init \
  --name "My Existing Project" \
  --ops-dir project_ops \
  --eval-log evals/platform_eval_log.jsonl
```

`--root`、`--ops-dir`、`--config` 这类全局参数需要放在子命令前面。

## 常用命令

```bash
opsflow paths
opsflow add-trace --stage plan --kind hypothesis --status planned --title "..." --summary "..."
opsflow add-eval --run-id run_002 --status failed --summary "Timed out" --resource timeout
opsflow add-ref --source-type paper --title "..." --status queued --scenario-match medium
opsflow query --keyword timeout --source all
opsflow latest --source trace --limit 10
opsflow handoff show
```

所有 add 命令都支持重复传入额外字段：

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

## 设计原则

- 保持零依赖，容易审计。
- 用结构化 JSONL 记录替代脆弱的聊天历史记忆。
- 当前态只放一个地方，历史记录放追加式账本。
- 让 handoff 文档即使脱离原始聊天上下文也能直接接手。

## 许可证

MIT
