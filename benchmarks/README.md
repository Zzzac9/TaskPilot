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

## Real DeepSeek Vision fallback

Phase 11 的真实视觉验收独立于 Phase 12 Primary 指标。它使用真实 Chromium viewport
截图和 `deepseek-flash`，让 DOM role locator 先确定性失败，再验证 HIGH-risk approval、
结构化坐标、坐标点击和页面 side effect：

```powershell
$env:VISION_API_KEY = "your-deepseek-key"
$env:VISION_BASE_URL = "https://api.deepseek.com"
.\.venv-benchmark\Scripts\python.exe -m benchmarks.phase11_real_vision `
  --attempts-per-scene 1
```

默认覆盖 center、top-right、bottom-left 三个带干扰项的 Canvas 场景。每个 attempt 使用
独立 Browser artifact 目录，结果写入新的
`artifacts/phase11-real-vision/<UTC timestamp>/`。成功必须同时满足真实 PNG 输入、审批前
零 Vision 调用、HIGH + require_approval、结构化目标、坐标落入 oracle 矩形、Vision
interaction mode 和页面点击状态改变。

## Production Graph Agent integration

以下命令不使用 scripted model、预编排 Tool Calls 或伪造审批记录。真实 Analyzer、
Planner、Executor、Verifier 和 Finalizer 通过生产 LangGraph 执行两个自然语言任务：

1. Browser 读取网页，再由 Filesystem MCP 生成 Word；外部解析 OOXML 内容和来源。
2. Agent 自主选择 Canvas 目标的 `browser_click`，Graph 在 Vision fallback 前真实中断；
   操作者检查冻结动作并输入 `APPROVE`，随后外部 fixture server 验证实际点击。

```powershell
$env:LLM_MODEL = "deepseek-flash"
$env:VISION_API_KEY = $env:LLM_API_KEY
$env:VISION_BASE_URL = $env:LLM_BASE_URL
$env:VISION_MODEL = "deepseek-flash"
.\.venv-benchmark\Scripts\python.exe -m benchmarks.real_agent_integration `
  --task all --timeout-seconds 300
```

失败诊断时可使用 `--task word` 或 `--task vision`，但最终报告应以 `--task all` 为准。
2026-10-17 的最终统一运行结果为 2/2、False Completion 0；原始结果位于忽略目录
`artifacts/real-agent-integration/20261017T082051Z/`。

## Final Phase 12 status

Real LLM smoke pipeline 已通过基础设施 gate；Primary、No-Replan、stability attempts 2/3
与 Runtime Reliability 均已执行。最终 source-of-truth 汇总为：

```text
benchmarks/results/20260902T043156Z-1da399b-evaluation/evaluation-summary.json
```

Primary 为 8/30，False Completion 为 11/30。当时的 Phase 12 source run 中 external
Vision-model ablation 为 `NOT_RUN`；后续独立的 Phase 11 Vision 和生产 Graph 集成验收不
回填或改写这组历史 Primary 指标。没有把 deterministic Vision targeter 或 pytest 结果
计入 Primary Task Success Rate，也没有声称 token reduction。
