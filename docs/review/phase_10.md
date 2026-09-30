# Phase 10 Review — Dynamic Replanning, Durable Trace, Context Budgeting

> 最终验收版本，已包含 Phase 10 Acceptance Repair。本文基于当前工作区最终源码、真实 SQLite crash/recovery 与真实 pytest 输出生成。未开始 Phase 11。

## 1. Phase Goal

Phase 10 实现 bounded Dynamic Replanning、plan revision、全局 fresh Step IDs、历史 Plan 与 verified StepResult 保留、独立 durable structured Trace，以及 deterministic Context Budgeting。

Acceptance Repair 进一步修复：Context overflow 逃出 Graph、unsafe Human RETRY 永久授权、成功 PLAN_REPLANNED event revision 仍为旧值。

明确未实现 Vision/OCR、FastAPI、OpenTelemetry exporter、vector memory、LLM summarizer、distributed tracing backend 与 Benchmark execution。

## 2. Final Directory Tree

```text
TaskPilot/
├── benchmarks
│   └── README.md
├── docs
│   └── review
│       ├── phase_03.md
│       ├── phase_04.md
│       ├── phase_05.md
│       ├── phase_06.md
│       ├── phase_07.md
│       ├── phase_08.md
│       ├── phase_09.md
│       └── phase_10.md
├── src
│   └── taskpilot
│       ├── browser
│       │   ├── __init__.py
│       │   ├── config.py
│       │   ├── locators.py
│       │   ├── session.py
│       │   └── tools.py
│       ├── context
│       │   ├── __init__.py
│       │   ├── builder.py
│       │   └── config.py
│       ├── mcp
│       │   ├── __init__.py
│       │   ├── adapter.py
│       │   ├── config.py
│       │   └── provider.py
│       ├── persistence
│       │   ├── __init__.py
│       │   ├── checkpoint.py
│       │   ├── config.py
│       │   ├── journal.py
│       │   └── models.py
│       ├── policy
│       │   ├── __init__.py
│       │   ├── browser.py
│       │   ├── engine.py
│       │   └── models.py
│       ├── tools
│       │   ├── __init__.py
│       │   ├── base.py
│       │   └── registry.py
│       ├── trace
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── recorder.py
│       │   └── sqlite.py
│       ├── __init__.py
│       ├── analyzer.py
│       ├── cli.py
│       ├── config.py
│       ├── executor.py
│       ├── graph.py
│       ├── llm.py
│       ├── models.py
│       ├── planner.py
│       ├── replanner.py
│       ├── state.py
│       └── verifier.py
├── tests
│   ├── fixtures
│   │   ├── browser_site
│   │   │   ├── delayed.html
│   │   │   ├── details.html
│   │   │   ├── download.txt
│   │   │   ├── frame.html
│   │   │   └── index.html
│   │   ├── mcp_config.json
│   │   └── mcp_server.py
│   ├── conftest.py
│   ├── test_analyzer.py
│   ├── test_browser_executor.py
│   ├── test_browser_policy.py
│   ├── test_browser.py
│   ├── test_context.py
│   ├── test_executor.py
│   ├── test_graph_smoke.py
│   ├── test_hitl.py
│   ├── test_mcp_integration.py
│   ├── test_mcp.py
│   ├── test_models.py
│   ├── test_phase10_graph.py
│   ├── test_phase10_trace_boundaries.py
│   ├── test_phase5_graph.py
│   ├── test_phase9_mcp_resume.py
│   ├── test_phase9_persistence.py
│   ├── test_phase9_recovery.py
│   ├── test_planner.py
│   ├── test_policy.py
│   ├── test_replanner.py
│   ├── test_tools.py
│   ├── test_trace.py
│   └── test_verifier.py
├── pyproject.toml
└── README.md
```

## 3. Dynamic Replanning Architecture

```text
START → initialize_task → analyze_task → plan_task → begin_step → decide_action
verify_step ├ accepted → advance_step
            ├ rejected + attempts remain → reset_step_attempt → decide_action
            └ attempts exhausted + budget → replan_task → begin_step
verify_task ├ completed → END
            └ rejected + budget → replan_task → begin_step
replan_task ├ accepted → begin_step
            └ invalid / budget exhausted → END FAILED
```

Recovery Review 与 Human Reject 不是 Replan trigger。

## 4. Replan Models

`ReplanTrigger`、`ReplanRequest`、`ReplanProposal`、`ReplanRecord` 均为 JSON-serializable Pydantic models。TaskState 保存 pending_replan、plan_revision、replan_count、replan_history；mutable defaults 使用 default_factory。

## 5. Plan Revision Semantics

Initial Planner 输出由代码统一覆盖为 revision 0。每次 Proposal 成功验证后，新步骤统一写 next revision，plan_revision/replan_count 各增加一次。非法 Proposal 不消耗 budget。

## 6. Fresh Step ID Rule

所有 Replan Step ID 必须大于历史最大 ID。Phase 9 Action Journal key 包含 task_id、step_id、retry_count、action_index，因此复用 ID 会污染 durable identity。

## 7. Replanner Interface

`TaskReplannerLike.replan(ContextSnapshot) -> ReplanProposal`。生产 `TaskReplanner` 使用现有 OpenAI-compatible Structured Output。Graph 负责 Context、validation、revision 与 State mutation。

## 8. Replanner Production Prompt

```text
你是 Task Replanner，只规划从当前真实状态出发接下来还需要完成的语义子目标（WHAT），不执行任务，也不宣布整个任务已经完成。
用户 Task Goal 与 Constraints 不允许修改；verified StepResult 是已完成事实，必须复用且不得让对应 completed Step 重新执行。failed/skipped 的旧路径可以替代，但不得把它们当作已完成事实。
你只输出剩余工作的新 PlanStep，不复制历史步骤。每个新 Step 必须有非空、仅针对该步骤的 success_criteria，并保持 pending、retry_count=0。
所有新 Step ID 必须严格大于输入中的 historical_max_step_id。depends_on 只能引用已 completed 的历史 Step ID，或本 proposal 中排在当前 Step 之前的新 Step ID；禁止依赖 failed、skipped、running 或 pending 的历史 Step。
不得选择或调用 Tool，不得输出 search(...)、browser_open(...)、click(...)、filesystem.write(...) 等调用、具体 Tool arguments、CSS selector 或浏览器定位器。
不得添加用户没有给出的硬约束，不得虚构环境事实、工具结果或尚未获得的数据。输出必须符合 ReplanProposal schema。
```

## 9. Replan Validation

检查 non-empty、unique/fresh IDs、non-empty success criteria、self/nonexistent dependency、只依赖 completed historical 或 earlier new Step、拒绝 failed/skipped/running/pending old dependency、无环、pending status、retry_count=0。revision 由代码覆盖，不静默修复模型语义。

## 10. Step-Failure Trigger

达到 max_step_attempts 且还有 Replan budget 时，当前 Step 置 FAILED，创建 STEP_ATTEMPTS_EXHAUSTED request，Task 保持 RUNNING并进入 replan_task。budget 不可用或 Replanner fatal 才 FAILED。

## 11. Task-Verification Trigger

Task Verifier completed=False 时保存 VerificationResult，并把 reason/missing_requirements 放入 TASK_VERIFICATION_FAILED request。有 budget 时重规划剩余工作并再次验证 Task。

## 12. max_replans

默认 2，必须 >=0；0 保留 fail-fast。只在 Proposal 成功接受后计数，不能无限调用 Replanner。

## 13. Historical Plan Preservation

completed 保持 completed；触发 Step 保持 failed；旧 pending/running 变 skipped；新 revision append。StepResults、ToolCallRecords、PolicyDecisions、ApprovalRecords 与 Journal 均不删除。

## 14. Updated Graph Flow

`current_revision_finished` 只检查 active revision 的 pending/running。历史 failed/skipped 不阻止 Task Verification；新步骤可依赖 completed historical StepResult。

## 15. Action Journal Collision Reasoning

Replan smoke 真实 journal keys 为 step 1、2、4，历史最大 ID 3，新 IDs 4/5，因此新旧 action identity 不碰撞，completed Step 1 不重跑。

## 16. Trace Models

TraceEvent 保存 event_id、task_id、event_type、timestamp、step/call identity、plan_revision、status、真实 latency 与脱敏 payload。事件覆盖 Task、Plan、Step、Context、LLM、Policy、Approval、Tool、Recovery 与 Verification。

## 17. SQLiteTraceRecorder

独立 traces.sqlite3，async lifecycle，每条 record commit，按 sequence 稳定读取；支持 list_events/count_by_type。Trace 不进入 TaskState。

## 18. Trace Event Instrumentation

Analyzer、Planner、Replanner、Executor decision、Tool invoke、Step/Task Verifier 使用 perf_counter。无 usage_metadata 时不伪造 token 0。Journal materialization 单独记录，不能冒充真实 invoke。

## 19. Trace Redaction

Tool arguments 只保存 recursive redacted preview；password/token/cookie/authorization/API key 等敏感键替换为 [REDACTED]，错误字符串还移除已知 LLM_API_KEY。

## 20. Trace Metrics Helper

`summarize_task_trace` 返回 event/tool/failure/rejection/replan/approval/recovery counts 与 observed latency。单任务不计算 Task Success Rate。

## 21. ContextConfig

max_total_chars、max_single_observation_chars、max_recent_tool_calls、max_dependency_result_chars、max_task_results_chars、max_feedback_chars 均有确定性默认值和校验。

## 22. ContextBuilder

统一构建 Executor/Replanner ContextSnapshot；Snapshot 保存 content、total chars、included/omitted tool calls、truncated sections，不写入 State。

## 23. Context Priority

Goal/Constraints/Current Step/success criteria → feedback → verified dependencies → recent observations → task_results → policy/approval。核心不会为旧 history 静默截断。

## 24. Observation Compaction

模型只看当前 Step 最近 N 条 observation；State 与 Trace 仍保留完整历史。Replanner 只看失败 Step 的近期相关 observation。

## 25. Explicit Truncation

超限值显式变为 `{"truncated":true,"original_length":...,"preview":"..."}`，且记录 truncated_sections。不存在静默截断。

## 26. ContextBudgetError

Builder 层仍直接抛 ContextBudgetError。生产 TaskExecutor.decide_action 在 model 创建/调用前捕获它，返回 fatal ExecutorDecisionResult；Graph 转为 StepExecutionOutcome + Task FAILED。不会放大 budget，也不会截断 Goal。

## 27. State vs Context vs Trace

State 是 durable task facts；Context 是动态模型输入子集；Trace 是独立审计/评测事件。State != Context != Trace。

## 28. CLI Integration

state-dir 下保持 checkpoints.sqlite3、actions.sqlite3，并新增 traces.sqlite3。AsyncExitStack 管理 provider/checkpoint/journal/trace；resume 延续同一 task_id trace namespace，最终只打印 summary。

## 29. Full Relevant Source Code and Tests

以下完整内容从当前最终工作区读取，不是 diff。

### src/taskpilot/models.py

```python
"""TaskPilot 任务状态使用的结构化数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PlanStepStatus(str, Enum):
    """单个计划步骤的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolCallStatus(str, Enum):
    """一次工具调用的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """整个任务的生命周期状态。"""

    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutorActionType(str, Enum):
    """Executor 单次模型决策的动作类型。"""

    TOOL = "tool"
    FINISH_STEP = "finish_step"


class ReplanTrigger(str, Enum):
    """触发动态重规划的两种任务语义。"""

    STEP_ATTEMPTS_EXHAUSTED = "step_attempts_exhausted"
    TASK_VERIFICATION_FAILED = "task_verification_failed"


class TaskSpec(BaseModel):
    """对用户任务进行规范化描述。"""

    goal: str
    # 每个模型实例都创建独立容器，避免可变默认值在实例之间共享。
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output: str | None = None
    completion_criteria: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    """任务计划中的一个可执行步骤。"""

    id: int
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    retry_count: int = 0
    depends_on: list[int] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    # revision 由 Graph 写入，模型输出的值不作为可信控制信息。
    revision: int = 0


class TaskPlan(BaseModel):
    """Initial Planner 生成的结构化任务计划。"""

    steps: list[PlanStep]


class ReplanRequest(BaseModel):
    """Graph 根据真实失败事实创建的重规划请求。"""

    trigger: ReplanTrigger
    failed_step_id: int | None = None
    reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    verification_feedback: str | None = None


class ReplanProposal(BaseModel):
    """Replanner 只提出后续语义步骤，不复制历史计划。"""

    reason: str
    steps: list[PlanStep]


class ReplanRecord(BaseModel):
    """一次已接受重规划的持久化摘要。"""

    revision: int
    trigger: ReplanTrigger
    reason: str
    replaced_step_ids: list[int] = Field(default_factory=list)
    new_step_ids: list[int] = Field(default_factory=list)


class StepExecutionOutcome(BaseModel):
    """Executor 对当前步骤执行结果的结构化声明。"""

    step_id: int
    claimed_complete: bool = False
    summary: str | None = None
    evidence: list[str] = Field(default_factory=list)
    # output 在 Verifier 接受前仍是不可信的 Executor 声明。
    output: dict[str, Any] = Field(default_factory=dict)
    action_count: int = 0
    error: str | None = None


class StepResult(BaseModel):
    """已经通过 Step Verifier 的正式步骤产物。"""

    step_id: int
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class CriterionCheck(BaseModel):
    """对一个步骤成功条件的独立验证结论。"""

    criterion_index: int
    satisfied: bool
    reason: str


class StepVerificationResult(BaseModel):
    """Step Verifier 对当前步骤完成声明的验证结果。"""

    step_id: int
    verified: bool
    checks: list[CriterionCheck] = Field(default_factory=list)
    feedback: str | None = None


class ToolCallRecord(BaseModel):
    """一次工具调用的原始执行记录。"""

    call_id: str
    step_id: int | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.PENDING
    result: Any | None = None
    error: str | None = None


class ExecutorAction(BaseModel):
    """已冻结并可写入 checkpoint 的单个 Executor 动作。"""

    step_id: int
    action_index: int
    call_id: str
    action_type: ExecutorActionType
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """任务完成条件的校验结果。"""

    completed: bool
    reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    next_action: str | None = None

```

### src/taskpilot/state.py

```python
"""TaskPilot 使用的 LangGraph 状态结构。"""

from typing import Any, TypedDict

from taskpilot.models import (
    ExecutorAction,
    PlanStep,
    ReplanRecord,
    ReplanRequest,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)
from taskpilot.persistence.models import RecoveryIssue
from taskpilot.policy.models import ApprovalRecord, PendingToolAction, PolicyDecision


class TaskState(TypedDict):
    """在 TaskPilot 图节点之间传递的结构化任务状态。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    # step_results 只保存通过 Step Verifier 的正式步骤产物。
    step_results: list[StepResult]
    # step_verification 是当前步骤最近一次验证，不等同于任务级 verification。
    step_verification: StepVerificationResult | None
    # tool_calls 保留原始执行记录，便于后续追踪与恢复。
    tool_calls: list[ToolCallRecord]
    # task_results 只保存与最终目标直接相关的提炼结果，不与工具日志混用。
    task_results: dict[str, Any]
    verification: VerificationResult | None
    # pending_action 是未执行的冻结动作，不属于 ToolCallRecord。
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    # 每次模型 decision 后递增；审批与崩溃恢复都不能偷偷重置。
    current_action_index: int
    # 模型 decision 与真实 Tool execution 之间的 durable frozen action。
    pending_executor_action: ExecutorAction | None
    # STARTED 且不可安全重放时保存给人工恢复审查的问题。
    recovery_issue: RecoveryIssue | None
    # Replan 控制事实属于 durable State；模型 Context 与 Trace 均不放在这里。
    pending_replan: ReplanRequest | None
    plan_revision: int
    replan_count: int
    replan_history: list[ReplanRecord]
    status: TaskStatus
    error: str | None


class TaskStateUpdate(TypedDict, total=False):
    """图节点返回的部分状态更新；未返回的字段由 LangGraph 原样保留。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    step_results: list[StepResult]
    step_verification: StepVerificationResult | None
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    verification: VerificationResult | None
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    current_action_index: int
    pending_executor_action: ExecutorAction | None
    recovery_issue: RecoveryIssue | None
    pending_replan: ReplanRequest | None
    plan_revision: int
    replan_count: int
    replan_history: list[ReplanRecord]
    status: TaskStatus
    error: str | None

```

### src/taskpilot/planner.py

```python
"""把 TaskSpec 拆解为结构化、与具体工具无关的任务计划。"""

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.llm import create_chat_model
from taskpilot.models import TaskPlan, TaskSpec


PLANNER_SYSTEM_PROMPT = """你是 Initial Planner，只负责根据输入的 TaskSpec 创建计划，不重新解释或修改用户目标，也不执行任务。
使用完成任务所需的最少合理步骤；每个 PlanStep 描述任务级子目标，不得包含 search、browser、filesystem 等具体工具调用、函数参数或选择器，除非某个工具名称本身就是用户明确要求处理的任务对象。
每一步都必须提供只针对该步骤的、可验证的 success_criteria；整个计划最终应覆盖 TaskSpec.completion_criteria。
合理填写 depends_on，步骤 ID 必须唯一、稳定，并从 1 顺序递增；初始步骤保持 pending 状态。
不得添加 TaskSpec 中不存在的硬约束，不得虚构尚未执行获得的数据，也不得声称已经搜索、打开、读取或生成任何内容。
Planner 只做规划，不做执行。输出必须符合 TaskPlan schema。"""


class TaskPlannerLike(Protocol):
    """真实 Planner 与离线 Fake 共同遵循的最小接口。"""

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        """根据结构化任务描述生成经过校验的计划。"""

        ...


def validate_plan(task_plan: TaskPlan) -> None:
    """对 Initial Planner 输出执行轻量、确定性的结构校验。"""

    if not task_plan.steps:
        raise ValueError("TaskPlan.steps 不能为空")

    step_ids = [step.id for step in task_plan.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("PlanStep.id 必须唯一")
    if any(step_id < 1 for step_id in step_ids):
        raise ValueError("Initial PlanStep.id 必须是正整数")
    if step_ids != sorted(step_ids) or any(
        right <= left for left, right in zip(step_ids, step_ids[1:])
    ):
        raise ValueError("Initial PlanStep.id 必须严格递增")
    step_positions = {step_id: index for index, step_id in enumerate(step_ids)}
    for index, step in enumerate(task_plan.steps):
        if not step.success_criteria:
            raise ValueError(f"PlanStep {step.id} 缺少 success_criteria")
        for dependency_id in step.depends_on:
            if dependency_id == step.id:
                raise ValueError(f"PlanStep {step.id} 不能依赖自身")
            if dependency_id not in step_positions:
                raise ValueError(
                    f"PlanStep {step.id} 引用了不存在的依赖 {dependency_id}"
                )
            if step_positions[dependency_id] >= index:
                raise ValueError(
                    f"PlanStep {step.id} 的依赖 {dependency_id} 必须出现在当前步骤之前"
                )


class TaskPlanner:
    """使用 Chat Model Structured Output 创建并校验初始计划。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        """从 TaskSpec 生成有效的结构化 TaskPlan。"""

        if self._model is None:
            # 与 Analyzer 一样延迟创建模型，保证默认测试无需 API Key。
            self._model = create_chat_model()
        structured_model = self._model.with_structured_output(TaskPlan)
        result = structured_model.invoke(
            [
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", "TaskSpec:\n" + task_spec.model_dump_json(indent=2)),
            ]
        )
        task_plan = (
            result if isinstance(result, TaskPlan) else TaskPlan.model_validate(result)
        )
        validate_plan(task_plan)
        # revision 属于 Graph 控制信息，不信任模型生成值。
        return TaskPlan(
            steps=[step.model_copy(update={"revision": 0}) for step in task_plan.steps]
        )

```

### src/taskpilot/replanner.py

```python
"""根据已发生事实调整剩余语义路径的 Dynamic Replanner。"""

import json
from typing import Protocol, Sequence

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.context import ContextSnapshot
from taskpilot.llm import create_chat_model
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    ReplanProposal,
)


REPLANNER_SYSTEM_PROMPT = """你是 Task Replanner，只规划从当前真实状态出发接下来还需要完成的语义子目标（WHAT），不执行任务，也不宣布整个任务已经完成。
用户 Task Goal 与 Constraints 不允许修改；verified StepResult 是已完成事实，必须复用且不得让对应 completed Step 重新执行。failed/skipped 的旧路径可以替代，但不得把它们当作已完成事实。
你只输出剩余工作的新 PlanStep，不复制历史步骤。每个新 Step 必须有非空、仅针对该步骤的 success_criteria，并保持 pending、retry_count=0。
所有新 Step ID 必须严格大于输入中的 historical_max_step_id。depends_on 只能引用已 completed 的历史 Step ID，或本 proposal 中排在当前 Step 之前的新 Step ID；禁止依赖 failed、skipped、running 或 pending 的历史 Step。
不得选择或调用 Tool，不得输出 search(...)、browser_open(...)、click(...)、filesystem.write(...) 等调用、具体 Tool arguments、CSS selector 或浏览器定位器。
不得添加用户没有给出的硬约束，不得虚构环境事实、工具结果或尚未获得的数据。输出必须符合 ReplanProposal schema。"""


class TaskReplannerLike(Protocol):
    """真实 Replanner 与离线 Fake 共用的最小接口。"""

    def replan(self, context: ContextSnapshot) -> ReplanProposal:
        """根据已压缩但事实完整的上下文提出剩余语义步骤。"""

        ...


class TaskReplanner:
    """使用现有 OpenAI-compatible Structured Output 的 Replanner。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def replan(self, context: ContextSnapshot) -> ReplanProposal:
        if self._model is None:
            self._model = create_chat_model()
        structured_model = self._model.with_structured_output(ReplanProposal)
        result = structured_model.invoke(
            [
                ("system", REPLANNER_SYSTEM_PROMPT),
                (
                    "human",
                    "Current verified task facts:\n"
                    + json.dumps(context.content, ensure_ascii=False, sort_keys=True),
                ),
            ]
        )
        return (
            result
            if isinstance(result, ReplanProposal)
            else ReplanProposal.model_validate(result)
        )


def validate_replan_proposal(
    proposal: ReplanProposal,
    historical_plan: Sequence[PlanStep],
) -> None:
    """拒绝会破坏历史身份、依赖或执行初态的 Proposal。"""

    if not proposal.steps:
        raise ValueError("ReplanProposal.steps 不能为空")
    historical_max = max((step.id for step in historical_plan), default=0)
    ids = [step.id for step in proposal.steps]
    if len(ids) != len(set(ids)):
        raise ValueError("ReplanProposal 的 PlanStep.id 必须唯一")
    if any(step_id <= historical_max for step_id in ids):
        raise ValueError(
            f"Replan Step ID 必须大于 historical_max_step_id={historical_max}"
        )

    historical_by_id = {step.id: step for step in historical_plan}
    completed_historical = {
        step.id for step in historical_plan if step.status is PlanStepStatus.COMPLETED
    }
    earlier_new: set[int] = set()
    for step in proposal.steps:
        if step.status is not PlanStepStatus.PENDING:
            raise ValueError(f"Replan Step {step.id} 初始 status 必须为 pending")
        if step.retry_count != 0:
            raise ValueError(f"Replan Step {step.id} 初始 retry_count 必须为 0")
        if not step.success_criteria:
            raise ValueError(f"Replan Step {step.id} 缺少 success_criteria")
        if step.id in step.depends_on:
            raise ValueError(f"Replan Step {step.id} 不能依赖自身")
        for dependency_id in step.depends_on:
            if dependency_id in earlier_new or dependency_id in completed_historical:
                continue
            historical = historical_by_id.get(dependency_id)
            if historical is not None:
                raise ValueError(
                    f"Replan Step {step.id} 不得依赖状态为 "
                    f"{historical.status.value} 的历史 Step {dependency_id}"
                )
            if dependency_id in ids:
                raise ValueError(
                    f"Replan Step {step.id} 的新依赖 {dependency_id} 必须更早出现"
                )
            raise ValueError(
                f"Replan Step {step.id} 引用了不存在的依赖 {dependency_id}"
            )
        earlier_new.add(step.id)

    # 上述“只能依赖更早新步骤”已经排除环；显式 DFS 保持规则可审计。
    new_by_id = {step.id: step for step in proposal.steps}
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(step_id: int) -> None:
        if step_id in visiting:
            raise ValueError("ReplanProposal 不允许循环依赖")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency_id in new_by_id[step_id].depends_on:
            if dependency_id in new_by_id:
                visit(dependency_id)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in ids:
        visit(step_id)

```

### src/taskpilot/context/__init__.py

```python
"""按确定性字符预算构造模型上下文。"""

from taskpilot.context.builder import (
    ContextBudgetError,
    ContextBuilder,
    ContextSnapshot,
)
from taskpilot.context.config import ContextConfig

__all__ = [
    "ContextBudgetError",
    "ContextBuilder",
    "ContextConfig",
    "ContextSnapshot",
]

```

### src/taskpilot/context/config.py

```python
"""Context Builder 的确定性字符预算配置。"""

from pydantic import BaseModel, Field


class ContextConfig(BaseModel):
    """字符预算不是 tokenizer 估算，只用于稳定限制上下文体积。"""

    max_total_chars: int = Field(default=24_000, ge=1)
    max_single_observation_chars: int = Field(default=4_000, ge=1)
    max_recent_tool_calls: int = Field(default=6, ge=0)
    max_dependency_result_chars: int = Field(default=6_000, ge=1)
    max_task_results_chars: int = Field(default=4_000, ge=1)
    max_feedback_chars: int = Field(default=3_000, ge=1)

```

### src/taskpilot/context/builder.py

```python
"""从 durable State 事实中选择当前模型调用所需的有限上下文。"""

import json
from typing import Any, Sequence

from pydantic import BaseModel, Field

from taskpilot.context.config import ContextConfig
from taskpilot.models import (
    PlanStep,
    ReplanRequest,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
)
from taskpilot.policy import ApprovalRecord, PolicyDecision, redact_preview


class ContextBudgetError(ValueError):
    """最高优先级核心上下文本身超过总字符预算。"""


class ContextSnapshot(BaseModel):
    """一次动态组装结果；只供模型输入和 Trace 使用。"""

    content: dict[str, Any]
    total_chars: int
    included_tool_calls: int = 0
    omitted_tool_calls: int = 0
    truncated_sections: list[str] = Field(default_factory=list)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _size(value: Any) -> int:
    return len(_json_text(value))


def _truncate(value: Any, limit: int) -> tuple[Any, bool]:
    """稳定截断序列化文本，并显式告知模型原内容不完整。"""

    text = _json_text(value)
    if len(text) <= limit:
        return value, False
    return {
        "truncated": True,
        "original_length": len(text),
        "preview": text[:limit],
    }, True


class ContextBuilder:
    """按固定优先级构建 Executor 与 Replanner 上下文。"""

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()

    def build_executor_context(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ContextSnapshot:
        """优先保留任务核心、反馈和 verified dependency results。"""

        core: dict[str, Any] = {
            "task_goal": task_spec.goal,
            "task_constraints": task_spec.constraints,
            "current_step": {
                "id": step.id,
                "revision": step.revision,
                "description": step.description,
                "success_criteria": step.success_criteria,
            },
        }
        if _size(core) > self.config.max_total_chars:
            raise ContextBudgetError(
                "任务目标、约束与当前步骤超过 max_total_chars，拒绝静默截断核心上下文"
            )

        content = dict(core)
        truncated: list[str] = []

        feedback: Any = None
        if verification_feedback is not None and verification_feedback.step_id == step.id:
            feedback, cut = _truncate(
                verification_feedback.model_dump(mode="json"),
                self.config.max_feedback_chars,
            )
            if cut:
                truncated.append("verification_feedback")
        self._add_high_priority(content, "verification_feedback", feedback, truncated)

        dependencies: list[Any] = []
        for result in step_results:
            if result.step_id not in step.depends_on:
                continue
            compact, cut = _truncate(
                result.model_dump(mode="json"),
                self.config.max_dependency_result_chars,
            )
            if cut:
                truncated.append(f"dependency_result:{result.step_id}")
            dependencies.append(compact)
        self._add_if_fits(content, "dependency_results", dependencies, truncated)

        current_calls = [record for record in tool_calls if record.step_id == step.id]
        selected = current_calls[-self.config.max_recent_tool_calls :]
        if self.config.max_recent_tool_calls == 0:
            selected = []
        observations: list[dict[str, Any]] = []
        for record in selected:
            value = record.result if record.error is None else record.error
            compact, cut = _truncate(value, self.config.max_single_observation_chars)
            if cut:
                truncated.append(f"tool_observation:{record.call_id}")
            observations.append(
                {
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "status": record.status.value,
                    "observation": compact,
                }
            )
        included = self._add_observations(content, observations, truncated)

        compact_results, cut = _truncate(
            task_results, self.config.max_task_results_chars
        )
        if cut:
            truncated.append("task_results")
        self._add_if_fits(content, "task_results", compact_results, truncated)

        policy = [
            item.model_dump(mode="json")
            for item in policy_decisions
            if item.step_id == step.id
        ]
        approvals = [
            item.model_dump(mode="json")
            for item in approval_records
            if item.step_id == step.id
        ]
        self._add_if_fits(content, "policy_feedback", policy, truncated)
        self._add_if_fits(content, "approval_history", approvals, truncated)
        return ContextSnapshot(
            content=content,
            total_chars=_size(content),
            included_tool_calls=included,
            omitted_tool_calls=len(current_calls) - included,
            truncated_sections=list(dict.fromkeys(truncated)),
        )

    def build_replan_context(
        self,
        *,
        task_spec: TaskSpec,
        plan: Sequence[PlanStep],
        step_results: Sequence[StepResult],
        tool_calls: Sequence[ToolCallRecord],
        request: ReplanRequest,
        plan_revision: int,
        replan_count: int,
    ) -> ContextSnapshot:
        """构造只包含真实事实与最近相关 Observation 的重规划上下文。"""

        failed = request.failed_step_id
        feedback, feedback_cut = _truncate(
            {
                "reason": request.reason,
                "missing_requirements": request.missing_requirements,
                "verification_feedback": request.verification_feedback,
            },
            self.config.max_feedback_chars,
        )
        core = {
            "task_goal": task_spec.goal,
            "task_constraints": task_spec.constraints,
            "completion_criteria": task_spec.completion_criteria,
            "replan_trigger": request.trigger.value,
            "current_plan_revision": plan_revision,
            "replan_count": replan_count,
            "historical_max_step_id": max((step.id for step in plan), default=0),
            "failed_step_id": failed,
            "failure_feedback": feedback,
        }
        if _size(core) > self.config.max_total_chars:
            raise ContextBudgetError("Replanner 核心任务事实超过 max_total_chars")
        content: dict[str, Any] = dict(core)
        truncated = ["failure_feedback"] if feedback_cut else []
        self._add_if_fits(
            content,
            "plan_status",
            [
                {
                    "id": step.id,
                    "revision": step.revision,
                    "description": step.description,
                    "status": step.status.value,
                    "depends_on": step.depends_on,
                    "success_criteria": step.success_criteria,
                }
                for step in plan
            ],
            truncated,
        )
        verified: list[Any] = []
        for result in step_results:
            compact, cut = _truncate(
                result.model_dump(mode="json"),
                self.config.max_dependency_result_chars,
            )
            if cut:
                truncated.append(f"verified_result:{result.step_id}")
            verified.append(compact)
        self._add_if_fits(content, "verified_step_results", verified, truncated)

        related = [record for record in tool_calls if record.step_id == failed]
        selected = related[-self.config.max_recent_tool_calls :]
        if self.config.max_recent_tool_calls == 0:
            selected = []
        observations = []
        for record in selected:
            compact, cut = _truncate(
                record.error if record.error is not None else record.result,
                self.config.max_single_observation_chars,
            )
            if cut:
                truncated.append(f"tool_observation:{record.call_id}")
            observations.append(
                {
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "arguments_preview": redact_preview(record.arguments),
                    "status": record.status.value,
                    "observation": compact,
                }
            )
        included = self._add_observations(content, observations, truncated)
        return ContextSnapshot(
            content=content,
            total_chars=_size(content),
            included_tool_calls=included,
            omitted_tool_calls=len(related) - included,
            truncated_sections=list(dict.fromkeys(truncated)),
        )

    def _add_if_fits(
        self,
        content: dict[str, Any],
        name: str,
        value: Any,
        truncated: list[str],
    ) -> bool:
        candidate = {**content, name: value}
        if _size(candidate) <= self.config.max_total_chars:
            content[name] = value
            return True
        truncated.append(name)
        return False

    def _add_high_priority(
        self,
        content: dict[str, Any],
        name: str,
        value: Any,
        truncated: list[str],
    ) -> None:
        """反馈放不下时缩短 preview，而不是静默丢弃。"""

        if self._add_if_fits(content, name, value, truncated):
            return
        text = _json_text(value)
        low, high = 0, len(text)
        fitted: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate = {
                "truncated": True,
                "original_length": len(text),
                "preview": text[:middle],
            }
            if _size({**content, name: candidate}) <= self.config.max_total_chars:
                fitted = candidate
                low = middle + 1
            else:
                high = middle - 1
        if fitted is None:
            raise ContextBudgetError(f"核心上下文后没有空间保留高优先级 {name}")
        content[name] = fitted

    def _add_observations(
        self,
        content: dict[str, Any],
        observations: Sequence[dict[str, Any]],
        truncated: list[str],
    ) -> int:
        included: list[dict[str, Any]] = []
        # 最新 Observation 优先；最终恢复为时间顺序，便于模型阅读。
        for observation in reversed(observations):
            candidate = list(reversed([observation, *included]))
            if _size({**content, "current_step_observations": candidate}) <= self.config.max_total_chars:
                included.insert(0, observation)
            else:
                truncated.append("current_step_observations")
        if _size({**content, "current_step_observations": included}) <= self.config.max_total_chars:
            content["current_step_observations"] = included
        else:
            truncated.append("current_step_observations")
            included = []
        return len(included)

```

### src/taskpilot/trace/__init__.py

```python
"""TaskPilot 自有的 durable structured trace。"""

from taskpilot.trace.models import TraceEvent, TraceEventType, TraceSummary
from taskpilot.trace.recorder import (
    InMemoryTraceRecorder,
    NoOpTraceRecorder,
    TraceRecorderLike,
    make_trace_event,
    summarize_task_trace,
)
from taskpilot.trace.sqlite import SQLiteTraceRecorder

__all__ = [
    "InMemoryTraceRecorder",
    "NoOpTraceRecorder",
    "SQLiteTraceRecorder",
    "TraceEvent",
    "TraceEventType",
    "TraceRecorderLike",
    "TraceSummary",
    "make_trace_event",
    "summarize_task_trace",
]

```

### src/taskpilot/trace/models.py

```python
"""Structured trace 的 JSON-serializable 事件模型。"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class TraceEventType(str, Enum):
    """Phase 10 需要审计和后续 Benchmark 聚合的稳定事件类型。"""

    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_ANALYZED = "task_analyzed"
    PLAN_CREATED = "plan_created"
    PLAN_REPLANNED = "plan_replanned"
    STEP_STARTED = "step_started"
    STEP_VERIFIED = "step_verified"
    STEP_REJECTED = "step_rejected"
    CONTEXT_BUILT = "context_built"
    LLM_ACTION_DECIDED = "llm_action_decided"
    POLICY_DECIDED = "policy_decided"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    TOOL_MATERIALIZED_FROM_JOURNAL = "tool_materialized_from_journal"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_RESOLVED = "recovery_resolved"
    TASK_VERIFIED = "task_verified"
    TASK_VERIFICATION_REJECTED = "task_verification_rejected"


class TraceEvent(BaseModel):
    """一次独立提交的审计事件；不进入 LangGraph State。"""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_type: TraceEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    step_id: int | None = None
    call_id: str | None = None
    plan_revision: int = 0
    status: str | None = None
    latency_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TraceSummary(BaseModel):
    """Phase 12 可直接复用的确定性任务 Trace 聚合。"""

    event_count: int
    tool_call_count: int
    tool_failure_count: int
    step_rejection_count: int
    replan_count: int
    approval_request_count: int
    recovery_required_count: int
    observed_latency_ms: float

```

### src/taskpilot/trace/recorder.py

```python
"""Trace recorder 接口、内存实现与通用聚合。"""

from typing import Any, Protocol, Sequence

from taskpilot.config import redact_sensitive_values
from taskpilot.policy import redact_preview
from taskpilot.trace.models import TraceEvent, TraceEventType, TraceSummary


class TraceRecorderLike(Protocol):
    """Graph 只依赖的最小异步 Trace 接口。"""

    async def record(self, event: TraceEvent) -> None: ...

    async def list_events(self, task_id: str) -> list[TraceEvent]: ...

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]: ...


def make_trace_event(
    *,
    task_id: str,
    event_type: TraceEventType,
    step_id: int | None = None,
    call_id: str | None = None,
    plan_revision: int = 0,
    status: str | None = None,
    latency_ms: float | None = None,
    payload: dict[str, Any] | None = None,
) -> TraceEvent:
    """创建事件并递归脱敏 payload 中可能出现的凭证字段。"""

    safe_payload = redact_preview(payload or {})

    def redact_known_values(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact_known_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_known_values(item) for item in value]
        if isinstance(value, str):
            return redact_sensitive_values(value)
        return value

    safe_payload = redact_known_values(safe_payload)
    if not isinstance(safe_payload, dict):  # pragma: no cover - 输入类型防御
        safe_payload = {"value": safe_payload}
    return TraceEvent(
        task_id=task_id,
        event_type=event_type,
        step_id=step_id,
        call_id=call_id,
        plan_revision=plan_revision,
        status=status,
        latency_ms=latency_ms,
        payload=safe_payload,
    )


class NoOpTraceRecorder:
    """默认不产生 I/O，保持库调用方无需配置 Trace。"""

    async def record(self, event: TraceEvent) -> None:
        return None

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        return []

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        return {}


class InMemoryTraceRecorder:
    """离线测试使用的轻量 recorder。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def record(self, event: TraceEvent) -> None:
        self.events.append(event.model_copy(deep=True))

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        return [event.model_copy(deep=True) for event in self.events if event.task_id == task_id]

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        return _count_types(await self.list_events(task_id))


def _count_types(events: Sequence[TraceEvent]) -> dict[TraceEventType, int]:
    counts: dict[TraceEventType, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return counts


async def summarize_task_trace(
    recorder: TraceRecorderLike, task_id: str
) -> TraceSummary:
    """只根据实际存在的事件计算任务 Trace 摘要。"""

    events = await recorder.list_events(task_id)
    counts = _count_types(events)
    return TraceSummary(
        event_count=len(events),
        tool_call_count=counts.get(TraceEventType.TOOL_STARTED, 0),
        tool_failure_count=counts.get(TraceEventType.TOOL_FAILED, 0),
        step_rejection_count=counts.get(TraceEventType.STEP_REJECTED, 0),
        replan_count=counts.get(TraceEventType.PLAN_REPLANNED, 0),
        approval_request_count=counts.get(TraceEventType.APPROVAL_REQUESTED, 0),
        recovery_required_count=counts.get(TraceEventType.RECOVERY_REQUIRED, 0),
        observed_latency_ms=sum(
            event.latency_ms for event in events if event.latency_ms is not None
        ),
    )

```

### src/taskpilot/trace/sqlite.py

```python
"""每条事件独立 commit 的单进程 SQLite Trace recorder。"""

import asyncio
from pathlib import Path

import aiosqlite

from taskpilot.trace.models import TraceEvent, TraceEventType


class SQLiteTraceRecorder:
    """使用独立 SQLite 文件保存 Trace，不污染 checkpoint 或 action journal。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path.expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "SQLiteTraceRecorder":
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._db_path)
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trace_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL
            )
            """
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trace_task_sequence "
            "ON trace_events(task_id, sequence)"
        )
        await self._connection.commit()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _active(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteTraceRecorder 尚未进入 async context")
        return self._connection

    async def record(self, event: TraceEvent) -> None:
        connection = self._active()
        async with self._lock:
            await connection.execute(
                "INSERT INTO trace_events(event_id, task_id, event_type, event_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.task_id,
                    event.event_type.value,
                    event.model_dump_json(),
                ),
            )
            await connection.commit()

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        connection = self._active()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT event_json FROM trace_events WHERE task_id = ? "
                "ORDER BY sequence",
                (task_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [TraceEvent.model_validate_json(row[0]) for row in rows]

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        connection = self._active()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT event_type, COUNT(*) FROM trace_events "
                "WHERE task_id = ? GROUP BY event_type",
                (task_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return {TraceEventType(row[0]): int(row[1]) for row in rows}

```

### src/taskpilot/persistence/models.py

```python
"""Action Journal、故障注入与人工恢复的数据模型。"""

from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class JournalStatus(str, Enum):
    """一次外部动作的 durable journal 状态。"""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ActionJournalRecord(BaseModel):
    """不复制原始参数、仅保留 canonical hash 的动作记录。"""

    action_key: str
    task_id: str
    step_id: int
    retry_count: int
    action_index: int
    call_id: str
    tool_name: str
    arguments_hash: str
    status: JournalStatus
    replay_safe: bool = False
    attempt_count: int = 1
    # 非 replay-safe STARTED action 的下一次人工授权 attempt；消费后立即清空。
    authorized_retry_attempt: int | None = None
    result: Any | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class RecoveryChoice(str, Enum):
    """Ambiguous action 只允许显式重试或终止。"""

    RETRY = "retry"
    ABORT = "abort"


class RecoveryResponse(BaseModel):
    """严格校验 LangGraph recovery interrupt 的 resume value。"""

    model_config = ConfigDict(extra="forbid")

    choice: RecoveryChoice
    reason: str | None = None


class RecoveryIssue(BaseModel):
    """STARTED 无终态且不可自动重放时的人机审查材料。"""

    action_key: str
    task_id: str
    step_id: int
    action_index: int
    tool_name: str
    call_id: str
    reason: str
    replay_safe: bool
    arguments_preview: dict[str, Any] = Field(default_factory=dict)
    retry_authorized: bool = False
    resolution_reason: str | None = None


class ExecutionFaultPoint(str, Enum):
    """覆盖三段 crash window 的确定性测试注入点。"""

    AFTER_JOURNAL_STARTED = "after_journal_started"
    AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL = (
        "after_tool_return_before_terminal_journal"
    )
    AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN = (
        "after_terminal_journal_before_graph_return"
    )


class InjectedCrash(BaseException):
    """模拟进程死亡；继承 BaseException 避免被普通业务异常吞掉。"""


class ExecutionFaultInjector(Protocol):
    """生产默认 no-op、测试可替换的 crash hook。"""

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        """在指定持久化边界同步触发或直接返回。"""

        ...


class NoOpExecutionFaultInjector:
    """生产路径默认不注入故障。"""

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        return None

```

### src/taskpilot/persistence/journal.py

```python
"""独立于 LangGraph schema 的 SQLite external-action journal。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

import aiosqlite

from taskpilot.persistence.models import ActionJournalRecord, JournalStatus


class ActionJournalLike(Protocol):
    """SQLite journal 与离线 memory double 共用的最小异步接口。"""

    async def get(self, action_key: str) -> ActionJournalRecord | None: ...

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord: ...

    async def retry_started(self, action_key: str) -> ActionJournalRecord: ...

    async def authorize_retry(self, action_key: str) -> ActionJournalRecord: ...

    async def consume_authorized_retry(
        self, action_key: str
    ) -> ActionJournalRecord: ...

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord: ...

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class SQLiteActionJournal:
    """每次状态写入都独立 commit，缩小但不掩盖 crash ambiguity window。"""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "SQLiteActionJournal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        self._connection = connection
        await self.setup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()

    async def setup(self) -> None:
        connection = self._require_connection()
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS action_journal (
                action_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                retry_count INTEGER NOT NULL,
                action_index INTEGER NOT NULL,
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                replay_safe INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                authorized_retry_attempt INTEGER,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor = await connection.execute("PRAGMA table_info(action_journal)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "authorized_retry_attempt" not in columns:
            # 兼容已有 Phase 9 actions.sqlite3，迁移只新增 nullable 字段。
            await connection.execute(
                "ALTER TABLE action_journal "
                "ADD COLUMN authorized_retry_attempt INTEGER"
            )
        await connection.commit()

    async def get(self, action_key: str) -> ActionJournalRecord | None:
        connection = self._require_connection()
        cursor = await connection.execute(
            "SELECT * FROM action_journal WHERE action_key = ?", (action_key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._from_row(row) if row is not None else None

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO action_journal (
                action_key, task_id, step_id, retry_count, action_index,
                call_id, tool_name, arguments_hash, status, replay_safe,
                attempt_count, result_json, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                record.action_key,
                record.task_id,
                record.step_id,
                record.retry_count,
                record.action_index,
                record.call_id,
                record.tool_name,
                record.arguments_hash,
                record.status.value,
                int(record.replay_safe),
                record.attempt_count,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await connection.commit()
        return record

    async def retry_started(self, action_key: str) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """UPDATE action_journal
               SET attempt_count = attempt_count + 1, updated_at = ?
               WHERE action_key = ? AND status = ? AND replay_safe = 1""",
            (utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        return self._require_record(await self.get(action_key), action_key)

    async def authorize_retry(self, action_key: str) -> ActionJournalRecord:
        """幂等签发仅适用于下一次 unsafe retry 的 durable permit。"""

        connection = self._require_connection()
        record = self._require_record(await self.get(action_key), action_key)
        if record.status is not JournalStatus.STARTED or record.replay_safe:
            raise ValueError("Recovery retry permit 只适用于 unsafe STARTED action")
        expected = record.attempt_count + 1
        if record.authorized_retry_attempt == expected:
            return record
        if record.authorized_retry_attempt is not None:
            raise ValueError("Action Journal 已存在不一致的 recovery retry permit")
        cursor = await connection.execute(
            """UPDATE action_journal
               SET authorized_retry_attempt = ?, updated_at = ?
               WHERE action_key = ? AND status = ? AND replay_safe = 0
                 AND authorized_retry_attempt IS NULL""",
            (expected, utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        updated_rows = cursor.rowcount
        await cursor.close()
        if updated_rows != 1:
            raise ValueError("Recovery retry permit durable 写入失败")
        return self._require_record(await self.get(action_key), action_key)

    async def consume_authorized_retry(self, action_key: str) -> ActionJournalRecord:
        """在 external invoke 前原子消费 permit 并增加 attempt_count。"""

        connection = self._require_connection()
        cursor = await connection.execute(
            """UPDATE action_journal
               SET attempt_count = attempt_count + 1,
                   authorized_retry_attempt = NULL,
                   updated_at = ?
               WHERE action_key = ? AND status = ? AND replay_safe = 0
                 AND authorized_retry_attempt = attempt_count + 1""",
            (utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        updated_rows = cursor.rowcount
        await cursor.close()
        if updated_rows != 1:
            raise ValueError("Recovery retry permit 不存在、已消费或 identity 不一致")
        return self._require_record(await self.get(action_key), action_key)

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord:
        return await self._terminal(
            action_key,
            JournalStatus.SUCCEEDED,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            error=None,
        )

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord:
        return await self._terminal(
            action_key,
            JournalStatus.FAILED,
            result_json=None,
            error=error,
        )

    async def _terminal(
        self,
        action_key: str,
        status: JournalStatus,
        *,
        result_json: str | None,
        error: str | None,
    ) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """UPDATE action_journal
               SET status = ?, result_json = ?, error = ?,
                   authorized_retry_attempt = NULL, updated_at = ?
               WHERE action_key = ? AND status = ?""",
            (
                status.value,
                result_json,
                error,
                utc_now().isoformat(),
                action_key,
                JournalStatus.STARTED.value,
            ),
        )
        await connection.commit()
        return self._require_record(await self.get(action_key), action_key)

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteActionJournal 必须先进入 async context")
        return self._connection

    @staticmethod
    def _require_record(
        record: ActionJournalRecord | None, action_key: str
    ) -> ActionJournalRecord:
        if record is None:
            raise RuntimeError(f"Action Journal record 不存在: {action_key}")
        return record

    @staticmethod
    def _from_row(row: aiosqlite.Row) -> ActionJournalRecord:
        return ActionJournalRecord(
            action_key=row["action_key"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            retry_count=row["retry_count"],
            action_index=row["action_index"],
            call_id=row["call_id"],
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            status=JournalStatus(row["status"]),
            replay_safe=bool(row["replay_safe"]),
            attempt_count=row["attempt_count"],
            authorized_retry_attempt=row["authorized_retry_attempt"],
            result=(
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            ),
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class InMemoryActionJournal:
    """无 SQLite Host 时的离线兼容实现；生产 CLI 不使用。"""

    def __init__(self) -> None:
        self.records: dict[str, ActionJournalRecord] = {}

    async def get(self, action_key: str) -> ActionJournalRecord | None:
        return self.records.get(action_key)

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord:
        if record.action_key in self.records:
            raise ValueError(f"Action Journal key 已存在: {record.action_key}")
        self.records[record.action_key] = record
        return record

    async def retry_started(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"attempt_count": self.records[action_key].attempt_count + 1,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

    async def authorize_retry(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key]
        if record.status is not JournalStatus.STARTED or record.replay_safe:
            raise ValueError("Recovery retry permit 只适用于 unsafe STARTED action")
        expected = record.attempt_count + 1
        if record.authorized_retry_attempt not in {None, expected}:
            raise ValueError("Action Journal 已存在不一致的 recovery retry permit")
        updated = record.model_copy(
            update={
                "authorized_retry_attempt": expected,
                "updated_at": utc_now(),
            }
        )
        self.records[action_key] = updated
        return updated

    async def consume_authorized_retry(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key]
        expected = record.attempt_count + 1
        if (
            record.status is not JournalStatus.STARTED
            or record.replay_safe
            or record.authorized_retry_attempt != expected
        ):
            raise ValueError("Recovery retry permit 不存在、已消费或 identity 不一致")
        updated = record.model_copy(
            update={
                "attempt_count": expected,
                "authorized_retry_attempt": None,
                "updated_at": utc_now(),
            }
        )
        self.records[action_key] = updated
        return updated

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"status": JournalStatus.SUCCEEDED, "result": result,
                    "error": None, "authorized_retry_attempt": None,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"status": JournalStatus.FAILED, "result": None,
                    "error": error, "authorized_retry_attempt": None,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

```

### src/taskpilot/persistence/config.py

```python
"""Phase 9 本地持久化路径配置。"""

from pathlib import Path

from pydantic import BaseModel, field_validator


class PersistenceConfig(BaseModel):
    """Checkpoint、action journal 与 trace 使用彼此独立的 SQLite 文件。"""

    checkpoint_db_path: Path = Path(".taskpilot/checkpoints.sqlite3")
    action_journal_db_path: Path = Path(".taskpilot/actions.sqlite3")
    trace_db_path: Path = Path(".taskpilot/traces.sqlite3")

    @field_validator(
        "checkpoint_db_path", "action_journal_db_path", "trace_db_path", mode="after"
    )
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        """把调用方给出的相对路径固定为绝对路径。"""

        return value.expanduser().resolve()

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> "PersistenceConfig":
        """用 CLI 的 state directory 构造默认的两个数据库路径。"""

        resolved = state_dir.expanduser().resolve()
        return cls(
            checkpoint_db_path=resolved / "checkpoints.sqlite3",
            action_journal_db_path=resolved / "actions.sqlite3",
            trace_db_path=resolved / "traces.sqlite3",
        )

```

### src/taskpilot/executor.py

```python
"""单个 PlanStep 的有界 Function Calling Executor Runtime。"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from taskpilot.config import redact_sensitive_values
from taskpilot.context import ContextBudgetError, ContextBuilder, ContextSnapshot
from taskpilot.llm import create_chat_model
from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    ToolPolicyLike,
)
from taskpilot.tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
)


FINISH_STEP_NAME = "finish_step"
FINISH_STEP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINISH_STEP_NAME,
        "description": (
            "Declare that the current step has sufficient evidence for its "
            "success criteria. This is a claim, not verifier approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Concise summary of what was achieved.",
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Evidence supporting each step success criterion.",
                },
                "output": {
                    "type": "object",
                    "description": (
                        "Structured result produced by this step for verified "
                        "downstream handoff."
                    ),
                },
            },
            "required": ["summary", "evidence", "output"],
            "additionalProperties": False,
        },
    },
}

EXECUTOR_SYSTEM_PROMPT = """你是 Task Executor，只执行当前 PlanStep，不执行未来步骤，也不修改 Task Goal 或 Constraints。
根据当前环境 Observation 决定下一动作；优先使用可用 Tool 获取事实，不得虚构外部结果。
每轮必须且最多选择一个动作：调用一个外部 Tool，或调用内部 finish_step。
Tool 失败后可以根据错误 Observation 调整下一动作，单次 Tool 失败不等于 Step 或 Task 失败。
只有当前 Step 的 success_criteria 已有充分 evidence 时才能调用 finish_step。
finish_step 的 output 必须只包含本步骤实际得到、可交给依赖步骤的结构化结果。
finish_step 只表示 Executor 声称当前 Step 已具备完成条件，最终是否完成由未来 Verifier 判断。
不要直接把 PlanStep 或整个 Task 声称为 completed。"""


@dataclass(frozen=True)
class ExecutorRunResult:
    """TaskExecutor 返回给 Graph 的步骤结果与新增工具记录。"""

    outcome: StepExecutionOutcome | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    pending_action: PendingToolAction | None = None
    policy_decisions: list[PolicyDecision] = field(default_factory=list)
    fatal_error: bool = False


@dataclass(frozen=True)
class ApprovedActionResult:
    """冻结动作获批后单次执行的记录；不重新询问模型或 Policy。"""

    tool_call: ToolCallRecord | None = None
    fatal_error: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ExecutorDecisionResult:
    """一次且仅一次模型调用产生的 frozen action 或 fatal error。"""

    action: ExecutorAction | None = None
    error: str | None = None
    fatal_error: bool = False
    context_snapshot: ContextSnapshot | None = None
    usage_metadata: dict[str, Any] | None = None


class TaskExecutorLike(Protocol):
    """真实 Executor 与离线 Fake 共同遵循的最小接口。"""

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """只执行传入的当前步骤。"""

        ...

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """只执行已获批的 exact frozen action。"""

        ...


def build_executor_messages(
    *,
    task_spec: TaskSpec,
    step: PlanStep,
    task_results: dict[str, Any],
    tool_calls: Sequence[ToolCallRecord],
    step_results: Sequence[StepResult] = (),
    verification_feedback: StepVerificationResult | None = None,
    policy_decisions: Sequence[PolicyDecision] = (),
    approval_records: Sequence[ApprovalRecord] = (),
    context_builder: ContextBuilder | None = None,
) -> list[BaseMessage]:
    """兼容入口；实际内容统一由 deterministic ContextBuilder 组装。"""

    snapshot = (context_builder or ContextBuilder()).build_executor_context(
        task_spec=task_spec,
        step=step,
        task_results=task_results,
        tool_calls=tool_calls,
        step_results=step_results,
        verification_feedback=verification_feedback,
        policy_decisions=policy_decisions,
        approval_records=approval_records,
    )
    return [
        SystemMessage(content=EXECUTOR_SYSTEM_PROMPT),
        HumanMessage(
            content=json.dumps(snapshot.content, ensure_ascii=False, default=str)
        ),
    ]


class TaskExecutor:
    """使用原生 Tool Calling 反复执行当前步骤，直到 finish_step 或动作上限。"""

    def __init__(
        self,
        registry: ToolRegistry,
        model: BaseChatModel | None = None,
        max_actions_per_step: int = 5,
        policy: ToolPolicyLike | None = None,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        if max_actions_per_step < 1:
            raise ValueError("max_actions_per_step 必须至少为 1")
        self._registry = registry
        self._model = model
        self._max_actions_per_step = max_actions_per_step
        self._policy = policy
        self._context_builder = context_builder or ContextBuilder()

    @property
    def max_actions_per_step(self) -> int:
        """供 Graph 在模型调用前执行 durable action 上限检查。"""

        return self._max_actions_per_step

    async def decide_action(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        action_index: int,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorDecisionResult:
        """调用模型一次并冻结一个动作；绝不执行外部 Tool。"""

        if action_index < 1:
            return ExecutorDecisionResult(
                error="Executor action_index 必须从 1 开始",
                fatal_error=True,
            )
        if not self._registry.list_tools():
            return ExecutorDecisionResult(
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )
        try:
            snapshot = self._context_builder.build_executor_context(
                task_spec=task_spec,
                step=step,
                task_results=task_results,
                tool_calls=tool_calls,
                step_results=step_results,
                verification_feedback=verification_feedback,
                policy_decisions=policy_decisions,
                approval_records=approval_records,
            )
        except ContextBudgetError as exc:
            # Context 是 deterministic production boundary；核心超限要返回
            # 可持久化的 Task failure，不能逃出 LangGraph invocation。
            detail = redact_sensitive_values(str(exc))
            return ExecutorDecisionResult(
                error=f"Context budget exceeded: {detail}",
                fatal_error=True,
                context_snapshot=None,
            )
        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        messages: list[BaseMessage] = [
            SystemMessage(content=EXECUTOR_SYSTEM_PROMPT),
            HumanMessage(
                content=json.dumps(snapshot.content, ensure_ascii=False, default=str)
            ),
        ]
        try:
            response = await self._model.bind_tools(schemas).ainvoke(messages)
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            return ExecutorDecisionResult(
                error=f"Executor 模型调用失败: {type(exc).__name__}: {detail}",
                fatal_error=True,
                context_snapshot=snapshot,
            )
        if not isinstance(response, AIMessage):
            return ExecutorDecisionResult(
                error="Executor 模型未返回 AIMessage",
                fatal_error=True,
                context_snapshot=snapshot,
            )
        if len(response.tool_calls) != 1:
            return ExecutorDecisionResult(
                error=("Executor 每轮必须返回且只能返回一个 Tool Call; "
                       f"实际数量: {len(response.tool_calls)}"),
                fatal_error=True,
                context_snapshot=snapshot,
            )
        raw = response.tool_calls[0]
        call_id = raw.get("id")
        if not call_id:
            return ExecutorDecisionResult(
                error="Executor Tool Call 缺少 tool_call_id", fatal_error=True
                , context_snapshot=snapshot
            )
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )
        if call_id in seen_call_ids:
            return ExecutorDecisionResult(
                error=f"Executor Tool Call ID 重复: {call_id}", fatal_error=True
                , context_snapshot=snapshot
            )
        name = raw["name"]
        arguments = raw["args"]
        if name == FINISH_STEP_NAME:
            error = self._validate_finish_arguments(arguments)
            if error is not None:
                return ExecutorDecisionResult(
                    error=error, fatal_error=True, context_snapshot=snapshot
                )
            return ExecutorDecisionResult(
                action=ExecutorAction(
                    step_id=step.id,
                    action_index=action_index,
                    call_id=call_id,
                    action_type=ExecutorActionType.FINISH_STEP,
                    arguments=arguments,
                ),
                context_snapshot=snapshot,
                usage_metadata=(
                    dict(response.usage_metadata)
                    if response.usage_metadata is not None
                    else None
                ),
            )
        try:
            self._registry.get(name)
            self._registry.validate_arguments(name, arguments)
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ExecutorDecisionResult(
                error=str(exc), fatal_error=True, context_snapshot=snapshot
            )
        return ExecutorDecisionResult(
            action=ExecutorAction(
                step_id=step.id,
                action_index=action_index,
                call_id=call_id,
                action_type=ExecutorActionType.TOOL,
                tool_name=name,
                arguments=arguments,
            ),
            context_snapshot=snapshot,
            usage_metadata=(
                dict(response.usage_metadata)
                if response.usage_metadata is not None
                else None
            ),
        )

    async def assess_action(self, action: ExecutorAction) -> PolicyDecision | None:
        """只评估 frozen Tool action；不执行 Tool。"""

        if action.action_type is not ExecutorActionType.TOOL or not action.tool_name:
            raise ValueError("Risk Policy 只能评估外部 Tool action")
        tool = self._registry.get(action.tool_name)
        self._registry.validate_arguments(action.tool_name, action.arguments)
        if self._policy is None:
            return None
        decision = await self._policy.assess(
            call_id=action.call_id,
            step_id=action.step_id,
            tool=tool,
            arguments=action.arguments,
        )
        if (
            decision.call_id != action.call_id
            or decision.step_id != action.step_id
            or decision.tool_name != action.tool_name
        ):
            raise ValueError("Tool Risk Policy 返回的 action identity 不一致，动作未执行")
        return decision

    def validate_frozen_action(self, action: ExecutorAction) -> None:
        """在 journal 与 invoke 前重新校验 frozen action identity/schema。"""

        if action.action_type is not ExecutorActionType.TOOL or not action.tool_name:
            raise ValueError("execute_tool_action 需要完整 Tool action")
        self._registry.get(action.tool_name)
        self._registry.validate_arguments(action.tool_name, action.arguments)

    def is_replay_safe(self, action: ExecutorAction) -> bool:
        """只采信 Host 已写入 Tool metadata 的显式 replay_safe。"""

        self.validate_frozen_action(action)
        tool = self._registry.get(action.tool_name or "")
        metadata = getattr(tool, "metadata", {}) or {}
        return metadata.get("replay_safe") is True

    async def invoke_frozen_action(self, action: ExecutorAction) -> Any:
        """执行一个已确定的 Tool action；调用方负责 journal ordering。"""

        self.validate_frozen_action(action)
        return await self._registry.invoke(action.tool_name or "", action.arguments)

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """运行当前 Step 的单动作循环，不直接修改 PlanStep 状态。"""

        if not self._registry.list_tools():
            return self._error_result(
                step_id=step.id,
                action_count=0,
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )

        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        model_with_tools = self._model.bind_tools(schemas)
        messages = build_executor_messages(
            task_spec=task_spec,
            step=step,
            task_results=task_results,
            tool_calls=tool_calls,
            step_results=step_results,
            verification_feedback=verification_feedback,
            policy_decisions=policy_decisions,
            approval_records=approval_records,
            context_builder=self._context_builder,
        )
        new_records: list[ToolCallRecord] = []
        new_decisions: list[PolicyDecision] = []
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )

        for action_index in range(self._max_actions_per_step):
            action_count = action_index + 1
            try:
                response = await model_with_tools.ainvoke(messages)
            except Exception as exc:
                error_detail = redact_sensitive_values(str(exc))
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 模型调用失败: "
                        f"{type(exc).__name__}: {error_detail}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            if not isinstance(response, AIMessage):
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor 模型未返回 AIMessage",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            actions = response.tool_calls
            if len(actions) != 1:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 每轮必须返回且只能返回一个 Tool Call; "
                        f"实际数量: {len(actions)}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            action = actions[0]
            action_name = action["name"]
            arguments = action["args"]
            call_id = action.get("id")
            if not call_id:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor Tool Call 缺少 tool_call_id",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            if call_id in seen_call_ids:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=f"Executor Tool Call ID 重复: {call_id}",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            seen_call_ids.add(call_id)

            if action_name == FINISH_STEP_NAME:
                finish_error = self._validate_finish_arguments(arguments)
                if finish_error is not None:
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=finish_error,
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                return ExecutorRunResult(
                    outcome=StepExecutionOutcome(
                        step_id=step.id,
                        claimed_complete=True,
                        summary=arguments["summary"],
                        evidence=arguments["evidence"],
                        output=arguments["output"],
                        action_count=action_count,
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                )

            try:
                tool = self._registry.get(action_name)
            except UnknownToolError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            try:
                self._registry.validate_arguments(action_name, arguments)
            except ToolArgumentsError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            if self._policy is not None:
                try:
                    decision = await self._policy.assess(
                        call_id=call_id,
                        step_id=step.id,
                        tool=tool,
                        arguments=arguments,
                    )
                except Exception as exc:
                    error_detail = redact_sensitive_values(str(exc))
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=(
                            "Tool Risk Policy 评估失败，动作未执行: "
                            f"{type(exc).__name__}: {error_detail}"
                        ),
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                if (
                    decision.call_id != call_id
                    or decision.step_id != step.id
                    or decision.tool_name != action_name
                ):
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error="Tool Risk Policy 返回的 action identity 不一致，动作未执行",
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                new_decisions.append(decision)
                if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                    return ExecutorRunResult(
                        outcome=None,
                        tool_calls=new_records,
                        pending_action=PendingToolAction(
                            call_id=call_id,
                            step_id=step.id,
                            tool_name=action_name,
                            arguments=dict(arguments),
                            policy_decision=decision,
                        ),
                        policy_decisions=new_decisions,
                    )
                if decision.outcome is PolicyOutcome.DENY:
                    messages.append(response)
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "status": "denied",
                                    "policy_decision": decision.model_dump(
                                        mode="json"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                            tool_call_id=call_id,
                            name=action_name,
                            status="error",
                        )
                    )
                    continue

            try:
                result = await self._registry.invoke(action_name, arguments)
            except ToolArgumentsError as exc:  # pragma: no cover - 已预校验
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            except Exception as exc:
                error = redact_sensitive_values(str(exc))
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.FAILED,
                    error=f"{type(exc).__name__}: {error}",
                )
                observation = {
                    "status": "failed",
                    "error": record.error,
                }
                tool_message_status = "error"
            else:
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.SUCCESS,
                    result=result,
                )
                observation = {"status": "success", "result": result}
                tool_message_status = "success"

            new_records.append(record)
            messages.append(response)
            messages.append(
                ToolMessage(
                    content=json.dumps(observation, ensure_ascii=False, default=str),
                    tool_call_id=call_id,
                    name=action_name,
                    status=tool_message_status,
                )
            )

        return self._error_result(
            step_id=step.id,
            action_count=self._max_actions_per_step,
            error=(
                "Executor action limit reached: "
                f"{self._max_actions_per_step}"
            ),
            tool_calls=new_records,
            policy_decisions=new_decisions,
            fatal_error=False,
        )

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """执行一次冻结动作；不重新调用 LLM 或 Risk Policy。"""

        matching = [
            record
            for record in approval_records
            if record.call_id == pending_action.call_id
            and record.step_id == pending_action.step_id
            and record.tool_name == pending_action.tool_name
        ]
        if len(matching) != 1 or matching[0].decision is not ApprovalChoice.APPROVE:
            return ApprovedActionResult(
                fatal_error=True,
                error="冻结 Tool action 缺少唯一 APPROVE record",
            )
        try:
            self._registry.get(pending_action.tool_name)
            self._registry.validate_arguments(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ApprovedActionResult(fatal_error=True, error=str(exc))

        try:
            result = await self._registry.invoke(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.FAILED,
                error=f"{type(exc).__name__}: {detail}",
            )
        else:
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.SUCCESS,
                result=result,
            )
        return ApprovedActionResult(tool_call=record)

    @staticmethod
    def _validate_finish_arguments(arguments: dict[str, Any]) -> str | None:
        parameters = FINISH_STEP_SCHEMA["function"]["parameters"]
        try:
            Draft202012Validator(parameters).validate(arguments)
        except ValidationError as exc:
            return f"finish_step 参数无效: {exc.message}"
        return None

    @staticmethod
    def _error_result(
        *,
        step_id: int,
        action_count: int,
        error: str,
        tool_calls: list[ToolCallRecord] | None = None,
        policy_decisions: list[PolicyDecision] | None = None,
        fatal_error: bool,
    ) -> ExecutorRunResult:
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_count,
                error=redact_sensitive_values(error),
            ),
            tool_calls=tool_calls or [],
            policy_decisions=policy_decisions or [],
            fatal_error=fatal_error,
        )

```

### src/taskpilot/graph.py

```python
"""TaskPilot durable action-level LangGraph 工作流。"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from typing import Any, Literal, Sequence, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.context import ContextBuilder
from taskpilot.executor import ExecutorRunResult, TaskExecutor, TaskExecutorLike
from taskpilot.models import (
    ExecutorActionType,
    PlanStep,
    PlanStepStatus,
    ReplanRecord,
    ReplanRequest,
    ReplanTrigger,
    StepExecutionOutcome,
    StepResult,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.persistence.journal import ActionJournalLike, InMemoryActionJournal
from taskpilot.persistence.models import (
    ActionJournalRecord,
    ExecutionFaultInjector,
    ExecutionFaultPoint,
    JournalStatus,
    NoOpExecutionFaultInjector,
    RecoveryChoice,
    RecoveryIssue,
    RecoveryResponse,
)
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.replanner import (
    TaskReplanner,
    TaskReplannerLike,
    validate_replan_proposal,
)
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    ApprovalResponse,
    PendingToolAction,
    PolicyOutcome,
    redact_preview,
)
from taskpilot.state import TaskState, TaskStateUpdate
from taskpilot.tools.registry import ToolRegistry
from taskpilot.trace import (
    NoOpTraceRecorder,
    TraceEventType,
    TraceRecorderLike,
    make_trace_event,
)
from taskpilot.verifier import (
    TaskVerifier,
    TaskVerifierLike,
    incomplete_outcome_rejection,
    validate_step_verification,
    validate_task_verification,
)


def initialize_task(state: TaskState) -> TaskStateUpdate:
    """将新创建的任务推进到运行状态。"""

    # 只允许 created → running，避免意外覆盖完成、失败等其他状态。
    if state["status"] == TaskStatus.CREATED:
        return {"status": TaskStatus.RUNNING}
    return {}


def analyze_task(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
) -> TaskStateUpdate:
    """调用 Analyzer，并将结构化结果写回任务状态。"""

    try:
        task_spec = analyzer.analyze(state["user_input"])
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Analyzer 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }
    return {"task_spec": task_spec, "error": None}


def plan_task(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
) -> TaskStateUpdate:
    """调用 Planner，并选出第一条依赖已满足的待执行步骤。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Task Planner 无法运行: task_spec 缺失",
        }

    try:
        task_plan = planner.plan(task_spec)
        initial_steps = [
            step.model_copy(update={"revision": 0}) for step in task_plan.steps
        ]
        current_step_id = find_next_executable_step(initial_steps)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "plan": [],
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Planner 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if current_step_id is None:
        return {
            "plan": initial_steps,
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": "Task Planner 返回的计划没有可执行步骤",
        }
    return {
        "plan": initial_steps,
        "current_step_id": current_step_id,
        "plan_revision": 0,
        "replan_count": 0,
        "replan_history": [],
        "pending_replan": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_step(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """只执行 current_step_id 对应步骤，并保存 Executor 声明。"""

    current_step_id = state["current_step_id"]
    if current_step_id is None:
        return _failed_update(state["error"] or "Executor 无法运行: current_step_id 缺失")
    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update(state["error"] or "Executor 无法运行: task_spec 缺失")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            "Executor 无法定位唯一当前步骤: "
            f"current_step_id={current_step_id}"
        )

    current_step = state["plan"][step_index]
    if current_step.status not in {
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    }:
        return _failed_update(
            f"Executor 不能执行状态为 {current_step.status.value} 的步骤 "
            f"{current_step.id}"
        )

    running_step = current_step.model_copy(
        update={"status": PlanStepStatus.RUNNING}
    )
    updated_plan = list(state["plan"])
    updated_plan[step_index] = running_step
    prior_step_tool_calls = [
        record
        for record in state["tool_calls"]
        if record.step_id == current_step_id
    ]

    try:
        execution = await executor.execute(
            task_spec=task_spec,
            step=running_step,
            task_results=state["task_results"],
            tool_calls=prior_step_tool_calls,
            step_results=state["step_results"],
            verification_feedback=state.get("step_verification"),
            policy_decisions=state.get("policy_decisions", []),
            approval_records=state.get("approval_records", []),
        )
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        error = f"Task Executor 调用失败: {type(exc).__name__}: {error_detail}"
        return {
            "plan": updated_plan,
            "step_outcome": StepExecutionOutcome(
                step_id=current_step_id,
                claimed_complete=False,
                action_count=0,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }

    return _execution_state_update(
        state=state,
        updated_plan=updated_plan,
        execution=execution,
    )


def prepare_approval(state: TaskState) -> TaskStateUpdate:
    """在 interrupt 前提交 WAITING_APPROVAL，且不执行目标 Tool。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("prepare_approval 需要 pending_action")
    if pending.step_id != state["current_step_id"]:
        return _failed_update("pending_action.step_id 与当前步骤不一致")
    return {"status": TaskStatus.WAITING_APPROVAL, "error": None}


def human_approval(state: TaskState) -> TaskStateUpdate:
    """通过 LangGraph interrupt 获取只含 approve/reject 的 resume value。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("human_approval 需要 pending_action")
    decision = pending.policy_decision
    payload = {
        "type": "tool_approval",
        "call_id": pending.call_id,
        "step_id": pending.step_id,
        "tool_name": pending.tool_name,
        "risk_level": decision.risk_level.value,
        "reason": decision.reason,
        "arguments_preview": decision.preview,
        "tool_source": decision.tool_source,
    }
    # interrupt 之前没有外部副作用；resume 时该 node 会从头重放到这里。
    resume_value = interrupt(payload)
    try:
        response = ApprovalResponse.model_validate(resume_value)
    except ValidationError as exc:
        return {
            "status": TaskStatus.FAILED,
            "error": f"Human approval resume value 无效，Tool 未执行: {exc}",
        }
    record = ApprovalRecord(
        call_id=pending.call_id,
        step_id=pending.step_id,
        tool_name=pending.tool_name,
        decision=response.decision,
        reason=response.reason,
    )
    return {
        "approval_records": state.get("approval_records", []) + [record],
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_approved_action(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """不再询问模型，执行 PendingToolAction 中冻结的 exact action 一次。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("execute_approved_action 需要 pending_action")
    result = await executor.execute_approved_action(
        pending_action=pending,
        approval_records=state.get("approval_records", []),
    )
    if result.fatal_error or result.tool_call is None:
        return {
            "status": TaskStatus.FAILED,
            "error": result.error or "获批 Tool action 未返回执行记录",
        }
    return {
        "tool_calls": state["tool_calls"] + [result.tool_call],
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def handle_rejection(state: TaskState) -> TaskStateUpdate:
    """清除被拒动作，让 Executor 从审批历史中看到拒绝并选择替代方案。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("handle_rejection 需要 pending_action")
    matches = [
        record
        for record in state.get("approval_records", [])
        if record.call_id == pending.call_id
        and record.step_id == pending.step_id
        and record.tool_name == pending.tool_name
    ]
    if len(matches) != 1 or matches[0].decision is not ApprovalChoice.REJECT:
        return _failed_update("被拒动作缺少唯一 REJECT approval record")
    return {
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_step(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    max_replans: int = 0,
) -> TaskStateUpdate:
    """验证当前 Step 声明，并记录接受或拒绝结论。"""

    current_step_id = state["current_step_id"]
    task_spec = state["task_spec"]
    outcome = state["step_outcome"]
    if current_step_id is None or task_spec is None or outcome is None:
        return _failed_update("Step Verifier 无法运行: 当前步骤上下文不完整")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"Step Verifier 无法定位唯一当前步骤: {current_step_id}"
        )
    step = state["plan"][step_index]
    if outcome.step_id != step.id:
        return _failed_update(
            "StepExecutionOutcome.step_id 与当前步骤不一致: "
            f"expected={step.id}, actual={outcome.step_id}"
        )

    current_calls = [
        record for record in state["tool_calls"] if record.step_id == step.id
    ]
    dependency_results = [
        result
        for result in state["step_results"]
        if result.step_id in step.depends_on
    ]
    try:
        if outcome.claimed_complete:
            result = verifier.verify_step(
                task_spec=task_spec,
                step=step,
                outcome=outcome,
                tool_calls=current_calls,
                dependency_results=dependency_results,
            )
        else:
            # 尚未 claim complete 时确定性拒绝，不浪费一次模型调用。
            result = incomplete_outcome_rejection(step, outcome)
        validate_step_verification(step, result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        failed_step = step.model_copy(update={"status": PlanStepStatus.FAILED})
        return {
            "plan": _replace_step(state["plan"], step_index, failed_step),
            "status": TaskStatus.FAILED,
            "error": (
                f"Step Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.verified:
        return {
            "step_verification": result,
            "status": TaskStatus.RUNNING,
            "error": None,
        }

    retry_count = step.retry_count + 1
    reached_limit = retry_count >= max_step_attempts
    rejected_step = step.model_copy(
        update={
            "retry_count": retry_count,
            "status": (
                PlanStepStatus.FAILED
                if reached_limit
                else PlanStepStatus.RUNNING
            ),
        }
    )
    can_replan = reached_limit and state.get("replan_count", 0) < max_replans
    update: TaskStateUpdate = {
        "plan": _replace_step(state["plan"], step_index, rejected_step),
        "step_verification": result,
        "status": (
            TaskStatus.RUNNING if can_replan or not reached_limit else TaskStatus.FAILED
        ),
        "error": None,
    }
    if reached_limit:
        reason = (
            f"Step {step.id} 连续被 Verifier 拒绝，已达到 "
            f"max_step_attempts={max_step_attempts}: "
            f"{result.feedback or '未提供反馈'}"
        )
        if can_replan:
            update["pending_replan"] = ReplanRequest(
                trigger=ReplanTrigger.STEP_ATTEMPTS_EXHAUSTED,
                failed_step_id=step.id,
                reason=reason,
                verification_feedback=result.feedback,
            )
            update["error"] = None
        else:
            update["error"] = (
                reason
                if max_replans == 0
                else f"{reason}; replan budget exhausted"
            )
    return update


def advance_step(state: TaskState) -> TaskStateUpdate:
    """接受已验证 Claim，沉淀 StepResult 并选择下一可执行步骤。"""

    current_step_id = state["current_step_id"]
    outcome = state["step_outcome"]
    verification = state["step_verification"]
    if (
        current_step_id is None
        or outcome is None
        or verification is None
        or not verification.verified
    ):
        return _failed_update("advance_step 需要已通过验证的当前步骤")
    if outcome.step_id != current_step_id or verification.step_id != current_step_id:
        return _failed_update("advance_step 的步骤 ID 不一致")
    if not outcome.summary:
        return _failed_update("已验证的 StepExecutionOutcome 缺少 summary")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"advance_step 无法定位唯一当前步骤: {current_step_id}"
        )
    completed_step = state["plan"][step_index].model_copy(
        update={"status": PlanStepStatus.COMPLETED}
    )
    updated_plan = _replace_step(state["plan"], step_index, completed_step)
    result = StepResult(
        step_id=current_step_id,
        summary=outcome.summary,
        output=outcome.output,
        evidence=outcome.evidence,
    )
    updated_results = _upsert_step_result(
        state["step_results"],
        result,
        updated_plan,
    )

    try:
        next_step_id = find_next_executable_step(updated_plan)
    except ValueError as exc:
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": str(exc),
        }

    active_revision = state.get("plan_revision", 0)
    if next_step_id is None and not current_revision_finished(
        updated_plan, active_revision
    ):
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": "Plan 状态异常: 没有 pending 步骤，但并非所有步骤已完成",
        }

    return {
        "plan": updated_plan,
        "step_results": updated_results,
        "current_step_id": next_step_id,
        # 新步骤不能继承上一条步骤的 Claim 或反馈。
        "step_outcome": None,
        "step_verification": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_task(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_replans: int = 0,
) -> TaskStateUpdate:
    """所有步骤完成后，独立验证 Task completion criteria。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update("Task Verifier 无法运行: task_spec 缺失")
    try:
        result = verifier.verify_task(
            task_spec=task_spec,
            step_results=state["step_results"],
            task_results=state["task_results"],
        )
        validate_task_verification(result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.completed:
        return {
            "verification": result,
            "current_step_id": None,
            "status": TaskStatus.COMPLETED,
            "error": None,
        }
    next_hint = f"; next_action={result.next_action}" if result.next_action else ""
    reason = f"Task Verifier 拒绝任务完成: {result.reason}{next_hint}"
    if state.get("replan_count", 0) < max_replans:
        return {
            "verification": result,
            "current_step_id": None,
            "pending_replan": ReplanRequest(
                trigger=ReplanTrigger.TASK_VERIFICATION_FAILED,
                reason=result.reason,
                missing_requirements=result.missing_requirements,
            ),
            "status": TaskStatus.RUNNING,
            "error": None,
        }
    return {
        "verification": result,
        "current_step_id": None,
        "status": TaskStatus.FAILED,
        "error": (
            reason
            if max_replans == 0
            else f"{reason}; replan budget exhausted"
        ),
    }


def current_revision_finished(plan: Sequence[PlanStep], revision: int) -> bool:
    """当前 revision 没有 pending/running 即可进入 Task Verifier。"""

    active = [step for step in plan if step.revision == revision]
    return bool(active) and not any(
        step.status in {PlanStepStatus.PENDING, PlanStepStatus.RUNNING}
        for step in active
    )


async def _trace(
    recorder: TraceRecorderLike,
    state: TaskState,
    event_type: TraceEventType,
    *,
    step_id: int | None = None,
    call_id: str | None = None,
    status: str | None = None,
    latency_ms: float | None = None,
    payload: dict[str, Any] | None = None,
    plan_revision_override: int | None = None,
) -> None:
    """把 state 中的稳定 identity 附到独立 Trace 事件。"""

    await recorder.record(
        make_trace_event(
            task_id=state["task_id"],
            event_type=event_type,
            step_id=step_id,
            call_id=call_id,
            plan_revision=(
                plan_revision_override
                if plan_revision_override is not None
                else state.get("plan_revision", 0)
            ),
            status=status,
            latency_ms=latency_ms,
            payload=payload,
        )
    )


async def initialize_task_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = initialize_task(state)
    await _trace(
        trace_recorder,
        state,
        TraceEventType.TASK_STARTED,
        status=TaskStatus.RUNNING.value,
        payload={"user_input_length": len(state["user_input"])},
    )
    return update


async def analyze_task_traced(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = analyze_task(state, analyzer=analyzer)
    latency = (perf_counter() - started) * 1000
    failed = update.get("status") is TaskStatus.FAILED
    await _trace(
        trace_recorder,
        state,
        TraceEventType.TASK_ANALYZED,
        status="failed" if failed else "success",
        latency_ms=latency,
        payload={"error": update.get("error")} if failed else {},
    )
    if failed:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


async def plan_task_traced(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = plan_task(state, planner=planner)
    latency = (perf_counter() - started) * 1000
    failed = update.get("status") is TaskStatus.FAILED
    await _trace(
        trace_recorder,
        state,
        TraceEventType.PLAN_CREATED,
        status="failed" if failed else "success",
        latency_ms=latency,
        payload={
            "step_ids": [step.id for step in update.get("plan", [])],
            **({"error": update.get("error")} if failed else {}),
        },
    )
    if failed:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


async def begin_step_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = begin_step(state)
    if update.get("status") is not TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.STEP_STARTED,
            step_id=state.get("current_step_id"),
            status=PlanStepStatus.RUNNING.value,
        )
    return update


async def replan_task(
    state: TaskState,
    *,
    replanner: TaskReplannerLike,
    context_builder: ContextBuilder,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """验证并接受一版只覆盖剩余工作的全新 revision。"""

    request = state.get("pending_replan")
    task_spec = state.get("task_spec")
    if request is None or task_spec is None:
        return _failed_update("replan_task 缺少 pending_replan 或 TaskSpec")
    if state.get("replan_count", 0) >= max_replans:
        error = "replan budget exhausted"
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": error, "trigger": request.trigger.value},
        )
        return _failed_update(error)

    started = perf_counter()
    try:
        snapshot = context_builder.build_replan_context(
            task_spec=task_spec,
            plan=state["plan"],
            step_results=state["step_results"],
            tool_calls=state["tool_calls"],
            request=request,
            plan_revision=state.get("plan_revision", 0),
            replan_count=state.get("replan_count", 0),
        )
        proposal = await asyncio.to_thread(replanner.replan, snapshot)
        validate_replan_proposal(proposal, state["plan"])
        next_revision = state.get("plan_revision", 0) + 1
        new_steps = [
            step.model_copy(
                update={
                    "revision": next_revision,
                    "status": PlanStepStatus.PENDING,
                    "retry_count": 0,
                }
            )
            for step in proposal.steps
        ]
        remaining_ids = [
            step.id
            for step in state["plan"]
            if step.status in {PlanStepStatus.PENDING, PlanStepStatus.RUNNING}
        ]
        replaced_ids = list(
            dict.fromkeys(
                ([request.failed_step_id] if request.failed_step_id is not None else [])
                + remaining_ids
            )
        )
        historical = [
            step.model_copy(update={"status": PlanStepStatus.SKIPPED})
            if step.id in remaining_ids
            else step
            for step in state["plan"]
        ]
        updated_plan = historical + new_steps
        next_step_id = find_next_executable_step(updated_plan)
        if next_step_id is None:
            raise ValueError("已接受的 ReplanProposal 没有可执行新步骤")
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        error = f"Task Replanner 调用或校验失败: {type(exc).__name__}: {detail}"
        await _trace(
            trace_recorder,
            state,
            TraceEventType.PLAN_REPLANNED,
            status="failed",
            latency_ms=(perf_counter() - started) * 1000,
            payload={"trigger": request.trigger.value, "error": error},
        )
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": error},
        )
        return _failed_update(error)

    record = ReplanRecord(
        revision=next_revision,
        trigger=request.trigger,
        reason=proposal.reason,
        replaced_step_ids=replaced_ids,
        new_step_ids=[step.id for step in new_steps],
    )
    await _trace(
        trace_recorder,
        state,
        TraceEventType.PLAN_REPLANNED,
        plan_revision_override=next_revision,
        status="success",
        latency_ms=(perf_counter() - started) * 1000,
        payload={
            "trigger": request.trigger.value,
            "failed_step_id": request.failed_step_id,
            "reason": proposal.reason,
            "replaced_step_ids": replaced_ids,
            "new_revision": next_revision,
            "new_step_ids": record.new_step_ids,
            "context_chars": snapshot.total_chars,
        },
    )
    return {
        "plan": updated_plan,
        "plan_revision": next_revision,
        "replan_count": state.get("replan_count", 0) + 1,
        "replan_history": state.get("replan_history", []) + [record],
        "current_step_id": next_step_id,
        "current_action_index": 0,
        "step_outcome": None,
        "step_verification": None,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "pending_replan": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def find_next_executable_step(plan: Sequence[PlanStep]) -> int | None:
    """按计划顺序选择第一条依赖均已完成的 pending 步骤。"""

    completed_ids = {
        step.id for step in plan if step.status == PlanStepStatus.COMPLETED
    }
    pending_steps = [
        step for step in plan if step.status == PlanStepStatus.PENDING
    ]
    for step in pending_steps:
        if all(dependency_id in completed_ids for dependency_id in step.depends_on):
            return step.id
    if pending_steps:
        blocked = {
            step.id: [
                dependency_id
                for dependency_id in step.depends_on
                if dependency_id not in completed_ids
            ]
            for step in pending_steps
        }
        raise ValueError(
            "Plan 依赖阻塞: 存在 pending 步骤但没有可执行步骤; "
            f"unmet_dependencies={blocked}"
        )
    return None


def _execution_state_update(
    *,
    state: TaskState,
    updated_plan: list[PlanStep],
    execution: ExecutorRunResult,
) -> TaskStateUpdate:
    """把 Executor 返回值合并到 LangGraph 状态。"""

    update: TaskStateUpdate = {
        "plan": updated_plan,
        "tool_calls": state["tool_calls"] + execution.tool_calls,
        "policy_decisions": (
            state.get("policy_decisions", []) + execution.policy_decisions
        ),
        "step_outcome": execution.outcome,
        "pending_action": execution.pending_action,
        "status": (
            TaskStatus.FAILED if execution.fatal_error else TaskStatus.RUNNING
        ),
        "error": (
            execution.outcome.error
            if execution.fatal_error and execution.outcome is not None
            else None
        ),
    }
    if execution.fatal_error and execution.outcome is None:
        update["error"] = "Executor fatal failure 未提供 StepExecutionOutcome"
    if not execution.fatal_error and execution.outcome is None:
        if execution.pending_action is None:
            update["status"] = TaskStatus.FAILED
            update["error"] = "Executor 未返回 outcome 或 pending_action"
    return update


def _find_unique_step_index(plan: Sequence[PlanStep], step_id: int) -> int | None:
    matches = [index for index, step in enumerate(plan) if step.id == step_id]
    return matches[0] if len(matches) == 1 else None


def _replace_step(
    plan: Sequence[PlanStep],
    index: int,
    step: PlanStep,
) -> list[PlanStep]:
    updated = list(plan)
    updated[index] = step
    return updated


def _upsert_step_result(
    step_results: Sequence[StepResult],
    result: StepResult,
    plan: Sequence[PlanStep],
) -> list[StepResult]:
    """按 step_id 替换正式结果，并保持计划顺序且不产生重复。"""

    by_step_id = {
        item.step_id: item for item in step_results if item.step_id != result.step_id
    }
    by_step_id[result.step_id] = result
    plan_order = {step.id: index for index, step in enumerate(plan)}
    return sorted(
        by_step_id.values(),
        key=lambda item: plan_order.get(item.step_id, len(plan)),
    )


def _failed_update(error: str) -> TaskStateUpdate:
    return {"status": TaskStatus.FAILED, "error": error}


def _route_after_plan(state: TaskState) -> Literal["execute_step", "end"]:
    return "end" if state["status"] == TaskStatus.FAILED else "execute_step"


def _route_after_execute(
    state: TaskState,
) -> Literal["prepare_approval", "verify_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    return "verify_step"


def _route_after_human_approval(
    state: TaskState,
) -> Literal["execute_approved_action", "handle_rejection", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    records = state.get("approval_records", [])
    if not records:
        return "end"
    return (
        "execute_approved_action"
        if records[-1].decision is ApprovalChoice.APPROVE
        else "handle_rejection"
    )


def _route_after_step_verification(
    state: TaskState,
) -> Literal["advance_step", "execute_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    verification = state["step_verification"]
    if verification is not None and verification.verified:
        return "advance_step"
    return "execute_step"


def _route_after_advance(
    state: TaskState,
) -> Literal["execute_step", "verify_task", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state["current_step_id"] is None:
        return "verify_task"
    return "execute_step"


def _build_legacy_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """构建带 Policy/HITL、两层验证和有限重试的状态图。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = (
        executor
        if executor is not None
        else TaskExecutor(registry=ToolRegistry())
    )
    active_verifier = verifier if verifier is not None else TaskVerifier()

    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node(
        "analyze_task",
        partial(analyze_task, analyzer=active_analyzer),
    )
    builder.add_node("plan_task", partial(plan_task, planner=active_planner))
    builder.add_node(
        "execute_step",
        partial(execute_step, executor=active_executor),
    )
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("human_approval", human_approval)
    builder.add_node(
        "execute_approved_action",
        partial(execute_approved_action, executor=active_executor),
    )
    builder.add_node("handle_rejection", handle_rejection)
    builder.add_node(
        "verify_step",
        partial(
            verify_step,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
        ),
    )
    builder.add_node("advance_step", advance_step)
    builder.add_node(
        "verify_task",
        partial(verify_task, verifier=active_verifier),
    )

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task",
        _route_after_plan,
        {"execute_step": "execute_step", "end": END},
    )
    builder.add_conditional_edges(
        "execute_step",
        _route_after_execute,
        {
            "prepare_approval": "prepare_approval",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_approved_action",
            "handle_rejection": "handle_rejection",
            "end": END,
        },
    )
    builder.add_edge("execute_approved_action", "execute_step")
    builder.add_edge("handle_rejection", "execute_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_step_verification,
        {
            "advance_step": "advance_step",
            "execute_step": "execute_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "advance_step",
        _route_after_advance,
        {
            "execute_step": "execute_step",
            "verify_task": "verify_task",
            "end": END,
        },
    )
    builder.add_edge("verify_task", END)
    return builder.compile(checkpointer=checkpointer)


def begin_step(state: TaskState) -> TaskStateUpdate:
    """进入新 Step，并为本次 verification attempt 初始化 action counter。"""

    step_id = state.get("current_step_id")
    if step_id is None:
        return _failed_update("begin_step 需要 current_step_id")
    index = _find_unique_step_index(state["plan"], step_id)
    if index is None:
        return _failed_update(f"begin_step 无法定位唯一当前步骤: {step_id}")
    step = state["plan"][index]
    running = step.model_copy(update={"status": PlanStepStatus.RUNNING})
    return {
        "plan": _replace_step(state["plan"], index, running),
        "current_action_index": 0,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "step_outcome": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def decide_action(
    state: TaskState,
    *,
    executor: TaskExecutor,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """最多调用模型一次，并把 frozen ExecutorAction 写回 state。"""

    step_id = state.get("current_step_id")
    task_spec = state.get("task_spec")
    if step_id is None or task_spec is None:
        return _failed_update("decide_action 缺少当前 Step 或 TaskSpec")
    index = _find_unique_step_index(state["plan"], step_id)
    if index is None:
        return _failed_update(f"decide_action 无法定位唯一当前步骤: {step_id}")
    current_index = state.get("current_action_index", 0)
    if current_index >= executor.max_actions_per_step:
        return {
            "step_outcome": StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=current_index,
                error=f"Executor action limit reached: {executor.max_actions_per_step}",
            ),
            "pending_executor_action": None,
            "status": TaskStatus.RUNNING,
            "error": None,
        }
    action_index = current_index + 1
    started = perf_counter()
    result = await executor.decide_action(
        task_spec=task_spec,
        step=state["plan"][index],
        action_index=action_index,
        task_results=state["task_results"],
        tool_calls=state["tool_calls"],
        step_results=state["step_results"],
        verification_feedback=state.get("step_verification"),
        policy_decisions=state.get("policy_decisions", []),
        approval_records=state.get("approval_records", []),
    )
    latency = (perf_counter() - started) * 1000
    if result.context_snapshot is not None:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.CONTEXT_BUILT,
            step_id=step_id,
            status="success",
            payload={
                "total_chars": result.context_snapshot.total_chars,
                "included_tool_calls": result.context_snapshot.included_tool_calls,
                "omitted_tool_calls": result.context_snapshot.omitted_tool_calls,
                "truncated_sections": result.context_snapshot.truncated_sections,
            },
        )
    await _trace(
        trace_recorder,
        state,
        TraceEventType.LLM_ACTION_DECIDED,
        step_id=step_id,
        call_id=result.action.call_id if result.action is not None else None,
        status="failed" if result.fatal_error else "success",
        latency_ms=latency,
        payload={
            "model_invoked": result.context_snapshot is not None,
            **(
                {
                    "action_type": result.action.action_type.value,
                    "tool_name": result.action.tool_name,
                }
                if result.action is not None
                else {"error": result.error}
            ),
            **(
                {"usage_metadata": result.usage_metadata}
                if result.usage_metadata is not None
                else {}
            ),
        },
    )
    if result.fatal_error:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            step_id=step_id,
            status=TaskStatus.FAILED.value,
            payload={"error": result.error},
        )
    if result.fatal_error or result.action is None:
        error = result.error or "Executor decision 未返回 action"
        return {
            "current_action_index": action_index,
            "step_outcome": StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_index,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }
    action = result.action
    if action.step_id != step_id or action.action_index != action_index:
        return _failed_update("ExecutorAction identity 与 Graph 当前边界不一致")
    return {
        "current_action_index": action_index,
        "pending_executor_action": action,
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def build_step_outcome(state: TaskState) -> TaskStateUpdate:
    """把 FINISH_STEP frozen arguments 转换为未验证的完成声明。"""

    action = state.get("pending_executor_action")
    if action is None or action.action_type is not ExecutorActionType.FINISH_STEP:
        return _failed_update("build_step_outcome 需要 FINISH_STEP action")
    arguments = action.arguments
    return {
        "step_outcome": StepExecutionOutcome(
            step_id=action.step_id,
            claimed_complete=True,
            summary=arguments["summary"],
            evidence=arguments["evidence"],
            output=arguments["output"],
            action_count=action.action_index,
        ),
        "pending_executor_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def assess_policy(
    state: TaskState,
    *,
    executor: TaskExecutor,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """评估 frozen action；审批之前绝不执行目标 Tool。"""

    action = state.get("pending_executor_action")
    if action is None:
        return _failed_update("assess_policy 需要 pending_executor_action")
    try:
        decision = await executor.assess_action(action)
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        return _failed_update(
            f"Tool Risk Policy 评估失败，动作未执行: {type(exc).__name__}: {detail}"
        )
    if decision is None:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.POLICY_DECIDED,
            step_id=action.step_id,
            call_id=action.call_id,
            status="not_configured",
        )
        return {"status": TaskStatus.RUNNING, "error": None}
    await _trace(
        trace_recorder,
        state,
        TraceEventType.POLICY_DECIDED,
        step_id=action.step_id,
        call_id=action.call_id,
        status=decision.outcome.value,
        payload={"reason": decision.reason, "risk_level": decision.risk_level.value},
    )
    update: TaskStateUpdate = {
        "policy_decisions": state.get("policy_decisions", []) + [decision],
        "status": TaskStatus.RUNNING,
        "error": None,
    }
    if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
        update["pending_action"] = PendingToolAction(
            call_id=action.call_id,
            step_id=action.step_id,
            tool_name=action.tool_name or "",
            arguments=dict(action.arguments),
            policy_decision=decision,
        )
    return update


def policy_feedback(state: TaskState) -> TaskStateUpdate:
    """保留 DENY decision，清除动作后让模型选择替代方案。"""

    return {
        "pending_executor_action": None,
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def rejection_feedback(state: TaskState) -> TaskStateUpdate:
    """校验 REJECT record 后清除两种 pending action。"""

    update = handle_rejection(state)
    if update.get("status") is not TaskStatus.FAILED:
        update["pending_executor_action"] = None
    return update


async def prepare_approval_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = prepare_approval(state)
    pending = state.get("pending_action")
    if pending is not None and update.get("status") is not TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.APPROVAL_REQUESTED,
            step_id=pending.step_id,
            call_id=pending.call_id,
            status=TaskStatus.WAITING_APPROVAL.value,
            payload={"tool_name": pending.tool_name},
        )
    return update


async def human_approval_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = human_approval(state)
    pending = state.get("pending_action")
    records = update.get("approval_records", [])
    if pending is not None and records:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.APPROVAL_RESOLVED,
            step_id=pending.step_id,
            call_id=pending.call_id,
            status=records[-1].decision.value,
            payload={"reason": records[-1].reason},
        )
    return update


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    """按 Phase 9 约定计算稳定 JSON SHA-256。"""

    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def action_key_for(state: TaskState) -> str:
    """生成包含 task/step/retry/action index 的稳定 journal key。"""

    action = state.get("pending_executor_action")
    if action is None:
        raise ValueError("action_key_for 需要 pending_executor_action")
    index = _find_unique_step_index(state["plan"], action.step_id)
    if index is None:
        raise ValueError("action_key_for 无法定位唯一 PlanStep")
    retry_count = state["plan"][index].retry_count
    return f"{state['task_id']}:{action.step_id}:{retry_count}:{action.action_index}"


def _journal_identity_error(
    record: ActionJournalRecord,
    *,
    state: TaskState,
    arguments_hash: str,
    replay_safe: bool,
) -> str | None:
    action = state["pending_executor_action"]
    assert action is not None
    index = _find_unique_step_index(state["plan"], action.step_id)
    assert index is not None
    expected = (
        state["task_id"], action.step_id, state["plan"][index].retry_count,
        action.action_index, action.call_id, action.tool_name, arguments_hash,
        replay_safe,
    )
    actual = (
        record.task_id, record.step_id, record.retry_count, record.action_index,
        record.call_id, record.tool_name, record.arguments_hash, record.replay_safe,
    )
    return None if actual == expected else "Action Journal identity/hash 不一致，拒绝恢复"


def _materialize_tool_call(
    state: TaskState,
    record: ActionJournalRecord,
) -> TaskStateUpdate:
    action = state["pending_executor_action"]
    assert action is not None and action.tool_name is not None
    tool_call = ToolCallRecord(
        call_id=action.call_id,
        step_id=action.step_id,
        tool_name=action.tool_name,
        arguments=action.arguments,
        status=(
            ToolCallStatus.SUCCESS
            if record.status is JournalStatus.SUCCEEDED
            else ToolCallStatus.FAILED
        ),
        result=record.result,
        error=record.error,
    )
    existing = [
        item for item in state["tool_calls"]
        if item.step_id == tool_call.step_id and item.call_id == tool_call.call_id
    ]
    if existing:
        if len(existing) != 1 or existing[0].model_dump(mode="json") != tool_call.model_dump(mode="json"):
            return _failed_update("ToolCallRecord identity 内容冲突，拒绝静默覆盖")
        updated_calls = state["tool_calls"]
    else:
        updated_calls = state["tool_calls"] + [tool_call]
    return {
        "tool_calls": updated_calls,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_tool_action(
    state: TaskState,
    *,
    executor: TaskExecutor,
    journal: ActionJournalLike,
    fault_injector: ExecutionFaultInjector,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """按 STARTED → one invoke → terminal → node return 的顺序执行一次。"""

    action = state.get("pending_executor_action")
    if action is None or action.action_type is not ExecutorActionType.TOOL:
        return _failed_update("execute_tool_action 需要 frozen Tool action")
    try:
        executor.validate_frozen_action(action)
        replay_safe = executor.is_replay_safe(action)
        action_key = action_key_for(state)
        arguments_hash = canonical_arguments_hash(action.arguments)
    except Exception as exc:
        return _failed_update(f"Frozen Tool action 校验失败: {exc}")

    record = await journal.get(action_key)
    if record is not None:
        identity_error = _journal_identity_error(
            record,
            state=state,
            arguments_hash=arguments_hash,
            replay_safe=replay_safe,
        )
        if identity_error is not None:
            return _failed_update(identity_error)
        if record.status in {JournalStatus.SUCCEEDED, JournalStatus.FAILED}:
            await _trace(
                trace_recorder,
                state,
                TraceEventType.TOOL_MATERIALIZED_FROM_JOURNAL,
                step_id=action.step_id,
                call_id=action.call_id,
                status=record.status.value,
                payload={"tool_name": action.tool_name},
            )
            return _materialize_tool_call(state, record)
        authorized_attempt = record.attempt_count + 1
        has_one_shot_permit = (
            record.authorized_retry_attempt == authorized_attempt
        )
        if not replay_safe and not has_one_shot_permit:
            preview = redact_preview(action.arguments)
            await _trace(
                trace_recorder,
                state,
                TraceEventType.RECOVERY_REQUIRED,
                step_id=action.step_id,
                call_id=action.call_id,
                status=TaskStatus.INTERRUPTED.value,
                payload={"tool_name": action.tool_name, "reason": "ambiguous_started"},
            )
            return {
                "recovery_issue": RecoveryIssue(
                    action_key=action_key,
                    task_id=state["task_id"],
                    step_id=action.step_id,
                    action_index=action.action_index,
                    tool_name=action.tool_name or "",
                    call_id=action.call_id,
                    reason=("Journal 仅有 STARTED；Tool 可能尚未执行、执行中，"
                            "或远端已产生副作用但终态未提交"),
                    replay_safe=False,
                    arguments_preview=cast(dict[str, Any], preview),
                ),
                "status": TaskStatus.INTERRUPTED,
                "error": None,
            }
        record = (
            await journal.retry_started(action_key)
            if replay_safe
            else await journal.consume_authorized_retry(action_key)
        )
    else:
        index = _find_unique_step_index(state["plan"], action.step_id)
        assert index is not None
        now = datetime.now(UTC)
        record = ActionJournalRecord(
            action_key=action_key,
            task_id=state["task_id"],
            step_id=action.step_id,
            retry_count=state["plan"][index].retry_count,
            action_index=action.action_index,
            call_id=action.call_id,
            tool_name=action.tool_name or "",
            arguments_hash=arguments_hash,
            status=JournalStatus.STARTED,
            replay_safe=replay_safe,
            created_at=now,
            updated_at=now,
        )
        await journal.start(record)
        fault_injector.hit(ExecutionFaultPoint.AFTER_JOURNAL_STARTED, action_key)

    await _trace(
        trace_recorder,
        state,
        TraceEventType.TOOL_STARTED,
        step_id=action.step_id,
        call_id=action.call_id,
        status=ToolCallStatus.RUNNING.value,
        payload={
            "tool_name": action.tool_name,
            "arguments_preview": redact_preview(action.arguments),
            "replay_safe": replay_safe,
        },
    )
    invocation_started = perf_counter()
    try:
        result = await executor.invoke_frozen_action(action)
        fault_injector.hit(
            ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL,
            action_key,
        )
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        record = await journal.fail(action_key, f"{type(exc).__name__}: {detail}")
    else:
        record = await journal.succeed(action_key, result)
    terminal_type = (
        TraceEventType.TOOL_SUCCEEDED
        if record.status is JournalStatus.SUCCEEDED
        else TraceEventType.TOOL_FAILED
    )
    await _trace(
        trace_recorder,
        state,
        terminal_type,
        step_id=action.step_id,
        call_id=action.call_id,
        status=record.status.value,
        latency_ms=(perf_counter() - invocation_started) * 1000,
        payload={"tool_name": action.tool_name, "error": record.error},
    )
    fault_injector.hit(
        ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN,
        action_key,
    )
    return _materialize_tool_call(state, record)


def recovery_review(state: TaskState) -> TaskStateUpdate:
    """对不可安全重放的 ambiguous STARTED action 发起真实 interrupt。"""

    issue = state.get("recovery_issue")
    if issue is None:
        return _failed_update("recovery_review 需要 RecoveryIssue")
    payload = {
        "type": "ambiguous_tool_recovery",
        "action_key": issue.action_key,
        "tool_name": issue.tool_name,
        "reason": issue.reason,
        "arguments_preview": issue.arguments_preview,
        "choices": [RecoveryChoice.RETRY.value, RecoveryChoice.ABORT.value],
    }
    resume_value = interrupt(payload)
    try:
        response = RecoveryResponse.model_validate(resume_value)
    except ValidationError as exc:
        return _failed_update(f"Recovery resume value 无效，Tool 未执行: {exc}")
    if response.choice is RecoveryChoice.ABORT:
        updated = issue.model_copy(update={"resolution_reason": response.reason})
        return {
            "recovery_issue": updated,
            "status": TaskStatus.FAILED,
            "error": response.reason or f"Human aborted ambiguous action {issue.action_key}",
        }
    updated = issue.model_copy(
        update={"retry_authorized": False, "resolution_reason": response.reason}
    )
    return {
        "recovery_issue": updated,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def recovery_review_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = recovery_review(state)
    issue = state.get("recovery_issue")
    if issue is not None:
        status = update.get("status")
        await _trace(
            trace_recorder,
            state,
            TraceEventType.RECOVERY_RESOLVED,
            step_id=issue.step_id,
            call_id=issue.call_id,
            status=status.value if isinstance(status, TaskStatus) else str(status),
            payload={"resolution_reason": update.get("error")},
        )
    return update


async def authorize_recovery_retry(
    state: TaskState,
    *,
    journal: ActionJournalLike,
) -> TaskStateUpdate:
    """把 Human RETRY 转换为 Journal 中下一 attempt 的 one-shot permit。"""

    issue = state.get("recovery_issue")
    action = state.get("pending_executor_action")
    if issue is None or action is None:
        return _failed_update("authorize_recovery_retry 缺少 RecoveryIssue 或 frozen action")
    try:
        action_key = action_key_for(state)
    except Exception as exc:
        return _failed_update(f"Recovery action identity 无效: {exc}")
    if (
        issue.action_key != action_key
        or issue.task_id != state["task_id"]
        or issue.step_id != action.step_id
        or issue.action_index != action.action_index
        or issue.call_id != action.call_id
        or issue.tool_name != action.tool_name
    ):
        return _failed_update("RecoveryIssue 与 frozen action identity 不一致")
    try:
        record = await journal.authorize_retry(action_key)
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        return _failed_update(
            f"Recovery retry permit durable 写入失败: {type(exc).__name__}: {detail}"
        )
    if record.authorized_retry_attempt != record.attempt_count + 1:
        return _failed_update("Recovery retry permit attempt identity 不一致")
    return {
        "recovery_issue": issue.model_copy(update={"retry_authorized": False}),
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def reset_step_attempt(state: TaskState) -> TaskStateUpdate:
    """Verifier reject 后开始新 attempt，不继承上一轮 action counter。"""

    return {
        "current_action_index": 0,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "step_outcome": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def verify_step_traced(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = verify_step(
        state,
        verifier=verifier,
        max_step_attempts=max_step_attempts,
        max_replans=max_replans,
    )
    result = update.get("step_verification")
    verified = result is not None and result.verified
    await _trace(
        trace_recorder,
        state,
        TraceEventType.STEP_VERIFIED if verified else TraceEventType.STEP_REJECTED,
        step_id=state.get("current_step_id"),
        status="verified" if verified else "rejected",
        latency_ms=(perf_counter() - started) * 1000,
        payload={
            "feedback": result.feedback if result is not None else update.get("error"),
            "replan_triggered": update.get("pending_replan") is not None,
            **(
                {"replan_trigger": update["pending_replan"].trigger.value}
                if update.get("pending_replan") is not None
                else {}
            ),
        },
    )
    if update.get("status") is TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


async def verify_task_traced(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = verify_task(state, verifier=verifier, max_replans=max_replans)
    result = update.get("verification")
    completed = result is not None and result.completed
    await _trace(
        trace_recorder,
        state,
        TraceEventType.TASK_VERIFIED
        if completed
        else TraceEventType.TASK_VERIFICATION_REJECTED,
        status="completed" if completed else "rejected",
        latency_ms=(perf_counter() - started) * 1000,
        payload={
            "reason": result.reason if result is not None else update.get("error"),
            "missing_requirements": (
                result.missing_requirements if result is not None else []
            ),
            "replan_triggered": update.get("pending_replan") is not None,
        },
    )
    if completed:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_COMPLETED,
            status=TaskStatus.COMPLETED.value,
        )
    elif update.get("status") is TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


def advance_step_action_level(state: TaskState) -> TaskStateUpdate:
    """复用 Step 推进逻辑，并为下一 Step 重置 action-level 字段。"""

    update = advance_step(state)
    update["current_action_index"] = 0
    update["pending_executor_action"] = None
    update["pending_action"] = None
    update["recovery_issue"] = None
    return update


def _route_after_decision(
    state: TaskState,
) -> Literal["assess_policy", "build_step_outcome", "verify_step", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    action = state.get("pending_executor_action")
    if action is None:
        return "verify_step"
    return (
        "build_step_outcome"
        if action.action_type is ExecutorActionType.FINISH_STEP
        else "assess_policy"
    )


def _route_after_policy(
    state: TaskState,
) -> Literal["policy_feedback", "prepare_approval", "execute_tool_action", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    decisions = state.get("policy_decisions", [])
    action = state.get("pending_executor_action")
    if decisions and action is not None and decisions[-1].call_id == action.call_id:
        if decisions[-1].outcome is PolicyOutcome.DENY:
            return "policy_feedback"
    return "execute_tool_action"


def _route_after_tool_execution(
    state: TaskState,
) -> Literal["decide_action", "recovery_review", "end"]:
    if state["status"] is TaskStatus.INTERRUPTED:
        return "recovery_review"
    if state["status"] is TaskStatus.FAILED:
        return "end"
    return "decide_action"


def _route_after_recovery(
    state: TaskState,
) -> Literal["authorize_recovery_retry", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "authorize_recovery_retry"


def _route_after_recovery_authorization(
    state: TaskState,
) -> Literal["execute_tool_action", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "execute_tool_action"


def _route_after_action_verification(
    state: TaskState,
) -> Literal["advance_step", "reset_step_attempt", "replan_task", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    if state.get("pending_replan") is not None:
        return "replan_task"
    result = state.get("step_verification")
    return "advance_step" if result is not None and result.verified else "reset_step_attempt"


def _route_after_action_advance(
    state: TaskState,
) -> Literal["decide_action", "verify_task", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    return "verify_task" if state["current_step_id"] is None else "decide_action"


def _route_after_task_verification(
    state: TaskState,
) -> Literal["replan_task", "end"]:
    return "replan_task" if state.get("pending_replan") is not None else "end"


def _route_after_replan(state: TaskState) -> Literal["begin_step", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "begin_step"


def _build_action_level_graph(
    *,
    analyzer: TaskAnalyzerLike,
    planner: TaskPlannerLike,
    executor: TaskExecutor,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    checkpointer: BaseCheckpointSaver[Any] | None,
    journal: ActionJournalLike,
    fault_injector: ExecutionFaultInjector,
    replanner: TaskReplannerLike,
    context_builder: ContextBuilder,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
) -> CompiledStateGraph:
    builder = StateGraph(TaskState)
    builder.add_node(
        "initialize_task",
        partial(initialize_task_traced, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "analyze_task",
        partial(
            analyze_task_traced,
            analyzer=analyzer,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node(
        "plan_task",
        partial(plan_task_traced, planner=planner, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "begin_step", partial(begin_step_traced, trace_recorder=trace_recorder)
    )
    builder.add_node(
        "decide_action",
        partial(decide_action, executor=executor, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "assess_policy",
        partial(assess_policy, executor=executor, trace_recorder=trace_recorder),
    )
    builder.add_node("policy_feedback", policy_feedback)
    builder.add_node(
        "prepare_approval",
        partial(prepare_approval_traced, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "human_approval",
        partial(human_approval_traced, trace_recorder=trace_recorder),
    )
    builder.add_node("rejection_feedback", rejection_feedback)
    builder.add_node(
        "execute_tool_action",
        partial(
            execute_tool_action,
            executor=executor,
            journal=journal,
            fault_injector=fault_injector,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node(
        "recovery_review",
        partial(recovery_review_traced, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "authorize_recovery_retry",
        partial(authorize_recovery_retry, journal=journal),
    )
    builder.add_node("build_step_outcome", build_step_outcome)
    builder.add_node(
        "verify_step",
        partial(
            verify_step_traced,
            verifier=verifier,
            max_step_attempts=max_step_attempts,
            max_replans=max_replans,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node("reset_step_attempt", reset_step_attempt)
    builder.add_node("advance_step", advance_step_action_level)
    builder.add_node(
        "verify_task",
        partial(
            verify_task_traced,
            verifier=verifier,
            max_replans=max_replans,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node(
        "replan_task",
        partial(
            replan_task,
            replanner=replanner,
            context_builder=context_builder,
            max_replans=max_replans,
            trace_recorder=trace_recorder,
        ),
    )

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task", _route_after_plan, {"execute_step": "begin_step", "end": END}
    )
    builder.add_edge("begin_step", "decide_action")
    builder.add_conditional_edges(
        "decide_action",
        _route_after_decision,
        {
            "assess_policy": "assess_policy",
            "build_step_outcome": "build_step_outcome",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "assess_policy",
        _route_after_policy,
        {
            "policy_feedback": "policy_feedback",
            "prepare_approval": "prepare_approval",
            "execute_tool_action": "execute_tool_action",
            "end": END,
        },
    )
    builder.add_edge("policy_feedback", "decide_action")
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_tool_action",
            "handle_rejection": "rejection_feedback",
            "end": END,
        },
    )
    builder.add_edge("rejection_feedback", "decide_action")
    builder.add_conditional_edges(
        "execute_tool_action",
        _route_after_tool_execution,
        {"decide_action": "decide_action", "recovery_review": "recovery_review", "end": END},
    )
    builder.add_conditional_edges(
        "recovery_review",
        _route_after_recovery,
        {"authorize_recovery_retry": "authorize_recovery_retry", "end": END},
    )
    builder.add_conditional_edges(
        "authorize_recovery_retry",
        _route_after_recovery_authorization,
        {"execute_tool_action": "execute_tool_action", "end": END},
    )
    builder.add_edge("build_step_outcome", "verify_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_action_verification,
        {
            "advance_step": "advance_step",
            "reset_step_attempt": "reset_step_attempt",
            "replan_task": "replan_task",
            "end": END,
        },
    )
    builder.add_edge("reset_step_attempt", "decide_action")
    builder.add_conditional_edges(
        "advance_step",
        _route_after_action_advance,
        {"decide_action": "decide_action", "verify_task": "verify_task", "end": END},
    )
    builder.add_conditional_edges(
        "verify_task",
        _route_after_task_verification,
        {"replan_task": "replan_task", "end": END},
    )
    builder.add_conditional_edges(
        "replan_task",
        _route_after_replan,
        {"begin_step": "begin_step", "end": END},
    )
    return builder.compile(checkpointer=checkpointer)


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    action_journal: ActionJournalLike | None = None,
    fault_injector: ExecutionFaultInjector | None = None,
    replanner: TaskReplannerLike | None = None,
    max_replans: int = 2,
    context_builder: ContextBuilder | None = None,
    trace_recorder: TraceRecorderLike | None = None,
) -> CompiledStateGraph:
    """构建 Graph；真实 TaskExecutor 使用 action-level durable execution。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    if max_replans < 0:
        raise ValueError("max_replans 必须大于等于 0")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = executor if executor is not None else TaskExecutor(ToolRegistry())
    active_verifier = verifier if verifier is not None else TaskVerifier()
    if isinstance(active_executor, TaskExecutor):
        return _build_action_level_graph(
            analyzer=active_analyzer,
            planner=active_planner,
            executor=active_executor,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
            checkpointer=checkpointer,
            journal=action_journal or InMemoryActionJournal(),
            fault_injector=fault_injector or NoOpExecutionFaultInjector(),
            replanner=replanner or TaskReplanner(),
            context_builder=context_builder or ContextBuilder(),
            max_replans=max_replans,
            trace_recorder=trace_recorder or NoOpTraceRecorder(),
        )
    # Phase 1–8 的离线 Fake 只实现 execute；保留兼容图，不用于生产 CLI。
    return _build_legacy_graph(
        analyzer=active_analyzer,
        planner=active_planner,
        executor=active_executor,
        verifier=active_verifier,
        max_step_attempts=max_step_attempts,
        checkpointer=checkpointer,
    )

```

### src/taskpilot/cli.py

```python
"""TaskPilot 的 async Host/CLI 入口。"""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from langgraph.types import Command

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.context import ContextBuilder
from taskpilot.executor import TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.mcp.provider import MCPToolProvider
from taskpilot.models import TaskStatus
from taskpilot.persistence import (
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry
from taskpilot.trace import SQLiteTraceRecorder, summarize_task_trace


def create_initial_state(user_input: str) -> TaskState:
    """根据用户输入创建结构化初始任务状态。"""

    return {
        "task_id": str(uuid4()),
        "user_input": user_input,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "pending_action": None,
        "policy_decisions": [],
        "approval_records": [],
        "current_action_index": 0,
        "pending_executor_action": None,
        "recovery_issue": None,
        "pending_replan": None,
        "plan_revision": 0,
        "replan_count": 0,
        "replan_history": [],
        "status": TaskStatus.CREATED,
        "error": None,
    }


async def async_main(argv: Sequence[str] | None = None) -> int:
    """在可选 MCP/Browser 生命周期内异步运行一次 TaskPilot Graph。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot graph.")
    parser.add_argument("task", nargs="?", help="Task description for a new task")
    parser.add_argument(
        "--resume",
        metavar="TASK_ID",
        help="Resume an existing durable LangGraph thread",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".taskpilot"),
        help=("Directory containing checkpoints.sqlite3, actions.sqlite3, "
              "and traces.sqlite3"),
    )
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="JSON file containing MCP server configurations",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Enable the Playwright Chromium Browser Tools",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chromium window (requires --browser)",
    )
    args = parser.parse_args(argv)

    if args.headed and not args.browser:
        parser.error("--headed requires --browser")
    if (args.task is None) == (args.resume is None):
        parser.error("provide exactly one of TASK or --resume TASK_ID")

    registry = ToolRegistry()
    initial_state = create_initial_state(args.task) if args.task is not None else None
    task_id = initial_state["task_id"] if initial_state is not None else args.resume
    assert task_id is not None
    persistence_config = PersistenceConfig.from_state_dir(args.state_dir)
    trace_summary = None
    # Host 持有所有外部资源；Graph 只接收已配置好的 Executor。
    async with AsyncExitStack() as stack:
        if args.mcp_config is not None:
            configs = load_mcp_server_configs(args.mcp_config)
            provider = await stack.enter_async_context(MCPToolProvider(configs))
            await provider.register_tools(registry)
        if args.browser:
            browser_provider = await stack.enter_async_context(
                BrowserToolProvider(BrowserConfig(headless=not args.headed))
            )
            browser_provider.register_tools(registry)

        checkpoint_provider = await stack.enter_async_context(
            SQLiteCheckpointProvider(persistence_config)
        )
        action_journal = await stack.enter_async_context(
            SQLiteActionJournal(persistence_config.action_journal_db_path)
        )
        trace_recorder = await stack.enter_async_context(
            SQLiteTraceRecorder(persistence_config.trace_db_path)
        )
        if checkpoint_provider.checkpointer is None:  # pragma: no cover - lifecycle 防御
            raise RuntimeError("SQLite checkpointer 未初始化")

        # 真实外部 Tool 默认经过 Policy；runtime provider 每次启动都重新构造。
        policy = DefaultRiskPolicy() if (args.browser or args.mcp_config) else None
        context_builder = ContextBuilder()
        executor = TaskExecutor(
            registry=registry,
            policy=policy,
            context_builder=context_builder,
        )
        graph = build_graph(
            executor=executor,
            checkpointer=checkpoint_provider.checkpointer,
            action_journal=action_journal,
            context_builder=context_builder,
            trace_recorder=trace_recorder,
        )
        graph_config = {"configurable": {"thread_id": task_id}}
        if initial_state is None:
            snapshot = await graph.aget_state(graph_config)
            if not snapshot.values:
                print(f"error=no durable checkpoint found for task_id={task_id}")
                return 1
            result = dict(snapshot.values)
            restored_status = result.get("status")
            if restored_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED.value}:
                print("task already completed")
            elif (
                restored_status in {TaskStatus.FAILED, TaskStatus.FAILED.value}
                and not snapshot.next
            ):
                print("task already failed; automatic restart is disabled")
            # 非 interrupt 的 crash 会留下 next node；None 表示从 durable position 续跑。
            if snapshot.next and not any(
                item for task in snapshot.tasks for item in task.interrupts
            ):
                result = await graph.ainvoke(None, config=graph_config)
        else:
            result = await graph.ainvoke(initial_state, config=graph_config)
        snapshot = await graph.aget_state(graph_config)
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        while interrupts:
            payload = interrupts[0].value
            print(f"interrupt_type={payload['type']}")
            print(f"approval_tool={payload['tool_name']}")
            print(f"approval_reason={payload['reason']}")
            print(
                "approval_arguments_preview="
                + json.dumps(
                    payload["arguments_preview"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            if payload["type"] == "tool_approval":
                print(f"approval_risk={payload['risk_level']}")
                answer = await asyncio.to_thread(input, "Approve? [y/N] ")
                resume = (
                    {"decision": "approve"}
                    if answer.strip().lower() == "y"
                    else {"decision": "reject", "reason": "Rejected from CLI"}
                )
            elif payload["type"] == "ambiguous_tool_recovery":
                answer = await asyncio.to_thread(
                    input,
                    "Action may already have run. Retry anyway? [y/N] ",
                )
                resume = (
                    {"choice": "retry", "reason": "Retry accepted from CLI"}
                    if answer.strip().lower() == "y"
                    else {"choice": "abort", "reason": "Aborted from CLI"}
                )
            else:
                print(f"error=unsupported interrupt type: {payload['type']}")
                return 1
            result = await graph.ainvoke(
                Command(resume=resume),
                config=graph_config,
            )
            snapshot = await graph.aget_state(graph_config)
            interrupts = [
                item
                for task in snapshot.tasks
                for item in task.interrupts
            ]
        trace_summary = await summarize_task_trace(trace_recorder, task_id)
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    task_spec = result.get("task_spec")
    if task_spec is not None:
        print(f"goal={task_spec.goal}")
        print(
            "constraints="
            + json.dumps(task_spec.constraints, ensure_ascii=False, sort_keys=True)
        )
        print(f"expected_output={task_spec.expected_output}")
        print(
            "completion_criteria="
            + json.dumps(task_spec.completion_criteria, ensure_ascii=False)
        )
    print("plan:")
    for step in result.get("plan", []):
        print(f"  step_id={step.id}")
        print(f"    description={step.description}")
        print(f"    depends_on={json.dumps(step.depends_on)}")
        print(
            "    success_criteria="
            + json.dumps(step.success_criteria, ensure_ascii=False)
        )
        print(f"    status={step.status.value}")
    print(f"current_step_id={result.get('current_step_id')}")
    print(f"plan_revision={result.get('plan_revision', 0)}")
    print(f"replan_count={result.get('replan_count', 0)}")
    if trace_summary is not None:
        print(
            "trace_summary="
            + trace_summary.model_dump_json()
        )
    step_outcome = result.get("step_outcome")
    if step_outcome is not None:
        print(f"claimed_complete={step_outcome.claimed_complete}")
        print(f"action_count={step_outcome.action_count}")
        print(f"execution_summary={step_outcome.summary}")
        print(
            "execution_evidence="
            + json.dumps(step_outcome.evidence, ensure_ascii=False)
        )
        print(
            "execution_output="
            + json.dumps(step_outcome.output, ensure_ascii=False)
        )
        if step_outcome.error:
            print(f"execution_error={step_outcome.error}")
    print(
        "step_results="
        + json.dumps(
            [item.model_dump(mode="json") for item in result.get("step_results", [])],
            ensure_ascii=False,
        )
    )
    verification = result.get("verification")
    if verification is not None:
        print(f"task_verified={verification.completed}")
        print(f"verification_reason={verification.reason}")
    if result.get("error"):
        print(f"error={result['error']}")
        return 1
    if step_outcome is not None and step_outcome.error:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 顶层创建一次事件循环；Graph node 内不会创建 event loop。"""

    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())

```

## 29.1 Full Relevant Tests

### tests/test_context.py

```python
"""Phase 10 deterministic Context Builder 离线测试。"""

import json

import pytest

from taskpilot.context import ContextBudgetError, ContextBuilder, ContextConfig
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    ReplanRequest,
    ReplanTrigger,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
)


def spec(goal: str = "Find verified candidates") -> TaskSpec:
    return TaskSpec(goal=goal, constraints={"count": 2}, completion_criteria=["done"])


def step() -> PlanStep:
    return PlanStep(
        id=3,
        description="Verify candidates",
        depends_on=[1],
        success_criteria=["Two candidates verified"],
    )


def call(number: int, result: object | None = None) -> ToolCallRecord:
    return ToolCallRecord(
        call_id=f"call-{number}",
        step_id=3,
        tool_name="lookup",
        status=ToolCallStatus.SUCCESS,
        result=result if result is not None else {"number": number},
    )


def test_context_preserves_core_fields() -> None:
    snapshot = ContextBuilder().build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[]
    )
    assert snapshot.content["task_goal"] == "Find verified candidates"
    assert snapshot.content["task_constraints"] == {"count": 2}
    assert snapshot.content["current_step"]["success_criteria"] == [
        "Two candidates verified"
    ]


def test_context_only_includes_latest_tool_calls() -> None:
    builder = ContextBuilder(ContextConfig(max_recent_tool_calls=2))
    snapshot = builder.build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[call(i) for i in range(5)]
    )
    assert [item["call_id"] for item in snapshot.content["current_step_observations"]] == [
        "call-3",
        "call-4",
    ]
    assert snapshot.included_tool_calls == 2
    assert snapshot.omitted_tool_calls == 3


def test_large_observation_has_explicit_truncation_marker() -> None:
    builder = ContextBuilder(ContextConfig(max_single_observation_chars=20))
    snapshot = builder.build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[call(1, "x" * 200)]
    )
    observation = snapshot.content["current_step_observations"][0]["observation"]
    assert observation["truncated"] is True
    assert observation["original_length"] > 20
    assert "tool_observation:call-1" in snapshot.truncated_sections
    print(
        "CONTEXT_BUDGET_SMOKE="
        + json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )


def test_verified_dependency_result_precedes_raw_history() -> None:
    builder = ContextBuilder(
        ContextConfig(max_total_chars=900, max_single_observation_chars=500)
    )
    dependency = StepResult(step_id=1, summary="verified", output={"facts": ["A", "B"]})
    snapshot = builder.build_executor_context(
        task_spec=spec(),
        step=step(),
        task_results={},
        tool_calls=[call(1, "x" * 500), call(2, "y" * 500)],
        step_results=[dependency],
    )
    assert snapshot.content["dependency_results"][0]["summary"] == "verified"
    assert snapshot.omitted_tool_calls >= 1


def test_feedback_is_kept_for_matching_step() -> None:
    feedback = StepVerificationResult(
        step_id=3,
        verified=False,
        checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="missing")],
        feedback="Need a second source",
    )
    snapshot = ContextBuilder().build_executor_context(
        task_spec=spec(),
        step=step(),
        task_results={},
        tool_calls=[],
        verification_feedback=feedback,
    )
    assert snapshot.content["verification_feedback"]["feedback"] == "Need a second source"


def test_feedback_from_other_step_is_not_included() -> None:
    feedback = StepVerificationResult(step_id=99, verified=False, feedback="irrelevant")
    snapshot = ContextBuilder().build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[], verification_feedback=feedback
    )
    assert snapshot.content["verification_feedback"] is None


def test_core_budget_overflow_is_explicit() -> None:
    builder = ContextBuilder(ContextConfig(max_total_chars=50))
    with pytest.raises(ContextBudgetError, match="核心上下文"):
        builder.build_executor_context(
            task_spec=spec("g" * 200), step=step(), task_results={}, tool_calls=[]
        )


def test_context_does_not_mutate_durable_history() -> None:
    records = [call(1, {"items": [1, 2]})]
    before = records[0].model_dump_json()
    ContextBuilder(ContextConfig(max_single_observation_chars=5)).build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=records
    )
    assert records[0].model_dump_json() == before


def test_replan_context_contains_plan_and_verified_facts() -> None:
    request = ReplanRequest(
        trigger=ReplanTrigger.STEP_ATTEMPTS_EXHAUSTED,
        failed_step_id=3,
        reason="path blocked",
        verification_feedback="missing evidence",
    )
    snapshot = ContextBuilder().build_replan_context(
        task_spec=spec(),
        plan=[PlanStep(id=1, description="done", success_criteria=["done"])],
        step_results=[StepResult(step_id=1, summary="fact")],
        tool_calls=[call(1)],
        request=request,
        plan_revision=0,
        replan_count=0,
    )
    assert snapshot.content["replan_trigger"] == "step_attempts_exhausted"
    assert snapshot.content["verified_step_results"][0]["summary"] == "fact"
    assert snapshot.content["historical_max_step_id"] == 1


def test_replan_context_redacts_tool_argument_secrets() -> None:
    record = call(1)
    record.arguments = {"api_key": "secret-value", "query": "safe"}
    snapshot = ContextBuilder().build_replan_context(
        task_spec=spec(),
        plan=[step()],
        step_results=[],
        tool_calls=[record],
        request=ReplanRequest(
            trigger=ReplanTrigger.STEP_ATTEMPTS_EXHAUSTED,
            failed_step_id=3,
            reason="failed",
        ),
        plan_revision=0,
        replan_count=0,
    )
    dumped = json.dumps(snapshot.content, ensure_ascii=False)
    assert "secret-value" not in dumped
    assert "[REDACTED]" in dumped


@pytest.mark.parametrize(
    "field",
    [
        "max_total_chars",
        "max_single_observation_chars",
        "max_dependency_result_chars",
        "max_task_results_chars",
        "max_feedback_chars",
    ],
)
def test_positive_context_limits_are_validated(field: str) -> None:
    with pytest.raises(ValueError):
        ContextConfig(**{field: 0})


def test_zero_recent_tool_calls_is_supported() -> None:
    snapshot = ContextBuilder(ContextConfig(max_recent_tool_calls=0)).build_executor_context(
        task_spec=spec(), step=step(), task_results={}, tool_calls=[call(1)]
    )
    assert snapshot.included_tool_calls == 0
    assert snapshot.omitted_tool_calls == 1


def test_context_budget_smoke_with_twenty_historical_calls() -> None:
    dependency = StepResult(step_id=1, summary="verified dependency", output={"fact": "kept"})
    snapshot = ContextBuilder(
        ContextConfig(max_recent_tool_calls=5, max_single_observation_chars=40)
    ).build_executor_context(
        task_spec=spec(),
        step=step(),
        task_results={},
        tool_calls=[call(index, "x" * 100) for index in range(20)],
        step_results=[dependency],
    )
    assert snapshot.included_tool_calls == 5
    assert snapshot.omitted_tool_calls == 15
    print(
        "CONTEXT_BUDGET_SMOKE="
        + json.dumps(
            {
                "historical_tool_calls": 20,
                "included_raw_tool_calls": snapshot.included_tool_calls,
                "goal_preserved": snapshot.content["task_goal"] == spec().goal,
                "dependency_result_preserved": bool(snapshot.content["dependency_results"]),
                "truncated_sections": snapshot.truncated_sections,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

```

### tests/test_replanner.py

```python
"""Phase 10 Replanner prompt 与 deterministic validation 测试。"""

import pytest

from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    ReplanProposal,
    ReplanRecord,
    ReplanRequest,
    ReplanTrigger,
)
from taskpilot.replanner import REPLANNER_SYSTEM_PROMPT, validate_replan_proposal


def historical(status: PlanStepStatus = PlanStepStatus.COMPLETED) -> list[PlanStep]:
    return [
        PlanStep(
            id=1,
            description="old",
            status=status,
            success_criteria=["old done"],
        )
    ]


def proposal(*steps: PlanStep) -> ReplanProposal:
    return ReplanProposal(reason="new path", steps=list(steps))


def new_step(step_id: int = 2, **updates: object) -> PlanStep:
    values = {
        "id": step_id,
        "description": "new semantic goal",
        "success_criteria": ["new done"],
    }
    values.update(updates)
    return PlanStep(**values)


def test_replanner_prompt_enforces_plan_only_boundary() -> None:
    for phrase in ["WHAT", "不得选择或调用 Tool", "Constraints 不允许修改", "不得虚构"]:
        assert phrase in REPLANNER_SYSTEM_PROMPT


def test_valid_replan_accepts_completed_historical_dependency() -> None:
    validate_replan_proposal(proposal(new_step(depends_on=[1])), historical())


def test_valid_replan_accepts_earlier_new_dependency() -> None:
    validate_replan_proposal(
        proposal(new_step(2), new_step(3, depends_on=[2])), historical()
    )


def test_empty_replan_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        validate_replan_proposal(proposal(), historical())


def test_duplicate_new_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="唯一"):
        validate_replan_proposal(proposal(new_step(), new_step()), historical())


@pytest.mark.parametrize("step_id", [0, 1])
def test_reused_or_old_step_ids_are_rejected(step_id: int) -> None:
    with pytest.raises(ValueError, match="historical_max_step_id"):
        validate_replan_proposal(proposal(new_step(step_id)), historical())


def test_empty_success_criteria_is_rejected() -> None:
    with pytest.raises(ValueError, match="success_criteria"):
        validate_replan_proposal(proposal(new_step(success_criteria=[])), historical())


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="依赖自身"):
        validate_replan_proposal(proposal(new_step(depends_on=[2])), historical())


def test_nonexistent_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="不存在"):
        validate_replan_proposal(proposal(new_step(depends_on=[99])), historical())


@pytest.mark.parametrize(
    "status",
    [
        PlanStepStatus.FAILED,
        PlanStepStatus.SKIPPED,
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    ],
)
def test_noncompleted_historical_dependency_is_rejected(status: PlanStepStatus) -> None:
    with pytest.raises(ValueError, match=status.value):
        validate_replan_proposal(proposal(new_step(depends_on=[1])), historical(status))


def test_later_new_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="必须更早出现"):
        validate_replan_proposal(
            proposal(new_step(2, depends_on=[3]), new_step(3)), historical()
        )


def test_nonpending_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="status"):
        validate_replan_proposal(
            proposal(new_step(status=PlanStepStatus.RUNNING)), historical()
        )


def test_nonzero_retry_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        validate_replan_proposal(proposal(new_step(retry_count=1)), historical())


def test_replan_model_mutable_defaults_are_isolated() -> None:
    first = ReplanRequest(trigger=ReplanTrigger.TASK_VERIFICATION_FAILED, reason="one")
    second = ReplanRequest(trigger=ReplanTrigger.TASK_VERIFICATION_FAILED, reason="two")
    first.missing_requirements.append("x")
    assert second.missing_requirements == []
    record_a = ReplanRecord(revision=1, trigger=first.trigger, reason="a")
    record_b = ReplanRecord(revision=1, trigger=first.trigger, reason="b")
    record_a.new_step_ids.append(4)
    assert record_b.new_step_ids == []

```

### tests/test_trace.py

```python
"""Phase 10 structured Trace recorder 离线测试。"""

from pathlib import Path

import pytest

from taskpilot.trace import (
    InMemoryTraceRecorder,
    NoOpTraceRecorder,
    SQLiteTraceRecorder,
    TraceEventType,
    make_trace_event,
    summarize_task_trace,
)


@pytest.mark.asyncio
async def test_in_memory_trace_preserves_event_order() -> None:
    recorder = InMemoryTraceRecorder()
    await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED))
    await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_COMPLETED))
    assert [event.event_type for event in await recorder.list_events("t")] == [
        TraceEventType.TASK_STARTED,
        TraceEventType.TASK_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_in_memory_trace_filters_tasks() -> None:
    recorder = InMemoryTraceRecorder()
    await recorder.record(make_trace_event(task_id="a", event_type=TraceEventType.TASK_STARTED))
    await recorder.record(make_trace_event(task_id="b", event_type=TraceEventType.TASK_STARTED))
    assert len(await recorder.list_events("a")) == 1


@pytest.mark.asyncio
async def test_count_by_type_uses_enum_keys() -> None:
    recorder = InMemoryTraceRecorder()
    for _ in range(2):
        await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TOOL_STARTED))
    assert (await recorder.count_by_type("t"))[TraceEventType.TOOL_STARTED] == 2


def test_make_trace_event_redacts_nested_secrets() -> None:
    event = make_trace_event(
        task_id="t",
        event_type=TraceEventType.TOOL_STARTED,
        payload={"arguments": {"authorization": "Bearer secret", "nested": {"token": "x"}}},
    )
    assert event.payload["arguments"]["authorization"] == "[REDACTED]"
    assert event.payload["arguments"]["nested"]["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_noop_trace_recorder_is_empty() -> None:
    recorder = NoOpTraceRecorder()
    await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED))
    assert await recorder.list_events("t") == []


@pytest.mark.asyncio
async def test_trace_summary_counts_real_events_and_latency() -> None:
    recorder = InMemoryTraceRecorder()
    types = [
        TraceEventType.TOOL_STARTED,
        TraceEventType.TOOL_FAILED,
        TraceEventType.STEP_REJECTED,
        TraceEventType.PLAN_REPLANNED,
        TraceEventType.APPROVAL_REQUESTED,
        TraceEventType.RECOVERY_REQUIRED,
    ]
    for event_type in types:
        await recorder.record(
            make_trace_event(task_id="t", event_type=event_type, latency_ms=2.5)
        )
    summary = await summarize_task_trace(recorder, "t")
    assert summary.event_count == 6
    assert summary.tool_call_count == 1
    assert summary.tool_failure_count == 1
    assert summary.step_rejection_count == 1
    assert summary.replan_count == 1
    assert summary.approval_request_count == 1
    assert summary.recovery_required_count == 1
    assert summary.observed_latency_ms == 15.0


@pytest.mark.asyncio
async def test_sqlite_trace_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "trace.sqlite3"
    async with SQLiteTraceRecorder(path) as recorder:
        event = make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED)
        await recorder.record(event)
        loaded = await recorder.list_events("t")
        assert loaded == [event]
        assert (await recorder.count_by_type("t"))[TraceEventType.TASK_STARTED] == 1


@pytest.mark.asyncio
async def test_sqlite_trace_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "trace.sqlite3"
    async with SQLiteTraceRecorder(path) as recorder:
        await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED))
    async with SQLiteTraceRecorder(path) as recorder:
        assert len(await recorder.list_events("t")) == 1


@pytest.mark.asyncio
async def test_sqlite_trace_requires_context_lifecycle(tmp_path: Path) -> None:
    recorder = SQLiteTraceRecorder(tmp_path / "trace.sqlite3")
    with pytest.raises(RuntimeError, match="async context"):
        await recorder.list_events("t")


def test_trace_event_is_json_serializable() -> None:
    event = make_trace_event(
        task_id="t", event_type=TraceEventType.CONTEXT_BUILT, payload={"chars": 12}
    )
    assert '"event_type":"context_built"' in event.model_dump_json()

```

### tests/test_phase10_graph.py

```python
"""Phase 10 Dynamic Replanning、Context 与 Trace 的 action-level smoke tests。"""

from dataclasses import dataclass, field
import json
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from taskpilot.cli import create_initial_state
from taskpilot.context import ContextBuilder, ContextConfig
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    PlanStepStatus,
    ReplanProposal,
    ReplanTrigger,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.persistence import InMemoryActionJournal
from taskpilot.trace import (
    InMemoryTraceRecorder,
    SQLiteTraceRecorder,
    TraceEventType,
    summarize_task_trace,
)
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.messages: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.messages.append(list(messages))
        return self.responses.pop(0)


def finish(call_id: str, step: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": FINISH_STEP_NAME,
                "args": {
                    "summary": f"step {step} claimed",
                    "evidence": [f"evidence-{step}"],
                    "output": {"step": step},
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def tool_action(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "unused",
                "args": {},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


@dataclass
class DummyTool:
    name: str = "unused"
    description: str = "Keeps registry non-empty for finish-only smoke tests"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    metadata: dict[str, Any] = field(
        default_factory=lambda: {"source": "local", "risk": "low", "replay_safe": True}
    )
    calls: int = 0

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, bool]:
        self.calls += 1
        return {"unused": True}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, constraints={"offline": True}, completion_criteria=["done"])


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="initial path",
                    success_criteria=["fact found"],
                    revision=99,
                )
            ]
        )


class ThreeStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(id=1, description="discover candidates", success_criteria=["candidates found"]),
                PlanStep(
                    id=2,
                    description="use broken strategy",
                    depends_on=[1],
                    success_criteria=["strategy succeeds"],
                ),
                PlanStep(
                    id=3,
                    description="old remaining work",
                    depends_on=[2],
                    success_criteria=["old path done"],
                ),
            ]
        )


class FakeReplanner:
    def __init__(self, proposals: Sequence[ReplanProposal]) -> None:
        self.proposals = list(proposals)
        self.contexts = []

    def replan(self, context):
        self.contexts.append(context)
        return self.proposals.pop(0)


class StepFailureVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        accepted = step.id != 2
        return StepVerificationResult(
            step_id=step.id,
            verified=accepted,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=accepted,
                    reason="accepted" if accepted else "initial path blocked",
                )
            ],
            feedback=None if accepted else "try a different semantic path",
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="replacement completed")


class TaskRejectionVerifier:
    def __init__(self) -> None:
        self.task_calls = 0

    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[CriterionCheck(criterion_index=0, satisfied=True, reason="accepted")],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        self.task_calls += 1
        if self.task_calls == 1:
            return VerificationResult(
                completed=False,
                reason="final format missing",
                missing_requirements=["produce final format"],
                next_action="replan remaining work",
            )
        return VerificationResult(completed=True, reason="all criteria satisfied")


def executor(responses: Sequence[AIMessage], model_out: list[FakeFunctionCallingModel]) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(DummyTool())
    model = FakeFunctionCallingModel(responses)
    model_out.append(model)
    return TaskExecutor(registry=registry, model=cast(BaseChatModel, model))


def initial(task_id: str):
    state = create_initial_state("complete an offline task")
    state["task_id"] = task_id
    return state


@pytest.mark.asyncio
async def test_step_failure_replans_with_fresh_id_and_preserves_history() -> None:
    models: list[FakeFunctionCallingModel] = []
    replanner = FakeReplanner(
        [
            ReplanProposal(
                reason="use remaining alternative",
                steps=[
                    PlanStep(
                        id=4,
                        description="alternative path",
                        depends_on=[1],
                        success_criteria=["fact recovered"],
                    ),
                    PlanStep(
                        id=5,
                        description="finish remaining work",
                        depends_on=[4],
                        success_criteria=["remaining work complete"],
                    ),
                ],
            )
        ]
    )
    trace = InMemoryTraceRecorder()
    journal = InMemoryActionJournal()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=ThreeStepPlanner(),
        executor=executor(
            [
                tool_action("tool-step-1"),
                finish("finish-1", 1),
                tool_action("tool-step-2"),
                finish("finish-2", 2),
                tool_action("tool-step-4"),
                finish("finish-4", 4),
                finish("finish-5", 5),
            ],
            models,
        ),
        verifier=StepFailureVerifier(),
        replanner=replanner,
        max_step_attempts=1,
        max_replans=2,
        trace_recorder=trace,
        action_journal=journal,
    )
    result = await graph.ainvoke(
        initial("step-replan"), config={"recursion_limit": 100}
    )

    assert result["status"] is TaskStatus.COMPLETED
    assert [(item.id, item.revision, item.status) for item in result["plan"]] == [
        (1, 0, PlanStepStatus.COMPLETED),
        (2, 0, PlanStepStatus.FAILED),
        (3, 0, PlanStepStatus.SKIPPED),
        (4, 1, PlanStepStatus.COMPLETED),
        (5, 1, PlanStepStatus.COMPLETED),
    ]
    assert [item.step_id for item in result["step_results"]] == [1, 4, 5]
    assert result["plan_revision"] == 1
    assert result["replan_count"] == 1
    assert result["replan_history"][0].replaced_step_ids == [2, 3]
    assert result["replan_history"][0].new_step_ids == [4, 5]
    assert replanner.contexts[0].content["historical_max_step_id"] == 3
    assert set(journal.records) == {
        "step-replan:1:0:1",
        "step-replan:2:0:1",
        "step-replan:4:0:1",
    }
    replan_event = next(
        event
        for event in await trace.list_events("step-replan")
        if event.event_type is TraceEventType.PLAN_REPLANNED
    )
    assert replan_event.plan_revision == 1
    assert replan_event.payload["new_revision"] == 1
    print(
        "REPLAN_TRACE_REVISION_SMOKE="
        + json.dumps(
            {
                "payload_revision": replan_event.payload["new_revision"],
                "trace_event_revision": replan_event.plan_revision,
            },
            sort_keys=True,
        )
    )
    print(
        "REPLAN_STEP_FAILURE_SMOKE="
        + json.dumps(
            {
                "initial_revision": 0,
                "failed_step_id": 2,
                "replan_trigger": result["replan_history"][0].trigger.value,
                "replan_count": result["replan_count"],
                "new_revision": result["plan_revision"],
                "new_step_ids": result["replan_history"][0].new_step_ids,
                "completed_old_step_rerun": False,
                "task_status": result["status"].value,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_task_verification_replans_and_reuses_completed_result() -> None:
    models: list[FakeFunctionCallingModel] = []
    verifier = TaskRejectionVerifier()
    replanner = FakeReplanner(
        [
            ReplanProposal(
                reason="add missing final format",
                steps=[
                    PlanStep(
                        id=2,
                        description="produce final format",
                        depends_on=[1],
                        success_criteria=["format produced"],
                    )
                ],
            )
        ]
    )
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor([finish("finish-a", 1), finish("finish-b", 2)], models),
        verifier=verifier,
        replanner=replanner,
        max_replans=2,
    )
    result = await graph.ainvoke(initial("task-replan"))

    assert result["status"] is TaskStatus.COMPLETED
    assert [item.step_id for item in result["step_results"]] == [1, 2]
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["plan"][0].revision == 0
    assert result["plan"][1].depends_on == [1]
    assert replanner.contexts[0].content["verified_step_results"][0]["step_id"] == 1
    assert replanner.contexts[0].content["failure_feedback"]["missing_requirements"] == [
        "produce final format"
    ]
    print(
        "TASK_VERIFICATION_REPLAN_SMOKE="
        + json.dumps(
            {
                "first_task_verification": False,
                "missing_requirements": ["produce final format"],
                "replan_count": result["replan_count"],
                "second_task_verification": result["verification"].completed,
                "task_status": result["status"].value,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_max_replans_zero_preserves_fail_fast() -> None:
    models: list[FakeFunctionCallingModel] = []
    replanner = FakeReplanner([])
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("finish", 1)], models), verifier=type(
            "AlwaysReject",
            (),
            {
                "verify_step": lambda self, **kwargs: StepVerificationResult(
                    step_id=kwargs["step"].id,
                    verified=False,
                    checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no")],
                    feedback="blocked",
                ),
                "verify_task": lambda self, **kwargs: VerificationResult(completed=False, reason="unused"),
            },
        )(),
        replanner=replanner, max_step_attempts=1, max_replans=0,
    )
    result = await graph.ainvoke(initial("no-replan"))
    assert result["status"] is TaskStatus.FAILED
    assert "max_step_attempts=1" in result["error"]
    assert replanner.contexts == []


@pytest.mark.asyncio
async def test_replan_budget_is_bounded() -> None:
    models: list[FakeFunctionCallingModel] = []
    replanner = FakeReplanner(
        [ReplanProposal(reason="once", steps=[PlanStep(id=2, description="second", success_criteria=["done"])])]
    )
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("one", 1), finish("two", 2)], models),
        verifier=StepFailureVerifier(), replanner=replanner,
        max_step_attempts=1, max_replans=1,
    )
    # 此 Verifier 会接受 id=2；换成始终拒绝以触发第二次预算检查。
    graph_verifier = type(
        "AlwaysReject",
        (),
        {
            "verify_step": lambda self, **kwargs: StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no")],
                feedback="still blocked",
            ),
            "verify_task": lambda self, **kwargs: VerificationResult(completed=False, reason="unused"),
        },
    )()
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("one-b", 1), finish("two-b", 2)], models),
        verifier=graph_verifier, replanner=replanner,
        max_step_attempts=1, max_replans=1,
    )
    result = await graph.ainvoke(initial("bounded-replan"))
    assert result["status"] is TaskStatus.FAILED
    assert result["replan_count"] == 1
    assert "replan budget exhausted" in result["error"]


@pytest.mark.asyncio
async def test_trace_smoke_records_context_decision_and_final_state() -> None:
    models: list[FakeFunctionCallingModel] = []
    trace = InMemoryTraceRecorder()
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("finish", 1)], models), verifier=TaskRejectionVerifier(),
        max_replans=0, trace_recorder=trace,
    )
    # TaskRejectionVerifier 会在 Task 层拒绝；这里改用其第二次调用语义。
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    graph = build_graph(
        analyzer=FakeAnalyzer(), planner=FakePlanner(),
        executor=executor([finish("finish-ok", 1)], models), verifier=verifier,
        max_replans=0, trace_recorder=trace,
    )
    result = await graph.ainvoke(initial("trace-smoke"))
    event_types = [event.event_type for event in await trace.list_events("trace-smoke")]
    assert result["status"] is TaskStatus.COMPLETED
    assert {
        TraceEventType.TASK_STARTED,
        TraceEventType.TASK_ANALYZED,
        TraceEventType.PLAN_CREATED,
        TraceEventType.STEP_STARTED,
        TraceEventType.CONTEXT_BUILT,
        TraceEventType.LLM_ACTION_DECIDED,
        TraceEventType.STEP_VERIFIED,
        TraceEventType.TASK_VERIFIED,
        TraceEventType.TASK_COMPLETED,
    } <= set(event_types)
    events = await trace.list_events("trace-smoke")
    print(
        "TRACE_SMOKE="
        + json.dumps(
            {
                "event_types": [event.event_type.value for event in events],
                "latencies_ms": [
                    event.latency_ms for event in events if event.latency_ms is not None
                ],
                "event_count": len(events),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_tool_trace_records_start_and_terminal_result() -> None:
    models: list[FakeFunctionCallingModel] = []
    trace = InMemoryTraceRecorder()
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor([tool_action("tool-1"), finish("finish-2", 1)], models),
        verifier=verifier,
        max_replans=0,
        trace_recorder=trace,
    )
    result = await graph.ainvoke(initial("tool-trace"))
    events = await trace.list_events("tool-trace")
    types = [event.event_type for event in events]
    assert result["status"] is TaskStatus.COMPLETED
    assert TraceEventType.TOOL_STARTED in types
    assert TraceEventType.TOOL_SUCCEEDED in types
    terminal = next(event for event in events if event.event_type is TraceEventType.TOOL_SUCCEEDED)
    assert terminal.latency_ms is not None
    assert terminal.latency_ms >= 0


@pytest.mark.asyncio
async def test_sqlite_trace_graph_smoke_reopens_and_summarizes(tmp_path) -> None:
    models: list[FakeFunctionCallingModel] = []
    path = tmp_path / "traces.sqlite3"
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    async with SQLiteTraceRecorder(path) as recorder:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=executor([tool_action("sqlite-tool"), finish("sqlite-finish", 1)], models),
            verifier=verifier, max_replans=0, trace_recorder=recorder,
        )
        result = await graph.ainvoke(initial("sqlite-trace-smoke"))
    async with SQLiteTraceRecorder(path) as reopened:
        summary = await summarize_task_trace(reopened, "sqlite-trace-smoke")
        events = await reopened.list_events("sqlite-trace-smoke")
    assert result["status"] is TaskStatus.COMPLETED
    assert summary.tool_call_count == 1
    assert any(event.event_type is TraceEventType.TOOL_SUCCEEDED for event in events)
    print(
        "TRACE_SMOKE="
        + json.dumps(
            {
                "task_id": "sqlite-trace-smoke",
                "tool_calls": summary.tool_call_count,
                "tool_failures": summary.tool_failure_count,
                "step_rejections": summary.step_rejection_count,
                "replans": summary.replan_count,
                "approval_requests": summary.approval_request_count,
                "recovery_required": summary.recovery_required_count,
                "trace_reopened": True,
                "event_count": summary.event_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def test_negative_max_replans_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_replans"):
        build_graph(max_replans=-1)


@pytest.mark.asyncio
async def test_context_overflow_is_graph_failure_without_model_or_tool_call() -> None:
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)
    model = FakeFunctionCallingModel([finish("must-not-run", 1)])
    active_executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        context_builder=ContextBuilder(ContextConfig(max_total_chars=50)),
    )
    verifier = TaskRejectionVerifier()
    verifier.task_calls = 1
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=active_executor,
        verifier=verifier,
        max_replans=0,
    )
    result = await graph.ainvoke(initial("context-overflow"))
    assert result["status"] is TaskStatus.FAILED
    assert "context budget" in result["error"].lower()
    assert model.messages == []
    assert tool.calls == 0
    print(
        "CONTEXT_OVERFLOW_GRAPH_SMOKE="
        + json.dumps(
            {
                "model_invoked": False,
                "task_status": result["status"].value,
                "graph_exception": False,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_successful_replan_trace_revision_increments_twice() -> None:
    models: list[FakeFunctionCallingModel] = []
    trace = InMemoryTraceRecorder()
    replanner = FakeReplanner(
        [
            ReplanProposal(
                reason="revision one",
                steps=[PlanStep(id=2, description="second", success_criteria=["done"])],
            ),
            ReplanProposal(
                reason="revision two",
                steps=[PlanStep(id=3, description="third", success_criteria=["done"])],
            ),
        ]
    )
    always_reject = type(
        "AlwaysRejectForTwoReplans",
        (),
        {
            "verify_step": lambda self, **kwargs: StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(criterion_index=0, satisfied=False, reason="no")],
                feedback="try another revision",
            ),
            "verify_task": lambda self, **kwargs: VerificationResult(
                completed=False, reason="unused"
            ),
        },
    )()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor(
            [finish("revision-0", 1), finish("revision-1", 2), finish("revision-2", 3)],
            models,
        ),
        verifier=always_reject,
        replanner=replanner,
        max_step_attempts=1,
        max_replans=2,
        trace_recorder=trace,
    )
    result = await graph.ainvoke(initial("two-revisions"), config={"recursion_limit": 100})
    events = [
        event
        for event in await trace.list_events("two-revisions")
        if event.event_type is TraceEventType.PLAN_REPLANNED
        and event.status == "success"
    ]
    assert result["status"] is TaskStatus.FAILED
    assert result["replan_count"] == 2
    assert [event.plan_revision for event in events] == [1, 2]
    assert [event.payload["new_revision"] for event in events] == [1, 2]

```

### tests/test_phase10_trace_boundaries.py

```python
"""Phase 10 对 Phase 8/9 approval、recovery、journal 边界的 Trace 测试。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from taskpilot.cli import create_initial_state
from taskpilot.executor import TaskExecutor
from taskpilot.graph import (
    canonical_arguments_hash,
    execute_tool_action,
    human_approval_traced,
    prepare_approval_traced,
    recovery_review_traced,
)
from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    PlanStepStatus,
    TaskSpec,
    TaskStatus,
)
from taskpilot.persistence import (
    ActionJournalRecord,
    InMemoryActionJournal,
    JournalStatus,
    RecoveryIssue,
)
from taskpilot.persistence.models import NoOpExecutionFaultInjector
from taskpilot.policy import (
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
)
from taskpilot.trace import InMemoryTraceRecorder, TraceEventType
from taskpilot.tools import ToolRegistry


@dataclass
class BoundaryTool:
    replay_safe: bool
    fail: bool = False
    calls: int = 0
    name: str = "boundary"
    description: str = "Offline trace boundary tool"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }
    )

    @property
    def metadata(self) -> dict[str, Any]:
        return {"source": "local", "risk": "low", "replay_safe": self.replay_safe}

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, int]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("controlled failure")
        return {"value": arguments["value"]}


def boundary_state(*, replay_safe: bool = True):
    state = create_initial_state("trace boundary")
    state.update(
        {
            "task_id": "boundary-task",
            "task_spec": TaskSpec(goal="trace boundary"),
            "plan": [
                PlanStep(
                    id=1,
                    description="boundary",
                    status=PlanStepStatus.RUNNING,
                    success_criteria=["observed"],
                )
            ],
            "current_step_id": 1,
            "status": TaskStatus.RUNNING,
            "pending_executor_action": ExecutorAction(
                step_id=1,
                action_index=1,
                call_id="call-1",
                action_type=ExecutorActionType.TOOL,
                tool_name="boundary",
                arguments={"value": 7},
            ),
        }
    )
    return state


def make_executor(tool: BoundaryTool) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return TaskExecutor(registry)


def terminal_record(status: JournalStatus, replay_safe: bool = True) -> ActionJournalRecord:
    now = datetime.now(UTC)
    return ActionJournalRecord(
        action_key="boundary-task:1:0:1",
        task_id="boundary-task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="boundary",
        arguments_hash=canonical_arguments_hash({"value": 7}),
        status=status,
        replay_safe=replay_safe,
        result={"value": 7} if status is JournalStatus.SUCCEEDED else None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_terminal_journal_materialization_is_traced_without_reinvoke() -> None:
    state = boundary_state()
    tool = BoundaryTool(replay_safe=True)
    journal = InMemoryActionJournal()
    journal.records["boundary-task:1:0:1"] = terminal_record(JournalStatus.SUCCEEDED)
    trace = InMemoryTraceRecorder()
    update = await execute_tool_action(
        state,
        executor=make_executor(tool),
        journal=journal,
        fault_injector=NoOpExecutionFaultInjector(),
        trace_recorder=trace,
    )
    types = [event.event_type for event in await trace.list_events("boundary-task")]
    assert tool.calls == 0
    assert update["tool_calls"][0].result == {"value": 7}
    assert types == [TraceEventType.TOOL_MATERIALIZED_FROM_JOURNAL]


@pytest.mark.asyncio
async def test_unsafe_started_requires_recovery_not_replanning() -> None:
    state = boundary_state(replay_safe=False)
    tool = BoundaryTool(replay_safe=False)
    journal = InMemoryActionJournal()
    journal.records["boundary-task:1:0:1"] = terminal_record(
        JournalStatus.STARTED, replay_safe=False
    )
    trace = InMemoryTraceRecorder()
    update = await execute_tool_action(
        state,
        executor=make_executor(tool),
        journal=journal,
        fault_injector=NoOpExecutionFaultInjector(),
        trace_recorder=trace,
    )
    assert update["status"] is TaskStatus.INTERRUPTED
    assert "pending_replan" not in update
    assert tool.calls == 0
    assert (await trace.list_events("boundary-task"))[0].event_type is TraceEventType.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_recovery_abort_is_traced_and_does_not_replan(monkeypatch) -> None:
    state = boundary_state(replay_safe=False)
    state["recovery_issue"] = RecoveryIssue(
        action_key="boundary-task:1:0:1",
        task_id="boundary-task",
        step_id=1,
        action_index=1,
        tool_name="boundary",
        call_id="call-1",
        reason="ambiguous",
        replay_safe=False,
    )
    monkeypatch.setattr(
        "taskpilot.graph.interrupt",
        lambda payload: {"choice": "abort", "reason": "operator abort"},
    )
    trace = InMemoryTraceRecorder()
    update = await recovery_review_traced(state, trace_recorder=trace)
    assert update["status"] is TaskStatus.FAILED
    assert "pending_replan" not in update
    assert (await trace.list_events("boundary-task"))[0].event_type is TraceEventType.RECOVERY_RESOLVED


@pytest.mark.asyncio
async def test_human_reject_is_traced_without_immediate_replan(monkeypatch) -> None:
    state = boundary_state()
    decision = PolicyDecision(
        call_id="call-1",
        step_id=1,
        tool_name="boundary",
        risk_level=RiskLevel.HIGH,
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        reason="write capable",
        tool_source="local",
    )
    state["pending_action"] = PendingToolAction(
        call_id="call-1",
        step_id=1,
        tool_name="boundary",
        arguments={"value": 7},
        policy_decision=decision,
    )
    trace = InMemoryTraceRecorder()
    prepared = await prepare_approval_traced(state, trace_recorder=trace)
    assert prepared["status"] is TaskStatus.WAITING_APPROVAL
    monkeypatch.setattr(
        "taskpilot.graph.interrupt",
        lambda payload: {"decision": "reject", "reason": "choose another action"},
    )
    update = await human_approval_traced(state, trace_recorder=trace)
    assert update["approval_records"][-1].decision.value == "reject"
    assert "pending_replan" not in update
    assert [event.event_type for event in await trace.list_events("boundary-task")] == [
        TraceEventType.APPROVAL_REQUESTED,
        TraceEventType.APPROVAL_RESOLVED,
    ]


@pytest.mark.asyncio
async def test_tool_failure_has_terminal_trace() -> None:
    state = boundary_state()
    tool = BoundaryTool(replay_safe=True, fail=True)
    trace = InMemoryTraceRecorder()
    update = await execute_tool_action(
        state,
        executor=make_executor(tool),
        journal=InMemoryActionJournal(),
        fault_injector=NoOpExecutionFaultInjector(),
        trace_recorder=trace,
    )
    types = [event.event_type for event in await trace.list_events("boundary-task")]
    assert tool.calls == 1
    assert update["tool_calls"][0].status.value == "failed"
    assert types == [TraceEventType.TOOL_STARTED, TraceEventType.TOOL_FAILED]

```

### tests/test_phase9_persistence.py

```python
"""Phase 9 SQLite checkpoint、journal 与 metadata 的离线测试。"""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from langgraph.graph import END, START, StateGraph

from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    StepResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.browser.tools import BrowserTools
from taskpilot.persistence import (
    ActionJournalRecord,
    JournalStatus,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.persistence.journal import utc_now
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
)
from taskpilot.state import TaskState


def complete_state() -> TaskState:
    decision = PolicyDecision(
        call_id="call-1",
        step_id=1,
        tool_name="local_read",
        risk_level=RiskLevel.HIGH,
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        reason="offline fixture",
        tool_source="local",
        preview={"query": "safe"},
    )
    return {
        "task_id": "round-trip-task",
        "user_input": "持久化完整状态",
        "task_spec": TaskSpec(
            goal="持久化完整状态",
            constraints={"offline": True},
            completion_criteria=["类型可恢复"],
        ),
        "plan": [PlanStep(id=1, description="测试", success_criteria=["通过"])],
        "current_step_id": 1,
        "step_outcome": None,
        "step_results": [StepResult(step_id=1, summary="已有结果")],
        "step_verification": None,
        "tool_calls": [
            ToolCallRecord(
                call_id="old-call",
                step_id=1,
                tool_name="local_read",
                status=ToolCallStatus.SUCCESS,
                result={"ok": True},
            )
        ],
        "task_results": {"answer": 42},
        "verification": None,
        "pending_action": PendingToolAction(
            call_id="call-1",
            step_id=1,
            tool_name="local_read",
            arguments={"query": "safe"},
            policy_decision=decision,
        ),
        "policy_decisions": [decision],
        "approval_records": [
            ApprovalRecord(
                call_id="call-1",
                step_id=1,
                tool_name="local_read",
                decision=ApprovalChoice.APPROVE,
            )
        ],
        "current_action_index": 1,
        "pending_executor_action": ExecutorAction(
            step_id=1,
            action_index=1,
            call_id="call-1",
            action_type=ExecutorActionType.TOOL,
            tool_name="local_read",
            arguments={"query": "safe"},
        ),
        "recovery_issue": None,
        "status": TaskStatus.WAITING_APPROVAL,
        "error": None,
    }


def checkpoint_graph(checkpointer: Any):
    builder = StateGraph(TaskState)
    builder.add_node("persist", lambda state: {})
    builder.add_edge(START, "persist")
    builder.add_edge("persist", END)
    return builder.compile(checkpointer=checkpointer)


def test_persistence_config_resolves_paths(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path / "state")

    assert config.checkpoint_db_path.is_absolute()
    assert config.action_journal_db_path.is_absolute()
    assert config.checkpoint_db_path.name == "checkpoints.sqlite3"
    assert config.action_journal_db_path.name == "actions.sqlite3"


def test_browser_replay_rules_are_conservative_and_explicit() -> None:
    tools = {tool.name: tool.metadata for tool in BrowserTools(object()).build()}

    assert tools["browser_observe"]["replay_safe"] is True
    assert tools["browser_navigate"]["replay_safe"] is True
    assert tools["browser_fill"]["replay_safe"] is True
    assert tools["browser_click"]["replay_safe"] is False
    assert tools["browser_download"]["replay_safe"] is False
    assert tools["browser_screenshot"]["replay_safe"] is False
    assert tools["browser_switch_page"]["replay_safe"] is False


def test_task_state_schema_contains_no_live_runtime_objects() -> None:
    forbidden = {
        "registry", "browser_session", "page", "mcp_session",
        "llm", "sqlite_connection", "checkpointer",
    }

    assert forbidden.isdisjoint(TaskState.__annotations__)


@pytest.mark.asyncio
async def test_sqlite_checkpoint_round_trip_restores_structured_state(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    graph_config = {"configurable": {"thread_id": "round-trip-task"}}

    async with SQLiteCheckpointProvider(config) as first:
        assert first.checkpointer is not None
        await checkpoint_graph(first.checkpointer).ainvoke(
            complete_state(), config=graph_config
        )
        history = await first.list_task_checkpoints("round-trip-task")
        assert len(history) >= 2

    async with SQLiteCheckpointProvider(config) as second:
        assert second.checkpointer is not None
        snapshot = await checkpoint_graph(second.checkpointer).aget_state(graph_config)

    restored = snapshot.values
    assert isinstance(restored["task_spec"], TaskSpec)
    assert isinstance(restored["plan"][0], PlanStep)
    assert isinstance(restored["step_results"][0], StepResult)
    assert isinstance(restored["tool_calls"][0], ToolCallRecord)
    assert isinstance(restored["policy_decisions"][0], PolicyDecision)
    assert isinstance(restored["pending_action"], PendingToolAction)
    assert isinstance(restored["approval_records"][0], ApprovalRecord)
    assert isinstance(restored["pending_executor_action"], ExecutorAction)
    assert restored["task_id"] == "round-trip-task"


@pytest.mark.asyncio
async def test_sqlite_action_journal_commits_started_retry_and_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    now = utc_now()
    original = ActionJournalRecord(
        action_key="task:1:0:1",
        task_id="task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="local_read",
        arguments_hash="abc",
        status=JournalStatus.STARTED,
        replay_safe=True,
        created_at=now,
        updated_at=now,
    )
    async with SQLiteActionJournal(path) as journal:
        await journal.start(original)
    async with SQLiteActionJournal(path) as journal:
        started = await journal.get(original.action_key)
        assert started is not None
        assert started.status is JournalStatus.STARTED
        retried = await journal.retry_started(original.action_key)
        assert retried.attempt_count == 2
        terminal = await journal.succeed(original.action_key, {"ok": True})
        assert terminal.status is JournalStatus.SUCCEEDED
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None
        assert restored.result == {"ok": True}
        assert restored.attempt_count == 2


@pytest.mark.asyncio
async def test_sqlite_action_journal_one_shot_retry_permit_is_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "one-shot-journal.sqlite3"
    now = utc_now()
    original = ActionJournalRecord(
        action_key="task:1:0:1",
        task_id="task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="unsafe_write",
        arguments_hash="abc",
        status=JournalStatus.STARTED,
        replay_safe=False,
        created_at=now,
        updated_at=now,
    )
    async with SQLiteActionJournal(path) as journal:
        await journal.start(original)
        authorized = await journal.authorize_retry(original.action_key)
        assert authorized.attempt_count == 1
        assert authorized.authorized_retry_attempt == 2
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None and restored.authorized_retry_attempt == 2
        consumed = await journal.consume_authorized_retry(original.action_key)
        assert consumed.attempt_count == 2
        assert consumed.authorized_retry_attempt is None
        with pytest.raises(ValueError, match="不存在、已消费"):
            await journal.consume_authorized_retry(original.action_key)
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None
        assert restored.attempt_count == 2
        assert restored.authorized_retry_attempt is None


@pytest.mark.asyncio
async def test_sqlite_action_journal_migrates_phase9_schema(tmp_path: Path) -> None:
    path = tmp_path / "phase9-schema.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            """
            CREATE TABLE action_journal (
                action_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                retry_count INTEGER NOT NULL,
                action_index INTEGER NOT NULL,
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                replay_safe INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await connection.commit()
    async with SQLiteActionJournal(path):
        pass
    async with aiosqlite.connect(path) as connection:
        cursor = await connection.execute("PRAGMA table_info(action_journal)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
    assert "authorized_retry_attempt" in columns

```

### tests/test_phase9_recovery.py

```python
"""Phase 9 action boundary、crash window、recovery 与 durable HITL 测试。"""

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
import aiosqlite
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.types import Command

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.cli import async_main
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.persistence import (
    ExecutionFaultPoint,
    InjectedCrash,
    JournalStatus,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations = 0
        self.messages: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations += 1
        self.messages.append(list(messages))
        return self.responses.pop(0)


def tool_call(call_id: str = "tool-1", value: int = 7) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "counter",
            "args": {"value": value},
            "id": call_id,
            "type": "tool_call",
        }],
    )


def finish_call(call_id: str = "finish-2") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": FINISH_STEP_NAME,
            "args": {
                "summary": "durable observation available",
                "evidence": ["counter observation is journaled"],
                "output": {"done": True},
            },
            "id": call_id,
            "type": "tool_call",
        }],
    )


@dataclass
class CountingTool:
    replay_safe: bool
    risk: str = "low"
    calls: list[dict[str, Any]] = field(default_factory=list)
    name: str = "counter"
    description: str = "Count deterministic offline invocations"
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    })

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "source": "local",
            "risk": self.risk,
            "replay_safe": self.replay_safe,
        }

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"call_count": len(self.calls), "value": arguments["value"]}


@dataclass
class FileCounterTool(CountingTool):
    """用临时文件模拟跨 runtime 可观察的外部副作用。"""

    counter_path: Path = Path("counter.txt")
    fail_after_increment: bool = False

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        count = (
            int(self.counter_path.read_text(encoding="utf-8"))
            if self.counter_path.exists()
            else 0
        ) + 1
        self.counter_path.write_text(str(count), encoding="utf-8")
        if self.fail_after_increment:
            raise RuntimeError("controlled terminal failure")
        return {"call_count": count, "value": arguments["value"]}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["finish"])


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[PlanStep(
            id=1,
            description="run one durable action",
            success_criteria=["observation exists"],
        )])


class TwoStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[
            PlanStep(id=1, description="first", success_criteria=["first done"]),
            PlanStep(
                id=2,
                description="second",
                depends_on=[1],
                success_criteria=["second done"],
            ),
        ])


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="offline accepted",
            )],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="offline complete")


class RejectOnceVerifier(FakeVerifier):
    def __init__(self) -> None:
        self.calls = 0

    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        self.calls += 1
        if self.calls == 1:
            return StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(
                    criterion_index=0,
                    satisfied=False,
                    reason="need durable evidence",
                )],
                feedback="need durable evidence",
            )
        return super().verify_step(**kwargs)


class CrashOnce:
    def __init__(self, point: ExecutionFaultPoint) -> None:
        self.point = point
        self.fired = False

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if point is self.point and not self.fired:
            self.fired = True
            raise InjectedCrash(f"{point.value}:{action_key}")


class CrashOnStepTwo(CrashOnce):
    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if ":2:" in action_key:
            super().hit(point, action_key)


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "run durable fixture",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "pending_action": None,
        "policy_decisions": [],
        "approval_records": [],
        "current_action_index": 0,
        "pending_executor_action": None,
        "recovery_issue": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def make_executor(tool: CountingTool, responses: Sequence[AIMessage]) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, FakeFunctionCallingModel(responses)),
        policy=DefaultRiskPolicy(),
    )


def graph_config(task_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": task_id},
        "recursion_limit": 100,
    }


def interrupts(snapshot: Any) -> list[Any]:
    return [item for task in snapshot.tasks for item in task.interrupts]


@pytest.mark.asyncio
async def test_action_level_graph_runs_one_tool_then_one_finish(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool = CountingTool(replay_safe=True)
    model = FakeFunctionCallingModel([tool_call(), finish_call()])
    registry = ToolRegistry()
    registry.register(tool)
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=executor,
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(initial_state("action-loop"), config=graph_config("action-loop"))

    nodes = set(graph.get_graph().nodes)
    assert {"decide_action", "execute_tool_action", "build_step_outcome"} <= nodes
    assert model.invocations == 2
    assert len(tool.calls) == 1
    assert len(result["tool_calls"]) == 1
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_risk_level_and_replay_safe_are_independent() -> None:
    tool = CountingTool(replay_safe=True, risk="high")
    executor = make_executor(tool, [finish_call()])
    decision_result = await executor.decide_action(
        task_spec=TaskSpec(goal="independence"),
        step=PlanStep(id=1, description="check", success_criteria=["checked"]),
        action_index=1,
        task_results={},
        tool_calls=[],
    )
    # 上面的模型返回 finish，因此另造 frozen Tool action 更直接验证两条轴。
    from taskpilot.models import ExecutorAction, ExecutorActionType

    action_model = ExecutorAction(
        step_id=1,
        action_index=1,
        call_id="independent",
        action_type=ExecutorActionType.TOOL,
        tool_name="counter",
        arguments={"value": 7},
    )
    policy_decision = await executor.assess_action(action_model)

    assert decision_result.action is not None
    assert executor.is_replay_safe(action_model) is True
    assert policy_decision is not None
    assert policy_decision.outcome.value == "require_approval"


@pytest.mark.asyncio
async def test_terminal_journal_deduplicates_after_graph_checkpoint_crash(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "terminal-dedup"
    counter_path = tmp_path / "terminal-counter.txt"
    tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    rebuilt_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None and record.status is JournalStatus.SUCCEEDED
        assert len(tool.calls) == 1

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert len(tool.calls) == 1
    assert rebuilt_tool.calls == []
    assert counter_path.read_text(encoding="utf-8") == "1"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0].result == {"call_count": 1, "value": 7}
    assert result["status"] is TaskStatus.COMPLETED
    print("TERMINAL_JOURNAL_CRASH_SMOKE=" + json.dumps({
        "tool_counter_before": 0,
        "tool_counter_after_execution": 1,
        "injected_crash": True,
        "journal_status": "succeeded",
        "tool_reinvoked": False,
        "tool_counter_final": 1,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_replay_safe_started_is_automatically_retried(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "safe-started"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert tool.calls == []

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")

    assert len(tool.calls) == 1
    assert record is not None and record.attempt_count == 2
    assert result["status"] is TaskStatus.COMPLETED
    print("REPLAY_SAFE_RECOVERY_SMOKE=" + json.dumps({
        "journal_status_before": "started",
        "automatic_retry": True,
        "recovery_interrupt": False,
        "attempt_count": record.attempt_count,
        "tool_counter_final": len(tool.calls),
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_unsafe_started_interrupts_and_abort_does_not_replay(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-abort"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert len(tool.calls) == 1

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        pending_interrupts = interrupts(snapshot)
        assert paused["status"] is TaskStatus.INTERRUPTED
        assert pending_interrupts[0].value["type"] == "ambiguous_tool_recovery"
        assert len(tool.calls) == 1
        print("AMBIGUOUS_UNSAFE_CRASH_SMOKE=" + json.dumps({
            "journal": "started",
            "side_effect_counter": 1,
            "replay_safe": False,
            "automatic_retry": False,
            "recovery_interrupt": True,
        }, sort_keys=True))
        result = await graph.ainvoke(
            Command(resume={"choice": "abort", "reason": "do not duplicate"}),
            config=graph_config(task_id),
        )

    assert len(tool.calls) == 1
    assert result["status"] is TaskStatus.FAILED
    assert result["recovery_issue"] is not None


@pytest.mark.asyncio
async def test_unsafe_started_human_retry_replays_exact_action(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-retry"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        result = await graph.ainvoke(
            Command(resume={"choice": "retry", "reason": "duplicate accepted"}),
            config=graph_config(task_id),
        )
        record = await journal.get(f"{task_id}:1:0:1")

    assert len(tool.calls) == 2
    assert tool.calls[0] == tool.calls[1] == {"value": 7}
    assert record is not None and record.attempt_count == 2
    assert result["status"] is TaskStatus.COMPLETED


async def _create_second_ambiguous_crash(
    tmp_path: Path,
    task_id: str,
) -> tuple[PersistenceConfig, Path]:
    """两次真实副作用都发生，但两次 terminal journal 前均 crash。"""

    config = PersistenceConfig.from_state_dir(tmp_path)
    counter_path = tmp_path / "one-shot-counter.txt"
    first_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
    assert counter_path.read_text(encoding="utf-8") == "1"

    second_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(second_tool, [finish_call("after-second-retry")]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(
                Command(resume={"choice": "retry", "reason": "one retry only"}),
                config=graph_config(task_id),
            )
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None
        assert record.attempt_count == 2
        assert record.authorized_retry_attempt is None
    assert counter_path.read_text(encoding="utf-8") == "2"
    return config, counter_path


@pytest.mark.asyncio
async def test_unsafe_human_retry_is_one_shot_after_second_crash(
    tmp_path: Path,
) -> None:
    task_id = "unsafe-one-shot-abort"
    config, counter_path = await _create_second_ambiguous_crash(tmp_path, task_id)
    third_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(third_tool, [finish_call("must-not-auto-run")]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert paused["status"] is TaskStatus.INTERRUPTED
        assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"
        assert counter_path.read_text(encoding="utf-8") == "2"
        assert third_tool.calls == []
        result = await graph.ainvoke(
            Command(resume={"choice": "abort", "reason": "no third side effect"}),
            config=graph_config(task_id),
        )

    assert result["status"] is TaskStatus.FAILED
    assert counter_path.read_text(encoding="utf-8") == "2"
    print(
        "ONE_SHOT_RECOVERY_SMOKE="
        + json.dumps(
            {
                "counter_after_first_ambiguous": 1,
                "counter_after_human_retry": 2,
                "second_crash": True,
                "automatic_third_invoke": False,
                "second_recovery_interrupt": True,
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_second_unsafe_retry_requires_second_human_confirmation(
    tmp_path: Path,
) -> None:
    task_id = "unsafe-one-shot-retry-again"
    config, counter_path = await _create_second_ambiguous_crash(tmp_path, task_id)
    third_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor=make_executor(third_tool, [finish_call("after-third-retry")]),
            verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"
        result = await graph.ainvoke(
            Command(resume={"choice": "retry", "reason": "second explicit retry"}),
            config=graph_config(task_id),
        )
        record = await journal.get(f"{task_id}:1:0:1")

    assert result["status"] is TaskStatus.COMPLETED
    assert counter_path.read_text(encoding="utf-8") == "3"
    assert third_tool.calls == [{"value": 7}]
    assert record is not None and record.attempt_count == 3
    assert record.authorized_retry_attempt is None


@pytest.mark.asyncio
async def test_high_risk_approval_survives_checkpointer_recreation(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "durable-approval"
    first_tool = CountingTool(replay_safe=False, risk="high")
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        paused = await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert paused["status"] is TaskStatus.WAITING_APPROVAL
        assert interrupts(snapshot)[0].value["type"] == "tool_approval"
        assert first_tool.calls == []

    rebuilt_tool = CountingTool(replay_safe=False, risk="high")
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "tool_approval"
        result = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=graph_config(task_id),
        )

    assert rebuilt_tool.calls == [{"value": 7}]
    assert result["status"] is TaskStatus.COMPLETED
    print("DURABLE_APPROVAL_SMOKE=" + json.dumps({
        "runtime_recreated": True,
        "pending_approval_restored": True,
        "tool_counter_before_approval": 0,
        "tool_counter_after_approval": len(rebuilt_tool.calls),
        "status": result["status"].value,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_terminal_failed_journal_is_materialized_without_reinvoke(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "terminal-failed"
    counter_path = tmp_path / "failed-counter.txt"
    first_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
        fail_after_increment=True,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None and record.status is JournalStatus.FAILED

    rebuilt_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
        fail_after_increment=True,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert counter_path.read_text(encoding="utf-8") == "1"
    assert rebuilt_tool.calls == []
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0].status.value == "failed"
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_unsafe_crash_before_invoke_is_still_ambiguous(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-before-invoke"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert tool.calls == []

    rebuilt_tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))

    assert rebuilt_tool.calls == []
    assert paused["status"] is TaskStatus.INTERRUPTED
    assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"


@pytest.mark.asyncio
async def test_arguments_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "hash-mismatch"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))

    async with aiosqlite.connect(config.action_journal_db_path) as connection:
        await connection.execute(
            "UPDATE action_journal SET arguments_hash = ? WHERE action_key = ?",
            ("tampered", f"{task_id}:1:0:1"),
        )
        await connection.commit()

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert tool.calls == []
    assert result["status"] is TaskStatus.FAILED
    assert "identity/hash" in result["error"]


@pytest.mark.asyncio
async def test_completed_step_is_not_rerun_after_step_two_crash(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "two-step-restart"
    counter_path = tmp_path / "two-step-counter.txt"
    first_runtime_tool = FileCounterTool(
        replay_safe=True, counter_path=counter_path
    )
    first_model = FakeFunctionCallingModel([
        tool_call("step-1-tool", value=1),
        finish_call("step-1-finish"),
        tool_call("step-2-tool", value=2),
    ])
    registry = ToolRegistry()
    registry.register(first_runtime_tool)
    first_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, first_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=TwoStepPlanner(),
            executor=first_executor, verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnStepTwo(
                ExecutionFaultPoint.AFTER_JOURNAL_STARTED
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert snapshot.values["plan"][0].status.value == "completed"
        assert counter_path.read_text(encoding="utf-8") == "1"

    rebuilt_tool = FileCounterTool(replay_safe=True, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=TwoStepPlanner(),
            executor=make_executor(rebuilt_tool, [finish_call("step-2-finish")]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert counter_path.read_text(encoding="utf-8") == "2"
    assert rebuilt_tool.calls == [{"value": 2}]
    assert [step.status.value for step in result["plan"]] == ["completed", "completed"]
    assert result["status"] is TaskStatus.COMPLETED
    print("FULL_RESTART_SMOKE=" + json.dumps({
        "step_1_tool_count": 1,
        "step_1_status": result["plan"][0].status.value,
        "step_2_tool_count": len(rebuilt_tool.calls),
        "step_2_status": result["plan"][1].status.value,
        "task_status": result["status"].value,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_verifier_retry_count_and_feedback_survive_restart(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "verifier-retry"
    tool = CountingTool(replay_safe=True)
    first_model = FakeFunctionCallingModel([
        finish_call("first-claim"),
        tool_call("retry-tool"),
    ])
    registry = ToolRegistry()
    registry.register(tool)
    first_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, first_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=first_executor, verifier=RejectOnceVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert snapshot.values["plan"][0].retry_count == 1
        assert snapshot.values["step_verification"].feedback == "need durable evidence"

    second_model = FakeFunctionCallingModel([finish_call("retry-finish")])
    registry = ToolRegistry()
    registry.register(tool)
    second_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, second_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=second_executor,
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert result["plan"][0].retry_count == 1
    assert "need durable evidence" in str(second_model.messages[0][-1].content)
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_action_level_max_actions_remains_bounded(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool = CountingTool(replay_safe=True)
    registry = ToolRegistry()
    registry.register(tool)
    model = FakeFunctionCallingModel([tool_call()])
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        max_actions_per_step=1,
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=executor,
            verifier=FakeVerifier(), max_step_attempts=1,
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(
            initial_state("bounded-actions"), config=graph_config("bounded-actions")
        )

    assert model.invocations == 1
    assert len(tool.calls) == 1
    assert result["step_outcome"].error == "Executor action limit reached: 1"
    assert result["status"] is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_cli_resume_completed_task_does_not_run_graph_again(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "completed-cli"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call(), finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        completed = await graph.ainvoke(
            initial_state(task_id), config=graph_config(task_id)
        )
        assert completed["status"] is TaskStatus.COMPLETED

    exit_code = await async_main(
        ["--resume", task_id, "--state-dir", str(tmp_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task already completed" in output
    assert "status=completed" in output
    assert f"task_id={task_id}" in output
    assert len(tool.calls) == 1

```

## 30. Pytest Real Result

Focused command：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest tests/test_context.py tests/test_phase9_recovery.py tests/test_phase10_graph.py tests/test_phase10_trace_boundaries.py -q
```

真实输出：

```text
................................................                         [100%]
48 passed in 3.09s
```

Full command：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q
```

真实输出：

```text
........................................................................ [ 35%]
........................................................................ [ 71%]
.........................................................                [100%]
201 passed in 23.22s
```

旧 195 项全部保留或等价迁移；当前 failed=0、errors=0。pytest 使用 Windows 系统临时目录和唯一 run directory，没有全局固定 basetemp。

## 31. Step Failure → Replan Smoke

```text
REPLAN_STEP_FAILURE_SMOKE={"completed_old_step_rerun": false, "failed_step_id": 2, "initial_revision": 0, "new_revision": 1, "new_step_ids": [4, 5], "replan_count": 1, "replan_trigger": "step_attempts_exhausted", "task_status": "completed"}
```

## 32. Task Verification → Replan Smoke

```text
TASK_VERIFICATION_REPLAN_SMOKE={"first_task_verification": false, "missing_requirements": ["produce final format"], "replan_count": 1, "second_task_verification": true, "task_status": "completed"}
```

## 33. Context Budget Smoke

```text
CONTEXT_BUDGET_SMOKE={"dependency_result_preserved": true, "goal_preserved": true, "historical_tool_calls": 20, "included_raw_tool_calls": 5, "truncated_sections": ["tool_observation:call-15", "tool_observation:call-16", "tool_observation:call-17", "tool_observation:call-18", "tool_observation:call-19"]}
```

## 34. Trace Reopen Smoke

```text
TRACE_SMOKE={"approval_requests": 0, "event_count": 14, "recovery_required": 0, "replans": 0, "step_rejections": 0, "task_id": "sqlite-trace-smoke", "tool_calls": 1, "tool_failures": 0, "trace_reopened": true}
```

## 35. Known Limitations and Boundary Q&A

- Context budget 是 character budget，不是 tokenizer 精确 token budget。
- Structured-output Analyzer/Planner/Replanner raw usage 暂不可自然取得。
- Trace DB 与 LangGraph checkpoint 独立提交；除特殊处理的 Tool Journal boundary 外，极端 crash 可能造成非 Tool semantic trace event replay/duplicate。Phase 12 关键 Tool metrics 应结合 ToolCallRecord、Action Journal 与 Trace，不能假设 Trace 天然 exactly-once。
- SQLite Trace 是单进程实现，没有 distributed collector/OpenTelemetry。
- 没有 Vision/OCR、FastAPI、vector memory、Benchmark execution。

Q1 Step retry exhausted 是否一定 Task FAILED？No，有 budget 时先 Replan。

Q2 Task Verifier 拒绝是否一定 Task FAILED？No，可按 missing requirements Replan。

Q3 Replanner 能否重跑 completed Step？No。

Q4 fresh ID 原因？避免 Phase 9 durable Action Journal identity 冲突。

Q5 failed/skipped 历史是否删除？No。

Q6 Replan 是否无限？No，max_replans 有界。

Q7 Replanner 是否选 Tool？No，只规划 WHAT。

Q8 State/Context/Trace 是否相同？No，职责与持久化边界不同。

Q9 Executor 是否看全部 raw history？No，只选近期相关信息。

Q10 能否为省 Context 删除 State history？No。

Q11 Trace 是否保存 raw secrets？No。

Q12 Trace 能否 resume 继续？Yes。

Q13 Context 是否精确 token budget？No。

Q14 是否实现 OpenTelemetry？No。

Q15 是否运行 Benchmark？No。

Q16 Human recovery RETRY 是否永久授权未来所有 ambiguous retries？No。每次 Human RETRY 只签发 Journal 中下一 attempt 的 one-shot permit；invoke 前 durable consume，下一次 ambiguous crash 必须再次人工确认。

## 36. Files Changed

Acceptance Repair 修改：executor.py、graph.py、persistence/models.py、persistence/journal.py、test_phase10_graph.py、test_phase9_persistence.py、test_phase9_recovery.py、phase_10.md。无新增 runtime dependency，无 Phase 11 文件。

## 37. git status --short

```text
 M .gitignore
 M README.md
 M docs/review/phase_03.md
 M pyproject.toml
 M src/taskpilot/cli.py
 M src/taskpilot/graph.py
 M src/taskpilot/models.py
 M src/taskpilot/planner.py
 M src/taskpilot/state.py
 M tests/test_analyzer.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
 M tests/test_planner.py
?? docs/review/phase_04.md
?? docs/review/phase_05.md
?? docs/review/phase_06.md
?? docs/review/phase_07.md
?? docs/review/phase_08.md
?? docs/review/phase_09.md
?? docs/review/phase_10.md
?? src/taskpilot/browser/
?? src/taskpilot/context/
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/persistence/
?? src/taskpilot/policy/
?? src/taskpilot/replanner.py
?? src/taskpilot/tools/
?? src/taskpilot/trace/
?? src/taskpilot/verifier.py
?? tests/conftest.py
?? tests/fixtures/
?? tests/test_browser.py
?? tests/test_browser_executor.py
?? tests/test_browser_policy.py
?? tests/test_context.py
?? tests/test_executor.py
?? tests/test_hitl.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase10_graph.py
?? tests/test_phase10_trace_boundaries.py
?? tests/test_phase5_graph.py
?? tests/test_phase9_mcp_resume.py
?? tests/test_phase9_persistence.py
?? tests/test_phase9_recovery.py
?? tests/test_policy.py
?? tests/test_replanner.py
?? tests/test_tools.py
?? tests/test_trace.py
?? tests/test_verifier.py
```

工作区既有 Phase 3–9 与 Phase 10 修改均未删除、未 reset、未自动 commit/push。

## Phase 10 Acceptance Repair

### 1. ContextBudgetError production Graph fix

`TaskExecutor.decide_action` 先构造 Context，再创建/调用 model。ContextBudgetError 被转换为 `ExecutorDecisionResult(error="Context budget exceeded: ...", fatal_error=True, context_snapshot=None)`。Graph 正常返回 FAILED State，不抛 invocation exception。

### 2. Graph-level Context overflow smoke

```text
CONTEXT_OVERFLOW_GRAPH_SMOKE={"graph_exception": false, "model_invoked": false, "task_status": "failed"}
```

真实 TaskExecutor、Fake model、极小 ContextConfig；模型调用 0 次、Tool 调用 0 次。

### 3. One-shot Human recovery authorization architecture

```text
unsafe STARTED
→ recovery_review interrupt
→ Human RETRY
→ authorize_recovery_retry node
→ Journal authorize next attempt + commit
→ Graph checkpoint
→ execute_tool_action
→ Journal consume permit + increment attempt + commit
→ external invoke once
```

RecoveryIssue.retry_authorized 仅为旧 checkpoint schema 兼容字段，固定为 false，不再具有执行授权能力。

### 4. Journal schema/update

ActionJournal 新增 nullable `authorized_retry_attempt`。SQLite setup 使用 PRAGMA table_info 检测 Phase 9 schema并执行单列 ALTER TABLE migration。内存与 SQLite journal 都实现 authorize/consume。

### 5. Retry permit durable consume ordering

Permit 值必须等于 `attempt_count + 1`。consume 使用条件 UPDATE 同时增加 attempt_count、清空 permit并 commit，然后才 invoke。若 consume 失败，Tool 不执行。

### 6. Second ambiguous crash regression

```text
ONE_SHOT_RECOVERY_SMOKE={"automatic_third_invoke": false, "counter_after_first_ambiguous": 1, "counter_after_human_retry": 2, "second_crash": true, "second_recovery_interrupt": true}
```

Restart C 的 counter 保持 2，自动第三次调用为 false，随后 Human ABORT，Task FAILED。

### 7. Second Human confirmation regression

另一个真实 SQLite/checkpoint test 在 Restart C 第二次 Human RETRY 后 counter 才从 2 变 3；Journal attempt_count=3 且 permit 再次消费为 null。

### 8. PLAN_REPLANNED revision fix

`_trace` 新增 `plan_revision_override`。成功 Replan 事件使用 next revision；失败事件仍使用当前旧 revision。两次成功 Replan 测试断言 event revisions 为 [1,2]。

```text
REPLAN_TRACE_REVISION_SMOKE={"payload_revision": 1, "trace_event_revision": 1}
```

### 9. Actual focused pytest output

```text
48 passed in 3.09s
```

### 10. Actual full pytest output

```text
201 passed in 23.22s
```

### 11. Updated Trace limitation

Trace 与 checkpoint 独立提交，非 Tool semantic events 在极端 crash 下不是 exactly-once。没有在本修复中扩张为 distributed/idempotent tracing system。

### 12. Git status

以第 37 节当前真实 `git status --short` 为准。
