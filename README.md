# TaskPilot

TaskPilot 是一个面向 Web 与本地环境的通用任务执行 Agent。

核心规划架构为：

```text
Planner → Executor → Verifier
```

当前已完成：

### Phase 1

- Task models
- Task State
- LangGraph skeleton

### Phase 2

- LLM-based Task Analyzer
- Structured TaskSpec generation
- Completion Criteria extraction

### Phase 3

- Initial Planner
- Structured TaskPlan
- Step dependencies
- Step success criteria
- Basic plan validation

### Phase 4

- Executor Runtime
- Native Function Calling loop
- Tool abstraction and ToolRegistry
- ToolCallRecord integration
- Explicit `finish_step` control
- StepExecutionOutcome
- Bounded actions per step

### Phase 5

- Step-level Verification and criterion-level checks
- Verifier feedback loop with bounded Step retries
- Verified `StepResult` persistence
- Cross-step result handoff
- Dependency-aware Step advancement
- Task-level Verification and verified Task completion

### Phase 6

- Unified async Tool Protocol, ToolRegistry and TaskExecutor
- LangGraph async execution through `graph.ainvoke(...)`
- Official MCP Python SDK v2 integration
- Dynamic MCP `list_tools()` discovery with pagination
- Namespaced MCP Tool adapters
- Long-lived STDIO and Streamable HTTP connection lifecycles
- JSON-serializable MCP result normalization and `is_error` handling
- Real local STDIO MCP discovery/call/Executor integration tests

### Phase 7

- Stateful Playwright Chromium `BrowserSession`
- DOM/accessibility-first Browser Tools with semantic Locators
- Stable page IDs for tabs and popups
- Form, iframe, download and screenshot support
- Shared Browser / MCP / Local `ToolRegistry`
- Real Chromium integration, Executor recovery and full Graph smoke tests

### Phase 8

- Deterministic LOW / MEDIUM / HIGH Tool Risk Policy
- Unified Browser / MCP / Local Tool policy gate
- LangGraph `interrupt` + `Command(resume=...)` Human Approval
- Frozen exact Tool action approval and explicit rejection feedback
- Redacted human-facing argument preview
- In-memory HITL checkpointing with `MemorySaver`

### Phase 9

- Durable SQLite LangGraph checkpoints and cross-process task resume
- Action-level Graph execution boundary: one model decision or one external Tool per node
- Independent durable SQLite Action Journal (`STARTED` / `SUCCEEDED` / `FAILED`)
- Confirmed terminal-action materialization without duplicate Tool invocation
- Explicit `replay_safe` recovery policy for Browser, MCP and Local Tools
- Human recovery review for ambiguous, non-replay-safe side effects
- Durable Human Approval across checkpointer and Graph reconstruction
- Host-side reconstruction of Browser/MCP/ToolRegistry/Executor runtime resources

### Phase 10

- Bounded Dynamic Replanning after Step-attempt exhaustion or Task verification rejection
- Immutable historical PlanSteps with globally fresh Step IDs and explicit plan revisions
- Preservation and cross-revision reuse of verified `StepResult` facts
- Deterministic Context Builder with character budgets and compact recent observations
- Independent durable structured Trace in `.taskpilot/traces.sqlite3`
- Trace aggregation for later benchmark metrics without fabricating token usage

### Phase 11

- DOM-first Browser click with optional Vision fallback
- Independent vision-capable multimodal targeter and deterministic coordinate checks
- HIGH-risk Human Approval before every click that requires Vision fallback
- Vision audit events without screenshot/base64 persistence
- Shared `TaskPilotService` used by CLI and the thin FastAPI interface
- Durable create/status/resume/trace-summary HTTP endpoints

### Phase 12

- Fixed 30-task localhost benchmark manifest with real MCP STDIO and Chromium paths
- Real production Analyzer / Planner / Executor / Verifier / Replanner evaluation
- Independent external oracle and strict `COMPLETED AND oracle_passed` success rule
- Immutable raw JSONL, environment snapshots and deterministic metric aggregation
- Full vs No-Replan ablation and three-attempt stability subset
- Runtime Reliability/Safety fault-injection suite
- Production Service Graph recursion safety fuse configurable by the Host

生产 `TaskExecutor` 当前状态图为：

```text
START → initialize_task → analyze_task → plan_task → begin_step → decide_action
  ├ finish_step → build_step_outcome → verify_step
  └ tool → assess_policy
       ├ deny → policy_feedback → decide_action
       ├ approval → human_approval [interrupt]
       │    ├ reject → rejection_feedback → decide_action
       │    └ approve → execute_tool_action
       └ allow → execute_tool_action
                    ├ terminal/replay-safe → decide_action
                    └ ambiguous unsafe → recovery_review [interrupt]
                         ├ retry → execute_tool_action
                         └ abort → END

verify_step ├ rejected, attempts remain → reset_step_attempt → decide_action
            ├ rejected, attempts exhausted → replan_task → begin_step
            └ verified → advance_step → decide_action / verify_task

verify_task ├ completed → generate and persist final_answer → END
            └ rejected → replan_task → begin_step
```

Tool 调用成功、Step 成功和 Task 成功是三层不同语义。`finish_step`
只是 Executor 的完成声明；Step 只有通过 Step Verifier 才会生成
`StepResult`。即使所有 Step 都完成，Task 也只有通过 Task Verifier 才会变为
`completed`。

`tool_calls` 保存原始工具执行记录，`step_results` 保存通过步骤验证的正式产物，
`task_results` 继续预留给与最终用户目标直接相关的任务级结构化结果，三者不会混用。
Task Verifier 通过后，独立 Final Answer Writer 只使用已验证的 `step_results`、
`task_results` 与 verification 生成面向用户的 Markdown 答案，并将 `final_answer`
持久化到 checkpoint；CLI 恢复已完成任务时不会重复调用模型。

Phase 10 继续严格区分三类信息：State 保存可恢复的任务事实；Context 是每次模型调用
前从 State 按优先级动态选择出的输入子集；Trace 是独立于 checkpoint 的审计与评测
事件。即 `State != Context != Trace`。当前 Context 使用 deterministic character budget，
它是体积保护而不是模型 tokenizer 的精确 token budget；核心 Goal、Constraints 与当前
Step 若自身超限会明确报错，不会为保留旧 Tool history 而静默截断用户目标。

仍未实现：

- Real Search
- Production Filesystem Tool
- Public-web production benchmark
- LLM-based context summarization / vector memory
- OpenTelemetry exporter / distributed tracing backend
- Authentication / Authorization / background workers
- Vision fallback for fill, select, check, drag or desktop automation
- Real external Vision model benchmark validation

## Phase 12 Benchmark-verified 结果

以下结果来自 `deepseek-v4-flash`、固定 `temperature=0`、真实生产组件和 localhost
fixtures；它们不代表公开互联网或生产流量表现：

- Primary：30/30 题实际执行，8 成功、22 失败，Task Success Rate 为 26.67%，Wilson
  95% CI 为 14.18%–44.45%。
- False Completion：11/30；内部 Verifier completed 不会替代 external oracle。
- Replanning ablation：固定 4 题中 Full 为 1/4，No-Replan 为 0/4，差值 +25 percentage
  points；样本很小，不外推为普遍提升。
- Stability：10 题各执行三次，仅 1 题三次全成功，2 题结果发生波动。
- Runtime Reliability/Safety：9/9 个预定义 mechanism cases 通过；观测到 0 次重复
  unsafe side effect、0 次 terminal journal dedup failure、0 次 approval 前未授权副作用。

原始来源和计算见 `benchmarks/results/20260902T043156Z-1da399b-evaluation/`。真实外部
Vision-model ablation 为 **NOT RUN**；没有 token reduction claim。

## 本地运行

项目要求 Python 3.11 或更高版本。

```powershell
python -m pip install -e .
python -m playwright install chromium
pytest
$env:LLM_API_KEY = "your-api-key"
$env:LLM_BASE_URL = "https://your-openai-compatible-endpoint/v1"
$env:LLM_MODEL = "your-model"
python -m taskpilot
python -m taskpilot.cli "找3家香港岛月费低于500港币的健身房并生成CSV"
python -m taskpilot.cli --interactive --browser --headed
python -m taskpilot.cli --resume TASK_ID
python -m taskpilot.cli --resume TASK_ID --state-dir .taskpilot
```

`python -m taskpilot` 是默认便捷入口，等价于
`python -m taskpilot.cli --interactive --browser --headed`：启动常驻交互 Shell，并默认
显示 TaskPilot ASCII 启动 Banner、当前 `LLM_MODEL`，并打开可见 Chromium。启动信息绝不
显示 API Key。MCP Server 仍需通过 `--mcp-config` 提供具体配置，Vision 仍遵循
Host 环境变量与独立模型配置，不会在缺少配置时假装启用。

交互模式启动后可在 `taskpilot>` 提示符连续输入自然语言任务，使用 `/help` 查看帮助，
使用 `/exit` 或 `/quit` 退出。Browser、MCP 与 state-dir 参数会沿用到每次输入；每次输入
仍创建独立 task 和 checkpoint，不会自动继承上一条任务的对话上下文。

CLI 会运行当前 Graph，但生产环境尚未注册真实 Executor Tool。即使 Analyzer 与
Planner 可用，执行层也会明确返回“没有注册任何外部工具”，不会伪造搜索结果或
声称步骤或任务已经完成。默认 CLI 还需要配置兼容 OpenAI 接口的 Analyzer、
Planner 与 Verifier 模型；项目测试全部使用 Fake，离线执行且不需要 API Key。
Analyzer、Planner、Verifier 与 Replanner 的结构化输出若发生 JSON 解析或 schema
校验错误，会携带明确的转义提示最多重试 3 次；鉴权、网络等非解析错误不会盲目重试。

Phase 6 的 Tool Runtime 是统一 async 接口。Host 使用
`MCPToolProvider → ToolRegistry → TaskExecutor → build_graph()` 连接 MCP，MCP
没有被做成 LangGraph node。配置 MCP 时可传入 JSON：

```json
{
  "servers": [
    {
      "server_id": "local-tools",
      "transport": "stdio",
      "command": "python",
      "args": ["path/to/server.py"],
      "env": {}
    },
    {
      "server_id": "remote-tools",
      "transport": "streamable_http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  ]
}
```

```powershell
python -m taskpilot.cli "执行任务" --mcp-config mcp.json
```

STDIO 只额外传入配置文件显式声明的 `env`；不会把 `LLM_API_KEY` 无脑发送给
第三方 MCP Server。当前没有自动重连、HTTP auth policy 或 HITL 风险策略。

Phase 7 可由 Host 启用长生命周期 Chromium；Browser 不是 LangGraph node：

```powershell
python -m taskpilot.cli "在网页中执行任务" --browser
python -m taskpilot.cli "组合浏览器与 MCP 工具" --browser --mcp-config mcp.json
python -m taskpilot.cli "显示浏览器窗口" --browser --headed
```

Browser Tool 只接受 HTTP(S) 导航，操作优先使用 role、label、test id 等语义
Locator；CSS 仅作 fallback，不提供任意 JavaScript 执行工具。下载与截图只能写入
配置的受控目录。`browser_press` 可在严格语义 Locator 上发送单个 Playwright 按键（如
在搜索框填写后按 `Enter`）。搜索语义的 GET 表单 Enter 会自动执行；非搜索或非 GET
表单的 Enter 仍会进入 HIGH 风险人工审批。
`browser_click` 始终先走 DOM/ARIA/Playwright Locator；定位预检失败会拒绝本次动作并
反馈给 Executor 选择新 Locator、键盘操作或直接 URL，不会直接终止整个任务。只有明确的
定位类失败、Host 显式启用 Vision 且配置独立 vision-capable model 时，才在当前
viewport screenshot 上解析目标中心点。DOM 成功时不会截图、不会调用 Vision model。
Phase 11 不为 fill/select/check/extract/drag 提供视觉 fallback。

Vision screenshot 只存在于本次 Tool runtime 内存，不写入 TaskState、checkpoint、
Action Journal 或 Trace。Trace 仅保存 viewport 尺寸、图片字节数、confidence 与原因。
需要 Vision fallback 的 click 一律按 HIGH 风险进入 Human Approval；人工审批的仍是冻结
的 semantic Locator 参数，不是易过期的 `{x, y}`。获批执行时会基于当前 viewport 重新
尝试 DOM-first，再按需运行 Vision。`browser_click` 继续是 `replay_safe=False`。

独立 Vision provider 可由 Host 环境变量启用：

```powershell
$env:TASKPILOT_VISION_ENABLED = "true"
$env:VISION_API_KEY = "your-vision-provider-key"
$env:VISION_BASE_URL = "https://your-openai-compatible-vision-endpoint/v1"
$env:VISION_MODEL = "your-vision-capable-model"
python -m taskpilot.cli "点击当前页面的 Canvas Action" --browser
```

默认离线测试使用 deterministic Fake Vision targeter 加真实本地 Chromium canvas；真实
外部多模态 provider 测试必须显式设置 `TASKPILOT_RUN_VISION_INTEGRATION=1`。当前实现
不代表真实视觉模型已经完成 benchmark，也没有完整解决网页 Prompt Injection；截图中
的网页文字只作为 untrusted environment data 交给最小视觉定位 prompt。

当前 Browser Executor 已为检测到的高风险 Tool action 增加人工审批，但这不是完整
生产安全保证，不能据此声称适合任意生产站点的外部写操作。

Phase 8 的默认 Policy 让 LOW/MEDIUM 动作自动执行，HIGH 动作必须通过真实
LangGraph interrupt 获批。Browser form submit、删除/发送/支付/发布等语义会进入
HIGH；未知 Local/MCP Tool 也保守按 HIGH 处理。MCP annotations 只有在对应 Server
配置 `trust_tool_annotations=true` 时才作为风险提示，annotations 是 hints，不能保证
远端 Tool 行为真实或安全。

Phase 9/10 默认把 Graph checkpoint 写入 `.taskpilot/checkpoints.sqlite3`，把外部动作
Journal 写入 `.taskpilot/actions.sqlite3`，把结构化 Trace 写入
`.taskpilot/traces.sqlite3`。恢复时 `thread_id` 继续等于原 `task_id`；
Host 会重新创建 ToolRegistry、LLM client、Browser/MCP provider 和 Executor，而不会把
这些 live runtime object 序列化进 checkpoint。

`TaskPilotService` 默认把顶层 LangGraph `recursion_limit` 配置为 500，Host 可显式调整，
非法的 0 或负数会 fail fast。它只是阻止无界 Graph supersteps 的最外围 safety fuse：
**Graph recursion limit != Agent semantic retry limit**。Executor 每步动作数、Step retry、
Replan budget、审批与 recovery policy 仍由各自独立的 bounded 控制保持不变。

Action Journal 的保证边界是：已经 durable 记录为 `SUCCEEDED` / `FAILED` 的动作在
Graph 恢复时不会再次 invoke；明确 `replay_safe=True` 的 ambiguous `STARTED` 动作可
自动重放；其他 ambiguous 动作必须人工选择 retry 或 abort。它不保证任意远端 API 的
universal exactly-once。若远端副作用成功后、Journal 终态提交前进程崩溃，本地只能
诚实地知道动作处于 `STARTED`。

对不可安全重放动作，Human RETRY 只会在 Action Journal 中签发下一 attempt 的
one-shot permit；permit 会在外部调用前 durable consume。若该次调用再次留下
ambiguous `STARTED`，恢复时必须再次请求人工确认，不会沿用上一次授权自动重试。

Trace DB 与 LangGraph checkpoint 独立提交，因此非 Tool semantic trace event 在极端
崩溃窗口中可能重放或重复。后续 Benchmark 对关键 Tool 指标应结合 ToolCallRecord、
Action Journal 与 Trace，而不是把 Trace 假设为天然 exactly-once。

当前只支持每个 `task_id` 一个 active runner，不包含 distributed lock、lease 或多
worker 协调。同一 Service/App 生命周期中，browser-enabled task 若停在 interrupt，
会按 task_id 独占保留原 BrowserSession，approve/reject 后继续使用该 live environment，
并在任务终态或 app shutdown 时关闭。Service 重启后不会恢复旧 Page、popup、DOM、
滚动位置或表单瞬态；若 durable State 正等待 browser action approval、但 live runtime
已不存在，resume 会返回 conflict 并 fail closed，绝不会在新建的 about:blank Page 上
执行旧 frozen click。当前也尚未完整解决网页 Prompt Injection；Risk Policy 只保护
Tool execution boundary。

## 薄 FastAPI 接口（仅本地开发）

CLI 与 HTTP 都调用同一个 `TaskPilotService`。Service 负责打开 SQLite、构造 Provider
与 Graph，并在完成、失败或 interrupt 时返回；FastAPI 不包含另一套
Planner–Executor–Verifier，也没有后台任务、线程队列、Celery 或 Redis。等待审批的
Browser task 会在当前 app lifespan 内保留独立 live runtime，不同 task_id 不共用
Page/BrowserContext。

```powershell
uvicorn --factory taskpilot.api.app:create_app --host 127.0.0.1 --port 8000
```

当前端点：

- `POST /tasks`：创建并同步运行任务，可请求 Host 已允许的 Browser。
- `GET /tasks/{task_id}`：checkpoint-only 读取 durable snapshot（包含已生成的
  `final_answer`）；不连接 MCP、不启动
  Browser、不构造 Host LLM components，也不继续执行 Graph。
- `POST /tasks/{task_id}/resume`：只接受匹配当前 interrupt 的审批决定或 recovery 选择。
- `GET /tasks/{task_id}/trace-summary`：先用同一 checkpoint-only 路径确认任务存在，再只
  打开 SQLite Trace recorder 返回聚合。

HTTP request 不能提交任意 MCP executable、command 或 Tool arguments。审批响应也不能
修改 call ID、step ID 或冻结参数；返回的审批预览使用既有递归脱敏逻辑，API 不直接
序列化整个 TaskState。

安全边界：Phase 11 HTTP API 没有 Authentication 或 Authorization，只供本地/开发环境
使用，不是 production-ready，也不应直接暴露到公网。请求会同步等待 Graph 停止，
没有可靠后台 worker、并发 runner 协调、速率限制或完整 Web Prompt Injection 防御。
