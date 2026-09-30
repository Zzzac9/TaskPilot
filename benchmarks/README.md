# TaskPilot Benchmarks

Phase 12 将评价层与 `src/taskpilot/` 生产 Runtime 分离。Primary E2E 使用真实
Analyzer、Planner、Function Calling Executor、Verifier 与 Replanner；Runtime
Reliability/Safety suite 才允许 deterministic model 和 fault injection。两套结果不会
混入同一个 Task Success Rate。

## Clean environment

```powershell
D:\Anaconda\envs\ai\python.exe -m venv .venv-benchmark
.\.venv-benchmark\Scripts\python.exe -m pip install --upgrade pip
.\.venv-benchmark\Scripts\python.exe -m pip install -e .
.\.venv-benchmark\Scripts\python.exe -m playwright install chromium
.\.venv-benchmark\Scripts\python.exe -m pip check
```

正式 run 必须看到 `No broken requirements found.`。共享 `ai` conda 环境不用于产生
Benchmark 或简历数字。

## Fixed task set

[`manifest.json`](manifest.json) 固定声明 30 题：

- MCP: 6
- Browser DOM: 8
- Form Interaction: 4
- Constraint Aggregation: 4
- Replanning: 4
- HITL: 4

网页全部来自真实 localhost fixture；MCP 题通过官方 SDK 连接真实 STDIO subprocess。
Success 同时要求 `TaskStatus.COMPLETED` 和 independent external oracle 通过。

## Commands

先配置真实 LLM（命令不会输出 key），再执行三题 Gate：

```powershell
$env:LLM_API_KEY = "..."
$env:LLM_BASE_URL = "https://provider.example/v1"
$env:LLM_MODEL = "model-name"
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner smoke
```

只有 smoke 通过后才能运行：

```powershell
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner primary
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner no-replan
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner stability-2
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner stability-3
```

独立 reliability suite：

```powershell
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.reliability
```

所有 source runs 完成后，可校验 run 关系并生成新的只读汇总 artifact：

```powershell
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.finalize `
  --primary benchmarks/results/20260902T040441Z-1da399b-primary `
  --no-replan benchmarks/results/20260902T041621Z-1da399b-no-replan `
  --stability-2 benchmarks/results/20260902T041758Z-1da399b-stability-2 `
  --stability-3 benchmarks/results/20260902T042251Z-1da399b-stability-3 `
  --reliability benchmarks/results/20260902T042822Z-1da399b-reliability
```

每次运行写入新的 `results/<timestamp>-<git-sha>-<label>/`，不会覆盖旧 raw artifacts。
Primary artifacts 包含 `config.json`、`environment.json`、`tasks.json`、`runs.jsonl`、
`summary.json`、`summary.csv`、`report.md`、`pip-freeze.txt` 与 `pip-check.txt`。

## Metrics

- Task Success Rate + 95% Wilson interval
- False Completion Rate
- Tool Call Count / Tool Failure Rate
- Replan Trigger / Recovery Rate
- Step Rejection / Approval Request Rate
- Mean / Median / P95 Wall Latency
- Category Success Rate
- Runtime crash recovery and duplicate side-effect counters
- Character-context reduction（只在 context-stress case 中；不称为 token reduction）

Token usage 只有 provider 返回真实 usage metadata 时才记录，否则为 `null`。
Fake Vision 只属于 reliability suite，不能用于宣称真实 Vision model 提升。

## Final Phase 12 status

Real LLM smoke pipeline 已通过基础设施 gate；Primary、No-Replan、stability attempts 2/3
与 Runtime Reliability 均已执行。最终 source-of-truth 汇总为：

```text
benchmarks/results/20260902T043156Z-1da399b-evaluation/evaluation-summary.json
```

Primary 为 8/30，False Completion 为 11/30。真实 external Vision-model ablation =
`NOT_RUN`；没有把 deterministic Vision targeter 或 pytest 结果计入 Primary Task Success
Rate，也没有声称 token reduction。
