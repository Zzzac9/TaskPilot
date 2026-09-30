# TaskPilot Interview Freeze

## 项目概览

TaskPilot 是一个以 Structured Task State 为核心的通用任务执行 Agent。生产 Graph 使用
Analyzer → Planner → action-level Executor → Step Verifier → Task Verifier，并按结果结束或
Dynamic Replan。MCP、Browser、HITL、Checkpoint、Action Journal 与 Trace 都围绕这个
状态机工作，而不是全部塞进 messages。

## 核心问答

### 为什么 TaskStatus.COMPLETED 不等于 Benchmark Success？

`COMPLETED` 只表示 Agent 自己的 Task Verifier 接受了结果。Benchmark Success 强制要求
`TaskStatus.COMPLETED AND external_oracle.passed`。Primary 中有 11/30 是 completed 但
oracle fail；如果只看内部状态，成功率会被严重高估。

### 为什么 Verifier 不能当 external Oracle？

Verifier 和 Analyzer/Planner/Executor 共享模型假设、上下文和潜在错误模式，容易自洽地
接受错误结果。External oracle 读取固定 ground truth、fixture side effect 或受控文件，
不依赖 Agent 的 completion claim，因此能测出 False Completion。

### 为什么不能保证 universal exactly-once？

本地 Action Journal 能避免对已经 durable 记录为 `SUCCEEDED`/`FAILED` 的动作重复调用，
但远端副作用成功后、Journal 终态落盘前仍可能崩溃。此时本地只能看到 `STARTED`，无法
证明远端是否成功。没有远端幂等键或事务协议时，不能诚实声称 universal exactly-once。

### STARTED ambiguous window 是什么？

流程是先 durable 写 `STARTED`，再调用外部 Tool，最后写 terminal journal 状态。若外部
调用与 terminal commit 之间崩溃，恢复时只知道动作开始过，不知道副作用是否已经发生。
Replay-safe 动作可以重放；unsafe 动作必须进入 Human recovery review。

### Human RETRY 后再次 crash 怎么办？

RETRY 不是永久授权。系统为下一 attempt 签发 one-shot permit，并在外部调用前 durable
consume。若再次留下 ambiguous `STARTED`，必须重新询问 Human，不沿用旧授权自动重试。

### 为什么 Dynamic Replanning 不直接从头跑？

从头跑会丢弃已验证事实、重复付费 Tool call，并可能重复副作用。TaskPilot 保存历史
PlanStep 和 StepResult，只替换未完成路径；新 revision 可以依赖已验证结果。

### 为什么 Replan 必须使用 fresh Step IDs？

Step ID 参与 ToolCall、approval、journal action key、Trace 和恢复定位。复用旧 ID 会让新旧
动作身份混淆，破坏去重与审计。Replan 因此使用全局递增的新 ID，旧步骤保持不可变历史。

### State、Context、Trace 分别是什么？

- State：任务恢复所需的 durable facts 和控制字段。
- Context：每次 LLM 调用前，从 State 按优先级和字符预算动态组装的输入。
- Trace：独立审计/评测事件，不作为恢复真相，也不假设 exactly-once。

三者必须分开，否则 messages 会同时承担事实、提示和审计，导致恢复边界不清。

### 为什么 DOM-first？

DOM/ARIA locator 可解释、可重试、对分辨率变化更稳定，也能提供明确的 Tool 参数和审计
证据。Vision 只在定位类失败时 fallback，避免每次点击都截图和调用模型。

### Vision 为什么是 HIGH risk？

坐标点击对页面时序、遮挡和模型误识别敏感，错误点击可能触发副作用。Vision fallback
因此必须进入 Human Approval；获批后仍重新 DOM-first，并在需要时基于当前 viewport
重新定位，而不是执行过期坐标。

### FastAPI Browser approval 为什么只能在同一 live runtime？

Page、popup、滚动位置和表单瞬态不能安全序列化到 SQLite。相同 Service lifespan 可以按
task_id 保留原 BrowserSession；Service 重建后若旧任务仍等待 browser approval，会返回
conflict 并 fail closed，不能在新的 about:blank 页面执行旧 frozen action。

### Graph recursion limit 与语义 retry 有什么区别？

默认 `graph_recursion_limit=100` 是外围 superstep safety fuse，防止 Graph 无界运行。
Executor `max_actions_per_step`、Step attempts、Replan budget、approval 和 recovery policy
仍分别 bounded。提高 Graph fuse 不等于允许 Agent 无限重试。

### 当前 Benchmark 说明了什么？

Primary 固定 30 题全部执行，8/30 成功，False Completion 11/30；类别成功率从 Constraint
Aggregation 0/4 到 HITL 3/4 不等。它证明 benchmark 能暴露真实薄弱点，而不是证明系统
已经生产可用。Full vs No-Replan 是 1/4 vs 0/4；该 25pp 只适用于这 4 题。

### 当前最大限制是什么？

真实任务成功率和 False Completion 仍不理想，尤其结构化输出稳定性、多源聚合、单动作
Tool Calling 协议和 Verifier 与 external oracle 的一致性。另有 localhost-only benchmark、
无 auth/background worker、无 distributed runner、Browser live state 不跨进程恢复、没有
真实 external Vision model benchmark，也未完整解决网页 Prompt Injection。

## Benchmark Evidence

- Primary：`20260902T040441Z-1da399b-primary`，8/30。
- No-Replan：`20260902T041621Z-1da399b-no-replan`，0/4。
- Stability：Primary attempt 1 + `20260902T041758Z-1da399b-stability-2` +
  `20260902T042251Z-1da399b-stability-3`。
- Reliability：`20260902T042822Z-1da399b-reliability`，9/9 predefined cases passed。
- Final evaluation：`20260902T043156Z-1da399b-evaluation`。
