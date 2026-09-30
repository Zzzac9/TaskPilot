# TaskPilot Resume Freeze

## Verified Claims Table

| Claim | Source metric | Run ID | Raw file | Calculation |
|---|---|---|---|---|
| 固定 30 题全部实际执行，Task Success Rate 26.67% | `primary.task_success_rate` | `20260902T040441Z-1da399b-primary` | `benchmarks/results/20260902T040441Z-1da399b-primary/runs.jsonl` | `8 completed+oracle_pass / 30 executed` |
| False Completion 36.67% | `primary.false_completion_rate` | `20260902T040441Z-1da399b-primary` | `benchmarks/results/20260902T040441Z-1da399b-primary/runs.jsonl` | `11 completed+oracle_fail / 30 executed` |
| Full Replanning 相比 No-Replan 高 25 percentage points | `replan_ablation` | Primary + `20260902T041621Z-1da399b-no-replan` | 两个 run 的 `runs.jsonl` | `1/4 - 0/4 = 25pp` |
| 9/9 个预定义 Runtime / fault-injection mechanism cases 通过 | `runtime_reliability.cases_passed` | `20260902T042822Z-1da399b-reliability` | `reliability-runs.json` | `9 passed / 9 total` |
| 预定义 reliability cases 中重复 unsafe side effect 为 0 | `duplicate_unsafe_side_effects` | `20260902T042822Z-1da399b-reliability` | `reliability-summary.json` | case counters 求和为 0 |
| Stability subset 仅 1/10 三轮全成功，2/10 出现波动 | `stability` | Primary + stability 2/3 | 三个 source run 的 `runs.jsonl` | 逐题三次 success 布尔值聚合 |

统一机器可读来源：
`benchmarks/results/20260902T043156Z-1da399b-evaluation/evaluation-summary.json`。

## Resume-ready Project Description

**TaskPilot：面向 Web 与本地环境的通用任务执行 Agent**  
技术栈：Python、LangGraph、MCP、Playwright、FastAPI、SQLite、Pydantic。

- 构建 Planner–Executor–Verifier 结构化工作流，将可恢复 Task State、动态 LLM Context
  与独立审计 Trace 分离；支持 bounded Step retry、依赖调度与 Dynamic Replanning。
- 集成 MCP 动态 Tool discovery 与 Playwright DOM-first Browser runtime；所有动作进入统一
  Tool Registry，并通过 deterministic risk policy 在高风险副作用前触发 Human approval。
- 使用 SQLite checkpoint 与独立 Action Journal 实现 crash recovery；针对 ambiguous
  `STARTED` window 区分 replay-safe 自动恢复和 unsafe Human review，不声称 universal
  exactly-once。
- 实现 DOM-first / Vision-fallback click path、坐标边界验证与 HIGH-risk approval。真实
  Chromium 路径已用 deterministic targeter 验证；external vision-model benchmark 未运行。
- 建立 30 题 external-oracle benchmark：真实 DeepSeek 模型与生产 MCP/Browser 组件取得
  8/30 Task Success Rate；单独揭示 11/30 False Completion，避免用内部 Verifier 美化结果。
- 在固定 4 题 ablation 中测得 Full Replanning 1/4、No-Replan 0/4；在 9 个预定义
  Runtime/Safety fault-injection cases 中 9/9 通过且未观测到重复 unsafe side effect。

## Claim Boundaries

- Benchmark 使用 localhost fixtures，不代表公开互联网成功率。
- 4-task replanning ablation 样本很小，不能外推为普遍 25pp 提升。
- Runtime reliability suite 验证预定义机制，不证明任意远端系统的 exactly-once。
- 没有真实 external Vision model 成功率或 uplift。
- 没有 tokenizer-based token reduction 测量。
