# TaskPilot Phase 5 Review

## 1. Phase Goal

本阶段目标是给 Phase 4 的 Executor claim 增加真实的 Step/Task 两层验证门，并让通过验证的步骤结果能够安全交给依赖步骤。

实际完成：

- criterion-level Step Verification；
- 确定性的 Verifier 输出一致性校验；
- Verifier feedback 注入和有上限的同 Step 重试；
- 通过验证后生成正式 `StepResult`；
- dependency-aware 步骤选择与跨步骤结果交接；
- 所有 Step 完成后的独立 Task-level Verification；
- 只有 Task Verifier 接受时才设置 `TaskStatus.COMPLETED`。

明确没有实现 Dynamic Replanning、MCP、Playwright/Browser、真实 Search、production Filesystem Tool、HITL、Checkpoint/Recovery、SQLite、FastAPI、Vision 或 Benchmark execution。达到重试上限或 Task Verification 拒绝时，本阶段直接失败，不触发 Replan。

## 2. Final Directory Tree

```text
TaskPilot/
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
├── benchmarks/
│   └── README.md
├── docs/
│   └── review/
│       ├── phase_03.md
│       ├── phase_04.md
│       └── phase_05.md
├── src/
│   └── taskpilot/
│       ├── __init__.py
│       ├── analyzer.py
│       ├── cli.py
│       ├── config.py
│       ├── executor.py
│       ├── graph.py
│       ├── llm.py
│       ├── models.py
│       ├── planner.py
│       ├── state.py
│       ├── verifier.py
│       └── tools/
│           ├── __init__.py
│           ├── base.py
│           └── registry.py
└── tests/
    ├── test_analyzer.py
    ├── test_executor.py
    ├── test_graph_smoke.py
    ├── test_models.py
    ├── test_phase5_graph.py
    ├── test_planner.py
    ├── test_tools.py
    └── test_verifier.py
```

目录树排除了 `.git/`、`__pycache__/`、`.pytest_cache/` 和包构建缓存。

## 3. 完整 Graph Flow

```text
START
→ initialize_task
→ analyze_task
→ plan_task
→ execute_step
   ├─ fatal error → END (Task failed)
   └─ non-fatal outcome → verify_step
      ├─ verified → advance_step
      │  ├─ next executable Step → execute_step
      │  ├─ all Steps completed → verify_task → END
      │  └─ pending Steps blocked → END (Task failed)
      ├─ rejected and retry_count < max_step_attempts → execute_step
      └─ rejected and retry_count >= max_step_attempts → END (Task failed)
```

职责：

- `initialize_task`：只执行 `created → running`。
- `analyze_task`：生成结构化 `TaskSpec`。
- `plan_task`：生成计划并选择第一条依赖已满足的 pending Step。
- `execute_step`：仅执行当前 Step，记录原始 ToolCallRecords 和不可信的 StepExecutionOutcome claim。
- `verify_step`：逐项验证当前 Step 的 success criteria；拒绝时累计 retry_count。
- `advance_step`：只接受 verified claim，生成 StepResult，并选择下一可执行 Step。
- `verify_task`：所有 Step 完成后独立验证 Task completion criteria。
- 条件边由 LangGraph 管理；没有 Python 外层 while。

## 4. Data Model Changes

新增和修改的数据含义：

- `StepExecutionOutcome.output`：Executor 声称本步骤产生的结构化输出；验证前不可信。
- `StepResult`：只在 Step Verifier 接受后生成的正式步骤产物。
- `CriterionCheck`：用原 success_criteria 的下标记录逐项验证结果，不让模型重新定义 criterion。
- `StepVerificationResult`：当前 Step 最近一次验证结论和反馈。
- `TaskState.step_results`：全部已验证的正式步骤产物。
- `TaskState.step_verification`：当前 Step 最近一次验证结果。
- `TaskState.verification`：保留为整个任务的最终验证结果。
- `PlanStep.retry_count`：本阶段开始实际使用，每次 Verifier rejection 增加 1。
- `task_results`：语义不变，仍是与最终用户目标直接相关的任务级结构化结果；它不等同于 step_results 或 tool_calls。

完整当前代码：

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


class TaskPlan(BaseModel):
    """Initial Planner 生成的结构化任务计划。"""

    steps: list[PlanStep]


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
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)


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
    status: TaskStatus
    error: str | None
```

## 5. StepResult 完整定义

```python
class StepResult(BaseModel):
    """已经通过 Step Verifier 的正式步骤产物。"""

    step_id: int
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
```

来源规则：`summary`、`evidence` 和 `output` 直接取自已经通过 Step Verification 的 `StepExecutionOutcome`。不会调用另一个 LLM 重写 output。同一 step_id 使用 upsert，最终列表不重复。

## 6. StepVerificationResult / CriterionCheck 完整定义

```python
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
```

`criterion_index` 对应 `PlanStep.success_criteria[index]`。checks 不重复、不遗漏，且 `verified` 必须与全部 `satisfied` 的合取完全一致。

## 7. finish_step 新 Schema

```python
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
```

`output` 是 required JSON object。`finish_step` 只创建 `StepExecutionOutcome(claimed_complete=True)`，不会创建 StepResult，也不会更新 Step status。

## 8. Executor Cross-Step Context Changes

当前 Executor 上下文仅含：

- Task goal 和 constraints；
- Current Step description / success_criteria；
- 当前 Step 自己的历史 ToolCallRecords；
- task_results；
- 当前 Step `depends_on` 对应的 verified StepResults；
- 仅当 step_id 匹配时的最近一次 Verifier feedback。

不会注入整个 Plan、其他 Step 的 raw Tool Calls、无关 StepResults 或其他 Step 的旧 feedback。Graph 向 Executor 传入结构化 `step_results` 与 `verification_feedback`，`build_executor_messages` 再按依赖和 step_id 过滤。

完整当前实现（包括本阶段同步后的 CLI 初始状态）：

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
from taskpilot.llm import create_chat_model
from taskpilot.models import (
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
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

    outcome: StepExecutionOutcome
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    fatal_error: bool = False


class TaskExecutorLike(Protocol):
    """真实 Executor 与离线 Fake 共同遵循的最小接口。"""

    def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
    ) -> ExecutorRunResult:
        """只执行传入的当前步骤。"""

        ...


def build_executor_messages(
    *,
    task_spec: TaskSpec,
    step: PlanStep,
    task_results: dict[str, Any],
    tool_calls: Sequence[ToolCallRecord],
    step_results: Sequence[StepResult] = (),
    verification_feedback: StepVerificationResult | None = None,
) -> list[BaseMessage]:
    """只使用当前步骤执行所需的信息构造模型上下文。"""

    observations = [
        {
            "call_id": record.call_id,
            "tool_name": record.tool_name,
            "status": record.status.value,
            "result": record.result,
            "error": record.error,
        }
        for record in tool_calls
        if record.step_id == step.id
    ]
    dependency_results = [
        result.model_dump(mode="json")
        for result in step_results
        if result.step_id in step.depends_on
    ]
    feedback = (
        verification_feedback.model_dump(mode="json")
        if verification_feedback is not None
        and verification_feedback.step_id == step.id
        else None
    )
    context = {
        "task_goal": task_spec.goal,
        "task_constraints": task_spec.constraints,
        "current_step": {
            "id": step.id,
            "description": step.description,
            "success_criteria": step.success_criteria,
        },
        "current_step_observations": observations,
        "task_results": task_results,
        "dependency_results": dependency_results,
        "verification_feedback": feedback,
    }
    return [
        SystemMessage(content=EXECUTOR_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)),
    ]


class TaskExecutor:
    """使用原生 Tool Calling 反复执行当前步骤，直到 finish_step 或动作上限。"""

    def __init__(
        self,
        registry: ToolRegistry,
        model: BaseChatModel | None = None,
        max_actions_per_step: int = 5,
    ) -> None:
        if max_actions_per_step < 1:
            raise ValueError("max_actions_per_step 必须至少为 1")
        self._registry = registry
        self._model = model
        self._max_actions_per_step = max_actions_per_step

    def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
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
        )
        new_records: list[ToolCallRecord] = []
        seen_call_ids = {record.call_id for record in tool_calls}

        for action_index in range(self._max_actions_per_step):
            action_count = action_index + 1
            try:
                response = model_with_tools.invoke(messages)
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
                    fatal_error=True,
                )
            if not isinstance(response, AIMessage):
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor 模型未返回 AIMessage",
                    tool_calls=new_records,
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
                    fatal_error=True,
                )
            if call_id in seen_call_ids:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=f"Executor Tool Call ID 重复: {call_id}",
                    tool_calls=new_records,
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
                )

            try:
                self._registry.get(action_name)
            except UnknownToolError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    fatal_error=True,
                )

            try:
                result = self._registry.invoke(action_name, arguments)
            except ToolArgumentsError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
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
            fatal_error=False,
        )

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
            fatal_error=fatal_error,
        )
```

### src/taskpilot/cli.py

```python
"""TaskPilot Phase 5 的诚实命令行入口。"""

import argparse
import json
from typing import Sequence
from uuid import uuid4

from taskpilot.graph import build_graph
from taskpilot.models import TaskStatus
from taskpilot.state import TaskState


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
        "status": TaskStatus.CREATED,
        "error": None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """运行一次 Phase 5 图，并输出当前结构化任务状态。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot Phase 5 graph.")
    parser.add_argument("task", help="Task description")
    args = parser.parse_args(argv)

    # 默认没有生产 Tool；执行层会明确失败，不会生成假的外部结果。
    result = build_graph().invoke(create_initial_state(args.task))
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    task_spec = result["task_spec"]
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
    for step in result["plan"]:
        print(f"  step_id={step.id}")
        print(f"    description={step.description}")
        print(f"    depends_on={json.dumps(step.depends_on)}")
        print(
            "    success_criteria="
            + json.dumps(step.success_criteria, ensure_ascii=False)
        )
        print(f"    status={step.status.value}")
    print(f"current_step_id={result['current_step_id']}")
    step_outcome = result["step_outcome"]
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
            [item.model_dump(mode="json") for item in result["step_results"]],
            ensure_ascii=False,
        )
    )
    verification = result["verification"]
    if verification is not None:
        print(f"task_verified={verification.completed}")
        print(f"verification_reason={verification.reason}")
    if result["error"]:
        print(f"error={result['error']}")
        return 1
    if step_outcome is not None and step_outcome.error:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

## 9. Verifier 完整源码

```python
"""Step 与 Task 两层完成语义的结构化 Verifier。"""

import json
from typing import Any, Protocol, Sequence

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.llm import create_chat_model
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    VerificationResult,
)


STEP_VERIFIER_SYSTEM_PROMPT = """你是 Step Verifier，只验证当前 PlanStep，不规划、不执行、不调用工具，也不修改 Task Goal 或 Constraints。
必须对当前 PlanStep 的每一项 success_criteria 分别判断，并使用该条件在原列表中的下标作为 criterion_index。
只能把提供的 StepExecutionOutcome、当前步骤 ToolCallRecords 和 dependency StepResults 当作证据；Executor 的完成声明本身不是通过依据。
不得虚构工具未返回的信息，不得补做任务，也不得使用未验证的其他步骤声明。
checks 必须完整覆盖每一个 success_criteria，criterion_index 不得重复或遗漏。
只有所有 check 的 satisfied 都为 true 时，verified 才能为 true；只要一项不满足，verified 必须为 false。
拒绝时 feedback 必须清楚说明缺少的证据或结果，供同一 Step 的下一次 Executor 尝试使用。
输出必须符合 StepVerificationResult schema。"""


TASK_VERIFIER_SYSTEM_PROMPT = """你是 Task Verifier，只验证整个 Task 是否完成，不规划、不执行、不调用工具，也不修改 Goal、Constraints 或 Completion Criteria。
所有 PlanStep 已完成并不自动表示 Task 成功；必须逐项检查 TaskSpec.completion_criteria。
只能使用提供的、已经验证的 StepResults 和 task_results，不得使用未验证的 Executor claim，也不得虚构缺失信息。
任一 Completion Criterion 不满足时，completed 必须为 false，并在 missing_requirements 中列出缺失要求。
completed 为 true 时 missing_requirements 必须为空；completed 为 false 时 reason 必须清楚说明未完成原因。
next_action 只能给出建议，不得实际重规划或执行。
输出必须符合 VerificationResult schema。"""


class TaskVerifierLike(Protocol):
    """真实 Verifier 与离线 Fake 共同遵循的最小接口。"""

    def verify_step(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        outcome: StepExecutionOutcome,
        tool_calls: Sequence[ToolCallRecord],
        dependency_results: Sequence[StepResult],
    ) -> StepVerificationResult:
        """验证当前步骤的完成声明。"""

        ...

    def verify_task(
        self,
        *,
        task_spec: TaskSpec,
        step_results: Sequence[StepResult],
        task_results: dict[str, Any],
    ) -> VerificationResult:
        """验证整个任务的完成条件。"""

        ...


def validate_step_verification(
    step: PlanStep,
    result: StepVerificationResult,
) -> None:
    """确定性校验 Step Verifier 输出，发现矛盾时直接拒绝。"""

    if result.step_id != step.id:
        raise ValueError(
            "StepVerificationResult.step_id 与当前步骤不一致: "
            f"expected={step.id}, actual={result.step_id}"
        )

    expected_count = len(step.success_criteria)
    if len(result.checks) != expected_count:
        raise ValueError(
            "StepVerificationResult.checks 数量与 success_criteria 不一致: "
            f"expected={expected_count}, actual={len(result.checks)}"
        )

    indexes = [check.criterion_index for check in result.checks]
    if len(indexes) != len(set(indexes)):
        raise ValueError("StepVerificationResult.criterion_index 必须唯一")

    expected_indexes = set(range(expected_count))
    actual_indexes = set(indexes)
    if actual_indexes != expected_indexes:
        raise ValueError(
            "StepVerificationResult.criterion_index 必须完整覆盖 "
            f"0..{expected_count - 1}; actual={sorted(actual_indexes)}"
        )

    all_satisfied = all(check.satisfied for check in result.checks)
    if result.verified != all_satisfied:
        raise ValueError(
            "StepVerificationResult.verified 必须且仅能在全部 checks "
            "satisfied 时为 true"
        )


def validate_task_verification(result: VerificationResult) -> None:
    """确定性校验 Task Verifier 输出的一致性。"""

    if result.completed and result.missing_requirements:
        raise ValueError(
            "VerificationResult.completed 为 true 时 "
            "missing_requirements 必须为空"
        )
    if not result.completed and not result.reason.strip():
        raise ValueError(
            "VerificationResult.completed 为 false 时 reason 不能为空"
        )


def incomplete_outcome_rejection(
    step: PlanStep,
    outcome: StepExecutionOutcome,
) -> StepVerificationResult:
    """不调用 LLM，直接拒绝 Executor 尚未完成的声明。"""

    detail = outcome.error or "Executor 未声明当前步骤完成"
    return StepVerificationResult(
        step_id=step.id,
        verified=False,
        checks=[
            CriterionCheck(
                criterion_index=index,
                satisfied=False,
                reason=f"尚无完成该条件的已验证声明：{detail}",
            )
            for index, _ in enumerate(step.success_criteria)
        ],
        feedback=f"Executor 尚未完成当前步骤：{detail}",
    )


class TaskVerifier:
    """使用 Chat Model Structured Output 执行 Step 与 Task 验证。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def verify_step(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        outcome: StepExecutionOutcome,
        tool_calls: Sequence[ToolCallRecord],
        dependency_results: Sequence[StepResult],
    ) -> StepVerificationResult:
        """使用结构化输出验证当前步骤。"""

        if not outcome.claimed_complete:
            result = incomplete_outcome_rejection(step, outcome)
            validate_step_verification(step, result)
            return result

        model = self._get_model().with_structured_output(StepVerificationResult)
        relevant_calls = [
            record.model_dump(mode="json")
            for record in tool_calls
            if record.step_id == step.id
        ]
        relevant_dependencies = [
            result.model_dump(mode="json")
            for result in dependency_results
            if result.step_id in step.depends_on
        ]
        context = {
            "task": {
                "goal": task_spec.goal,
                "constraints": task_spec.constraints,
            },
            "current_step": step.model_dump(mode="json"),
            "step_outcome": outcome.model_dump(mode="json"),
            "current_step_tool_calls": relevant_calls,
            "dependency_results": relevant_dependencies,
        }
        raw_result = model.invoke(
            [
                ("system", STEP_VERIFIER_SYSTEM_PROMPT),
                ("human", json.dumps(context, ensure_ascii=False, default=str)),
            ]
        )
        result = (
            raw_result
            if isinstance(raw_result, StepVerificationResult)
            else StepVerificationResult.model_validate(raw_result)
        )
        validate_step_verification(step, result)
        return result

    def verify_task(
        self,
        *,
        task_spec: TaskSpec,
        step_results: Sequence[StepResult],
        task_results: dict[str, Any],
    ) -> VerificationResult:
        """使用结构化输出验证整个任务。"""

        model = self._get_model().with_structured_output(VerificationResult)
        context = {
            "task": task_spec.model_dump(mode="json"),
            "verified_step_results": [
                result.model_dump(mode="json") for result in step_results
            ],
            "task_results": task_results,
        }
        raw_result = model.invoke(
            [
                ("system", TASK_VERIFIER_SYSTEM_PROMPT),
                ("human", json.dumps(context, ensure_ascii=False, default=str)),
            ]
        )
        result = (
            raw_result
            if isinstance(raw_result, VerificationResult)
            else VerificationResult.model_validate(raw_result)
        )
        validate_task_verification(result)
        return result

    def _get_model(self) -> BaseChatModel:
        """延迟创建模型，保证导入和离线 Fake 测试无需 API Key。"""

        if self._model is None:
            self._model = create_chat_model()
        return self._model
```

## 10. Step Verifier Prompt 全文

```text
你是 Step Verifier，只验证当前 PlanStep，不规划、不执行、不调用工具，也不修改 Task Goal 或 Constraints。
必须对当前 PlanStep 的每一项 success_criteria 分别判断，并使用该条件在原列表中的下标作为 criterion_index。
只能把提供的 StepExecutionOutcome、当前步骤 ToolCallRecords 和 dependency StepResults 当作证据；Executor 的完成声明本身不是通过依据。
不得虚构工具未返回的信息，不得补做任务，也不得使用未验证的其他步骤声明。
checks 必须完整覆盖每一个 success_criteria，criterion_index 不得重复或遗漏。
只有所有 check 的 satisfied 都为 true 时，verified 才能为 true；只要一项不满足，verified 必须为 false。
拒绝时 feedback 必须清楚说明缺少的证据或结果，供同一 Step 的下一次 Executor 尝试使用。
输出必须符合 StepVerificationResult schema。
```

约束点：只验证当前 Step；逐项检查；不调用工具、不规划、不执行；只能使用 Outcome、当前步骤 Tool Records 和 dependency StepResults；不能把 claim 本身当证据；不能虚构；拒绝时必须反馈。

## 11. Task Verifier Prompt 全文

```text
你是 Task Verifier，只验证整个 Task 是否完成，不规划、不执行、不调用工具，也不修改 Goal、Constraints 或 Completion Criteria。
所有 PlanStep 已完成并不自动表示 Task 成功；必须逐项检查 TaskSpec.completion_criteria。
只能使用提供的、已经验证的 StepResults 和 task_results，不得使用未验证的 Executor claim，也不得虚构缺失信息。
任一 Completion Criterion 不满足时，completed 必须为 false，并在 missing_requirements 中列出缺失要求。
completed 为 true 时 missing_requirements 必须为空；completed 为 false 时 reason 必须清楚说明未完成原因。
next_action 只能给出建议，不得实际重规划或执行。
输出必须符合 VerificationResult schema。
```

约束点：所有步骤完成不等于任务完成；逐项检查 Task completion criteria；只使用 verified StepResults 和 task_results；不使用未验证 claim；不规划、不执行、不补数据。

## 12. Deterministic Verification Validation

Step validation 实际检查：

1. `result.step_id == current_step.id`；
2. checks 数量等于 success_criteria 数量；
3. criterion_index 唯一；
4. criterion_index 精确覆盖 `0..len(success_criteria)-1`；
5. `verified=True` 当且仅当所有 checks.satisfied 为 true；
6. 不一致时抛清晰 `ValueError`，不静默修复。

Task validation 实际检查：

1. `completed=True` 时 missing_requirements 必须为空；
2. `completed=False` 时 reason 去除空白后必须非空；
3. 不一致时抛异常，Graph 记录 Task Verifier failure。

当前没有实现基于证据内容的确定性语义推理；语义判断由 Structured Output Verifier 完成，代码只校验结构和逻辑一致性。

## 13. Retry / Feedback Loop

`retry_count` 表示当前 Step 被 Verifier 拒绝的次数。每次 rejection 加 1：

- 新值仍小于 `max_step_attempts`：Step 保持 running，Graph 回到 execute_step；
- 下一次 Executor 只收到同一 Step 的 rejection feedback；
- 新值达到上限：Step failed、Task failed、Graph 结束；
- 默认 `max_step_attempts=3`，且 build_graph 拒绝小于 1 的配置。

例如上限为 3 时，最多执行三轮 Executor claim/Verification。没有无限循环，也没有 Replanning。

当 `outcome.claimed_complete=False` 时，Graph 通过 `incomplete_outcome_rejection` 确定性生成 rejection，不调用 Step Verifier LLM；outcome.error 会进入 checks.reason 和 feedback。

## 14. advance_step 实现

```python
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

    if next_step_id is None and not all(
        step.status == PlanStepStatus.COMPLETED for step in updated_plan
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
```

接受路径才会：

1. 把当前 Step 改为 completed；
2. 从 verified outcome 创建 StepResult；
3. upsert 结果，避免重复 step_id；
4. 清除临时 step_outcome 和 step_verification；
5. 依赖感知地选择下一 Step，或进入 Task Verification。

## 15. Dependency-Aware Next-Step Selection

```python
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
```

选择严格按 Plan 输出顺序进行，仅返回第一条 `status=pending` 且所有 depends_on 均为 completed 的步骤。不会使用 `current_step_id + 1`，也没有并行调度。若仍有 pending Step 但没有任何一条依赖满足，抛出包含 unmet_dependencies 的清晰错误。

## 16. Task Verification Logic

```python
def verify_task(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
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
    return {
        "verification": result,
        "current_step_id": None,
        "status": TaskStatus.FAILED,
        "error": f"Task Verifier 拒绝任务完成: {result.reason}{next_hint}",
    }
```

只有 `VerificationResult.completed=True` 才写入 `TaskStatus.COMPLETED` 和 `current_step_id=None`。Task Verifier 拒绝时保存原始 `state.verification`，设置 Task failed，并把 reason 以及可选 next_action 写入 error；本阶段不会真的执行 `replan`。

## 17. Full Relevant Graph Source

### src/taskpilot/graph.py

```python
"""TaskPilot Phase 5 的 LangGraph 验证与步骤推进工作流。"""

from functools import partial
from typing import Literal, Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.executor import ExecutorRunResult, TaskExecutor, TaskExecutorLike
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    TaskStatus,
)
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.state import TaskState, TaskStateUpdate
from taskpilot.tools.registry import ToolRegistry
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
        current_step_id = find_next_executable_step(task_plan.steps)
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
            "plan": task_plan.steps,
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": "Task Planner 返回的计划没有可执行步骤",
        }
    return {
        "plan": task_plan.steps,
        "current_step_id": current_step_id,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def execute_step(
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
        execution = executor.execute(
            task_spec=task_spec,
            step=running_step,
            task_results=state["task_results"],
            tool_calls=prior_step_tool_calls,
            step_results=state["step_results"],
            verification_feedback=state["step_verification"],
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


def verify_step(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
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
    update: TaskStateUpdate = {
        "plan": _replace_step(state["plan"], step_index, rejected_step),
        "step_verification": result,
        "status": TaskStatus.FAILED if reached_limit else TaskStatus.RUNNING,
        "error": None,
    }
    if reached_limit:
        update["error"] = (
            f"Step {step.id} 连续被 Verifier 拒绝，已达到 "
            f"max_step_attempts={max_step_attempts}: "
            f"{result.feedback or '未提供反馈'}"
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

    if next_step_id is None and not all(
        step.status == PlanStepStatus.COMPLETED for step in updated_plan
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
    return {
        "verification": result,
        "current_step_id": None,
        "status": TaskStatus.FAILED,
        "error": f"Task Verifier 拒绝任务完成: {result.reason}{next_hint}",
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

    return {
        "plan": updated_plan,
        "tool_calls": state["tool_calls"] + execution.tool_calls,
        "step_outcome": execution.outcome,
        "status": (
            TaskStatus.FAILED if execution.fatal_error else TaskStatus.RUNNING
        ),
        "error": execution.outcome.error if execution.fatal_error else None,
    }


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


def _route_after_execute(state: TaskState) -> Literal["verify_step", "end"]:
    return "end" if state["status"] == TaskStatus.FAILED else "verify_step"


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


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
) -> CompiledStateGraph:
    """构建并编译带两层验证和有限重试的 Phase 5 状态图。"""

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
        {"verify_step": "verify_step", "end": END},
    )
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
    return builder.compile()
```

## 18. Full Relevant Tests

以下为本阶段数据模型、Executor 上下文、Verifier 和 Graph 状态机的完整当前测试源码。

### tests/test_models.py

```python
"""TaskPilot 结构化模型测试。"""

from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    VerificationResult,
)


def test_task_spec_can_be_created() -> None:
    spec = TaskSpec(
        goal="Generate a CSV of matching gyms",
        constraints={"monthly_fee_max": 500},
        expected_output="CSV",
        completion_criteria=["Contains three gyms"],
    )

    assert spec.goal == "Generate a CSV of matching gyms"
    assert spec.constraints == {"monthly_fee_max": 500}
    assert spec.expected_output == "CSV"


def test_plan_step_defaults() -> None:
    step = PlanStep(id=1, description="Find candidate gyms")

    assert step.status is PlanStepStatus.PENDING
    assert step.retry_count == 0
    assert step.depends_on == []
    assert step.success_criteria == []


def test_mutable_defaults_are_not_shared() -> None:
    first_spec = TaskSpec(goal="First")
    second_spec = TaskSpec(goal="Second")
    first_step = PlanStep(id=1, description="First")
    second_step = PlanStep(id=2, description="Second")

    first_spec.constraints["region"] = "Hong Kong Island"
    first_spec.completion_criteria.append("Complete")
    first_step.depends_on.append(99)
    first_step.success_criteria.append("First step is complete")

    assert second_spec.constraints == {}
    assert second_spec.completion_criteria == []
    assert second_step.depends_on == []
    assert second_step.success_criteria == []


def test_verification_result_can_be_created() -> None:
    result = VerificationResult(
        completed=False,
        reason="One requirement remains",
        missing_requirements=["CSV output"],
        next_action="Generate the CSV",
    )

    assert result.completed is False
    assert result.missing_requirements == ["CSV output"]
    assert result.next_action == "Generate the CSV"


def test_step_execution_outcome_evidence_is_not_shared() -> None:
    first = StepExecutionOutcome(step_id=1)
    second = StepExecutionOutcome(step_id=2)

    first.evidence.append("offline evidence")
    first.output["candidate"] = "A"

    assert first.claimed_complete is False
    assert first.action_count == 0
    assert first.evidence == ["offline evidence"]
    assert second.evidence == []
    assert second.output == {}


def test_step_result_mutable_defaults_are_not_shared() -> None:
    first = StepResult(step_id=1, summary="First")
    second = StepResult(step_id=2, summary="Second")

    first.output["items"] = ["A"]
    first.evidence.append("Evidence A")

    assert second.output == {}
    assert second.evidence == []


def test_step_verification_result_can_be_created() -> None:
    result = StepVerificationResult(
        step_id=1,
        verified=True,
        checks=[
            CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="Evidence is present",
            )
        ],
    )

    assert result.step_id == 1
    assert result.verified is True
    assert result.checks[0].criterion_index == 0
```

### tests/test_executor.py

```python
"""Phase 4 Executor Function Calling Runtime 的离线测试。"""

import json
from dataclasses import dataclass, field
from typing import Any, Sequence, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from taskpilot.executor import (
    EXECUTOR_SYSTEM_PROMPT,
    FINISH_STEP_NAME,
    ExecutorRunResult,
    TaskExecutor,
    build_executor_messages,
)
from taskpilot.graph import build_graph
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    CriterionCheck,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
    VerificationResult,
)
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@dataclass
class LookupTool:
    name: str = "lookup"
    description: str = "Return fixed offline candidates."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: dict(LOOKUP_SCHEMA)
    )

    def invoke(self, arguments: dict[str, Any]) -> Any:
        return {
            "query": arguments["query"],
            "items": ["candidate-a", "candidate-b"],
        }


@dataclass
class FailingTool:
    name: str = "lookup"
    description: str = "Fail in a controlled offline manner."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: dict(LOOKUP_SCHEMA)
    )

    def invoke(self, arguments: dict[str, Any]) -> Any:
        raise RuntimeError("controlled lookup failure")


class FakeFunctionCallingModel:
    """按顺序返回 AIMessage，并保留每轮完整消息快照。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tools: list[dict[str, Any]] = []
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def tool_call(
    name: str,
    arguments: dict[str, Any],
    call_id: str,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def finish_call(call_id: str = "finish-1") -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": "Current step has enough offline evidence.",
            "evidence": ["Two fixed candidates were returned."],
            "output": {"items": ["candidate-a", "candidate-b"]},
        },
        call_id,
    )


def make_task_spec() -> TaskSpec:
    return TaskSpec(
        goal="Find offline candidates",
        constraints={"count": 2},
        completion_criteria=["Return two candidates"],
    )


def make_step(step_id: int = 1) -> PlanStep:
    return PlanStep(
        id=step_id,
        description="Discover candidates that can be verified",
        success_criteria=["At least two candidates are available"],
    )


def make_registry(tool: LookupTool | FailingTool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return registry


def test_executor_runs_tool_then_finish_step_with_linked_tool_message() -> None:
    model = FakeFunctionCallingModel(
        [
            tool_call("lookup", {"query": "gym"}, "lookup-1"),
            finish_call("finish-2"),
        ]
    )
    step = make_step()
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=step,
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is True
    assert result.outcome.action_count == 2
    assert result.outcome.evidence == ["Two fixed candidates were returned."]
    assert result.outcome.output == {
        "items": ["candidate-a", "candidate-b"]
    }
    assert result.fatal_error is False
    assert len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record.call_id == "lookup-1"
    assert record.step_id == step.id
    assert record.status is ToolCallStatus.SUCCESS
    assert record.result == {
        "query": "gym",
        "items": ["candidate-a", "candidate-b"],
    }
    assert step.status is PlanStepStatus.PENDING

    second_round_messages = model.invocations[1]
    assert isinstance(second_round_messages[-2], AIMessage)
    assert isinstance(second_round_messages[-1], ToolMessage)
    assert second_round_messages[-2].tool_calls[0]["id"] == "lookup-1"
    assert second_round_messages[-1].tool_call_id == "lookup-1"
    assert second_round_messages[-1].status == "success"

    schema_names = [schema["function"]["name"] for schema in model.bound_tools]
    assert schema_names == ["lookup", FINISH_STEP_NAME]


def test_tool_failure_becomes_observation_and_loop_can_continue() -> None:
    model = FakeFunctionCallingModel(
        [
            tool_call("lookup", {"query": "gym"}, "failed-1"),
            tool_call("backup_lookup", {"query": "gym"}, "backup-2"),
            finish_call("finish-after-error"),
        ]
    )
    registry = make_registry(FailingTool())
    registry.register(LookupTool(name="backup_lookup"))
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is True
    assert result.outcome.action_count == 3
    assert result.fatal_error is False
    assert result.tool_calls[0].status is ToolCallStatus.FAILED
    assert result.tool_calls[1].status is ToolCallStatus.SUCCESS
    assert "controlled lookup failure" in result.tool_calls[0].error
    error_message = model.invocations[1][-1]
    assert isinstance(error_message, ToolMessage)
    assert error_message.tool_call_id == "failed-1"
    assert error_message.status == "error"
    assert "controlled lookup failure" in str(error_message.content)


def test_tool_error_does_not_make_graph_task_failed() -> None:
    model = FakeFunctionCallingModel(
        [
            tool_call("lookup", {"query": "gym"}, "graph-failed-1"),
            tool_call("backup_lookup", {"query": "gym"}, "graph-backup-2"),
            finish_call("graph-finish-3"),
        ]
    )
    registry = make_registry(FailingTool())
    registry.register(LookupTool(name="backup_lookup"))
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
    )

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
    ).invoke(make_initial_state())

    assert result["status"] is TaskStatus.COMPLETED
    assert result["tool_calls"][0].status is ToolCallStatus.FAILED
    assert result["tool_calls"][1].status is ToolCallStatus.SUCCESS
    assert result["step_outcome"] is None
    assert result["step_results"][0].summary == (
        "Current step has enough offline evidence."
    )
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["error"] is None


def test_action_limit_returns_incomplete_outcome() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("lookup", {"query": "gym"}, "limit-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
        max_actions_per_step=1,
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.outcome.action_count == 1
    assert result.outcome.error == "Executor action limit reached: 1"
    assert len(result.tool_calls) == 1
    assert result.fatal_error is False


def test_multiple_tool_calls_are_rejected() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"query": "one"},
                "id": "multi-1",
                "type": "tool_call",
            },
            {
                "name": "lookup",
                "args": {"query": "two"},
                "id": "multi-2",
                "type": "tool_call",
            },
        ],
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert "实际数量: 2" in result.outcome.error
    assert result.tool_calls == []


def test_unknown_tool_call_returns_clear_error() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("missing", {"query": "gym"}, "unknown-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert result.outcome.error == "未注册的工具: missing"
    assert result.tool_calls == []


def test_invalid_tool_arguments_return_clear_error() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("lookup", {}, "invalid-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert "参数不符合 input_schema" in result.outcome.error
    assert result.tool_calls == []


def test_missing_action_is_not_treated_as_completion() -> None:
    model = FakeFunctionCallingModel([AIMessage(content="I am done")])
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "实际数量: 0" in result.outcome.error


def test_finish_step_requires_nonempty_evidence() -> None:
    response = tool_call(
        FINISH_STEP_NAME,
        {"summary": "Done", "evidence": [], "output": {}},
        "empty-evidence-1",
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "finish_step 参数无效" in result.outcome.error


def test_finish_step_requires_structured_output_object() -> None:
    response = tool_call(
        FINISH_STEP_NAME,
        {"summary": "Done", "evidence": ["Evidence"]},
        "missing-output-1",
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "output" in result.outcome.error


def test_empty_registry_reports_execution_unavailable_without_model_call() -> None:
    model = FakeFunctionCallingModel([])
    executor = TaskExecutor(
        registry=make_registry(),
        model=cast(BaseChatModel, model),
    )

    result = executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.outcome.action_count == 0
    assert result.outcome.error == "Executor 当前没有注册任何外部工具"
    assert result.fatal_error is True
    assert model.invocations == []


def test_context_contains_only_relevant_structured_state() -> None:
    messages = build_executor_messages(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={"selected": ["candidate-a"]},
        tool_calls=[
            ToolCallRecord(
                call_id="current-1",
                step_id=1,
                tool_name="lookup",
                arguments={"query": "gym"},
                status=ToolCallStatus.SUCCESS,
                result={"items": ["candidate-a"]},
            ),
            ToolCallRecord(
                call_id="future-1",
                step_id=2,
                tool_name="lookup",
                arguments={"query": "other"},
                status=ToolCallStatus.SUCCESS,
                result={"items": ["should-not-appear"]},
            ),
        ],
    )

    assert EXECUTOR_SYSTEM_PROMPT in str(messages[0].content)
    assert isinstance(messages[1], HumanMessage)
    context = json.loads(str(messages[1].content))
    assert context["task_goal"] == "Find offline candidates"
    assert context["current_step"]["id"] == 1
    assert context["current_step_observations"][0]["call_id"] == "current-1"
    assert len(context["current_step_observations"]) == 1
    assert "plan" not in context
    assert context["verification_feedback"] is None


def test_context_filters_dependency_results_and_feedback_by_current_step() -> None:
    step = PlanStep(
        id=2,
        description="Organize candidates",
        depends_on=[1],
        success_criteria=["Candidates are organized"],
    )
    matching_feedback = StepVerificationResult(
        step_id=2,
        verified=False,
        checks=[
            CriterionCheck(
                criterion_index=0,
                satisfied=False,
                reason="Missing names",
            )
        ],
        feedback="Add candidate names",
    )
    messages = build_executor_messages(
        task_spec=make_task_spec(),
        step=step,
        task_results={},
        tool_calls=[],
        step_results=[
            StepResult(step_id=1, summary="Dependency", output={"items": ["A"]}),
            StepResult(step_id=99, summary="Unrelated", output={"secret": True}),
        ],
        verification_feedback=matching_feedback,
    )

    context = json.loads(str(messages[1].content))
    assert [item["step_id"] for item in context["dependency_results"]] == [1]
    assert context["verification_feedback"]["feedback"] == "Add candidate names"
    assert "Unrelated" not in str(context)


def test_context_excludes_feedback_for_other_step() -> None:
    messages = build_executor_messages(
        task_spec=make_task_spec(),
        step=make_step(2),
        task_results={},
        tool_calls=[],
        verification_feedback=StepVerificationResult(
            step_id=1,
            verified=False,
            checks=[],
            feedback="Old feedback",
        ),
    )

    context = json.loads(str(messages[1].content))
    assert context["verification_feedback"] is None


def test_executor_prompt_preserves_phase_4_boundaries() -> None:
    assert "只执行当前 PlanStep" in EXECUTOR_SYSTEM_PROMPT
    assert "不执行未来步骤" in EXECUTOR_SYSTEM_PROMPT
    assert "不得虚构外部结果" in EXECUTOR_SYSTEM_PROMPT
    assert "每轮必须且最多选择一个动作" in EXECUTOR_SYSTEM_PROMPT
    assert "单次 Tool 失败不等于 Step 或 Task 失败" in EXECUTOR_SYSTEM_PROMPT
    assert "success_criteria 已有充分 evidence" in EXECUTOR_SYSTEM_PROMPT
    assert "未来 Verifier 判断" in EXECUTOR_SYSTEM_PROMPT
    assert "不要直接把 PlanStep 或整个 Task 声称为 completed" in (
        EXECUTOR_SYSTEM_PROMPT
    )


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return make_task_spec()


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                make_step(1),
            ]
        )


class FakeExecutor:
    def __init__(self) -> None:
        self.executed_step_ids: list[int] = []

    def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult],
        verification_feedback: StepVerificationResult | None,
    ) -> ExecutorRunResult:
        self.executed_step_ids.append(step.id)
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step.id,
                claimed_complete=True,
                summary="Fake executor claim",
                evidence=["Fake evidence"],
                output={"items": ["candidate-a"]},
                action_count=2,
            ),
            tool_calls=[
                ToolCallRecord(
                    call_id="graph-call-1",
                    step_id=step.id,
                    tool_name="lookup",
                    arguments={"query": "gym"},
                    status=ToolCallStatus.SUCCESS,
                    result={"items": ["candidate-a"]},
                )
            ],
        )


class FakeVerifier:
    def verify_step(self, *, task_spec, step, outcome, tool_calls, dependency_results):
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=index,
                    satisfied=True,
                    reason="Fake evidence accepted",
                )
                for index, _ in enumerate(step.success_criteria)
            ],
        )

    def verify_task(self, *, task_spec, step_results, task_results):
        return VerificationResult(completed=True, reason="Fake task accepted")


def make_initial_state() -> TaskState:
    return {
        "task_id": "phase-4-graph",
        "user_input": "Find offline candidates",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def test_graph_verifies_and_completes_single_step() -> None:
    executor = FakeExecutor()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
    )

    result = graph.invoke(make_initial_state())

    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("plan_task", "execute_step") in edges
    assert ("execute_step", "__end__") in edges
    assert executor.executed_step_ids == [1]
    assert result["status"] is TaskStatus.COMPLETED
    assert result["current_step_id"] is None
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["step_outcome"] is None
    assert result["step_results"][0].step_id == 1
    assert result["tool_calls"][0].call_id == "graph-call-1"
    assert result["error"] is None
```

### tests/test_verifier.py

```python
"""Phase 5 Step/Task Verifier 的确定性与离线测试。"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    VerificationResult,
)
from taskpilot.verifier import (
    STEP_VERIFIER_SYSTEM_PROMPT,
    TASK_VERIFIER_SYSTEM_PROMPT,
    TaskVerifier,
    validate_step_verification,
    validate_task_verification,
)


def make_step() -> PlanStep:
    return PlanStep(
        id=2,
        description="整理候选",
        depends_on=[1],
        success_criteria=["包含两个候选", "每个候选都有名称"],
    )


def make_valid_step_verification() -> StepVerificationResult:
    return StepVerificationResult(
        step_id=2,
        verified=True,
        checks=[
            CriterionCheck(criterion_index=0, satisfied=True, reason="有两个"),
            CriterionCheck(criterion_index=1, satisfied=True, reason="都有名称"),
        ],
    )


def test_valid_step_verification_passes() -> None:
    validate_step_verification(make_step(), make_valid_step_verification())


def test_step_verification_rejects_missing_criterion_index() -> None:
    result = make_valid_step_verification().model_copy(
        update={"checks": [make_valid_step_verification().checks[0]]}
    )

    with pytest.raises(ValueError, match="数量"):
        validate_step_verification(make_step(), result)


def test_step_verification_rejects_duplicate_indexes() -> None:
    result = make_valid_step_verification()
    result.checks[1] = result.checks[1].model_copy(
        update={"criterion_index": 0}
    )

    with pytest.raises(ValueError, match="必须唯一"):
        validate_step_verification(make_step(), result)


@pytest.mark.parametrize(
    ("verified", "checks"),
    [
        (
            True,
            [
                CriterionCheck(criterion_index=0, satisfied=True, reason="ok"),
                CriterionCheck(criterion_index=1, satisfied=False, reason="missing"),
            ],
        ),
        (
            False,
            [
                CriterionCheck(criterion_index=0, satisfied=True, reason="ok"),
                CriterionCheck(criterion_index=1, satisfied=True, reason="ok"),
            ],
        ),
    ],
)
def test_step_verification_rejects_verified_checks_mismatch(
    verified: bool,
    checks: list[CriterionCheck],
) -> None:
    result = StepVerificationResult(
        step_id=2,
        verified=verified,
        checks=checks,
    )

    with pytest.raises(ValueError, match="必须且仅能"):
        validate_step_verification(make_step(), result)


def test_step_verification_rejects_wrong_step_id() -> None:
    result = make_valid_step_verification().model_copy(update={"step_id": 99})

    with pytest.raises(ValueError, match="step_id"):
        validate_step_verification(make_step(), result)


def test_incomplete_outcome_skips_llm_and_is_rejected() -> None:
    model = Mock(spec=BaseChatModel)
    verifier = TaskVerifier(model=model)

    result = verifier.verify_step(
        task_spec=TaskSpec(goal="整理候选"),
        step=make_step(),
        outcome=StepExecutionOutcome(
            step_id=2,
            claimed_complete=False,
            error="action limit reached",
        ),
        tool_calls=[],
        dependency_results=[],
    )

    assert result.verified is False
    assert len(result.checks) == 2
    assert "action limit reached" in result.feedback
    model.with_structured_output.assert_not_called()


def test_real_step_verifier_uses_structured_output() -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = make_valid_step_verification()
    model = Mock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    result = TaskVerifier(model=model).verify_step(
        task_spec=TaskSpec(goal="整理候选"),
        step=make_step(),
        outcome=StepExecutionOutcome(
            step_id=2,
            claimed_complete=True,
            summary="完成",
            evidence=["两个候选"],
            output={"items": [{"name": "A"}, {"name": "B"}]},
        ),
        tool_calls=[],
        dependency_results=[StepResult(step_id=1, summary="发现候选")],
    )

    assert result.verified is True
    model.with_structured_output.assert_called_once_with(StepVerificationResult)
    structured_model.invoke.assert_called_once()


def test_task_verification_validation_accepts_consistent_results() -> None:
    validate_task_verification(
        VerificationResult(completed=True, reason="全部满足")
    )
    validate_task_verification(
        VerificationResult(
            completed=False,
            reason="缺少 CSV",
            missing_requirements=["CSV"],
        )
    )


def test_task_verification_rejects_completed_with_missing_requirements() -> None:
    result = VerificationResult(
        completed=True,
        reason="矛盾输出",
        missing_requirements=["仍缺 CSV"],
    )

    with pytest.raises(ValueError, match="必须为空"):
        validate_task_verification(result)


def test_task_verification_rejects_empty_failure_reason() -> None:
    result = VerificationResult(completed=False, reason="   ")

    with pytest.raises(ValueError, match="reason 不能为空"):
        validate_task_verification(result)


def test_verifier_prompts_preserve_phase_5_boundaries() -> None:
    assert "只验证当前 PlanStep" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "不规划、不执行、不调用工具" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "完成声明本身不是通过依据" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "不得虚构" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "criterion_index" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "feedback" in STEP_VERIFIER_SYSTEM_PROMPT
    assert "所有 PlanStep 已完成并不自动表示 Task 成功" in (
        TASK_VERIFIER_SYSTEM_PROMPT
    )
    assert "已经验证的 StepResults" in TASK_VERIFIER_SYSTEM_PROMPT
    assert "不得使用未验证的 Executor claim" in TASK_VERIFIER_SYSTEM_PROMPT
```

### tests/test_phase5_graph.py

```python
"""Phase 5 验证、重试、依赖交接与任务完成门控的离线测试。"""

from collections import defaultdict
from typing import Any, Sequence

import pytest

from taskpilot.executor import ExecutorRunResult
from taskpilot.graph import advance_step, build_graph, find_next_executable_step
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)
from taskpilot.state import TaskState


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            constraints={"count": 2},
            completion_criteria=["返回两个整理后的离线候选"],
        )


class TwoStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="发现离线候选",
                    success_criteria=["获得两个候选"],
                ),
                PlanStep(
                    id=2,
                    description="整理离线候选",
                    depends_on=[1],
                    success_criteria=["输出两个候选名称"],
                ),
            ]
        )


class OneStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="生成离线结果",
                    success_criteria=["结果存在"],
                )
            ]
        )


class RecordingExecutor:
    """返回固定 Claim，并保留 Graph 传入的跨步骤上下文。"""

    def __init__(self, *, fatal: bool = False) -> None:
        self.fatal = fatal
        self.call_count_by_step: dict[int, int] = defaultdict(int)
        self.received_step_results: list[tuple[int, list[StepResult]]] = []
        self.received_feedback: list[tuple[int, StepVerificationResult | None]] = []

    def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult],
        verification_feedback: StepVerificationResult | None,
    ) -> ExecutorRunResult:
        self.call_count_by_step[step.id] += 1
        self.received_step_results.append((step.id, list(step_results)))
        self.received_feedback.append((step.id, verification_feedback))
        if self.fatal:
            return ExecutorRunResult(
                outcome=StepExecutionOutcome(
                    step_id=step.id,
                    claimed_complete=False,
                    error="fatal offline executor error",
                ),
                fatal_error=True,
            )
        output = (
            {"candidates": [{"name": "A"}, {"name": "B"}]}
            if step.id == 1
            else {"names": ["A", "B"]}
        )
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step.id,
                claimed_complete=True,
                summary=f"Step {step.id} offline claim",
                evidence=[f"step-{step.id}-evidence"],
                output=output,
                action_count=1,
            )
        )


class SequencedVerifier:
    """按调用顺序接受或拒绝 Step，并返回固定 Task 结论。"""

    def __init__(
        self,
        step_decisions: Sequence[bool],
        task_result: VerificationResult | None = None,
    ) -> None:
        self.step_decisions = list(step_decisions)
        self.task_result = task_result or VerificationResult(
            completed=True,
            reason="离线任务条件全部满足",
        )
        self.step_call_count = 0
        self.task_call_count = 0
        self.dependency_results_seen: list[tuple[int, list[StepResult]]] = []

    def verify_step(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        outcome: StepExecutionOutcome,
        tool_calls: Sequence[ToolCallRecord],
        dependency_results: Sequence[StepResult],
    ) -> StepVerificationResult:
        decision = self.step_decisions[self.step_call_count]
        self.step_call_count += 1
        self.dependency_results_seen.append((step.id, list(dependency_results)))
        return StepVerificationResult(
            step_id=step.id,
            verified=decision,
            checks=[
                CriterionCheck(
                    criterion_index=index,
                    satisfied=decision,
                    reason="满足" if decision else "证据不足",
                )
                for index, _ in enumerate(step.success_criteria)
            ],
            feedback=None if decision else "请补充可核验的候选证据",
        )

    def verify_task(
        self,
        *,
        task_spec: TaskSpec,
        step_results: Sequence[StepResult],
        task_results: dict[str, Any],
    ) -> VerificationResult:
        self.task_call_count += 1
        return self.task_result


def make_initial_state() -> TaskState:
    return {
        "task_id": "phase-5-offline",
        "user_input": "获取两个离线候选并整理结果",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def test_multi_step_graph_hands_off_verified_result_and_completes() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([True, True])

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=TwoStepPlanner(),
        executor=executor,
        verifier=verifier,
    ).invoke(make_initial_state())

    assert [step.status for step in result["plan"]] == [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
    ]
    assert result["status"] is TaskStatus.COMPLETED
    assert result["current_step_id"] is None
    assert [item.step_id for item in result["step_results"]] == [1, 2]
    assert result["step_results"][0].output == {
        "candidates": [{"name": "A"}, {"name": "B"}]
    }
    step_2_input = next(
        items for step_id, items in executor.received_step_results if step_id == 2
    )
    assert [item.step_id for item in step_2_input] == [1]
    step_2_verifier_input = next(
        items for step_id, items in verifier.dependency_results_seen if step_id == 2
    )
    assert [item.step_id for item in step_2_verifier_input] == [1]
    assert verifier.task_call_count == 1
    assert result["verification"].completed is True


def test_rejection_retries_same_step_with_feedback_then_advances() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([False, True])

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=executor,
        verifier=verifier,
        max_step_attempts=3,
    ).invoke(make_initial_state())

    assert executor.call_count_by_step[1] == 2
    assert result["plan"][0].retry_count == 1
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert executor.received_feedback[0] == (1, None)
    retry_feedback = executor.received_feedback[1][1]
    assert retry_feedback is not None
    assert retry_feedback.verified is False
    assert retry_feedback.feedback == "请补充可核验的候选证据"
    assert result["status"] is TaskStatus.COMPLETED


def test_repeated_rejection_stops_at_retry_bound() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([False, False, False, True])

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=executor,
        verifier=verifier,
        max_step_attempts=3,
    ).invoke(make_initial_state())

    assert executor.call_count_by_step[1] == 3
    assert verifier.step_call_count == 3
    assert verifier.task_call_count == 0
    assert result["plan"][0].retry_count == 3
    assert result["plan"][0].status is PlanStepStatus.FAILED
    assert result["status"] is TaskStatus.FAILED
    assert "max_step_attempts=3" in result["error"]


def test_task_verifier_rejection_fails_task_after_steps_complete() -> None:
    verifier = SequencedVerifier(
        [True],
        task_result=VerificationResult(
            completed=False,
            reason="最终格式仍缺失",
            missing_requirements=["CSV 输出"],
            next_action="replan",
        ),
    )

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(),
        verifier=verifier,
    ).invoke(make_initial_state())

    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["verification"].completed is False
    assert result["status"] is TaskStatus.FAILED
    assert "next_action=replan" in result["error"]


def test_inconsistent_task_verification_fails_validation() -> None:
    verifier = SequencedVerifier(
        [True],
        task_result=VerificationResult(
            completed=True,
            reason="矛盾",
            missing_requirements=["仍缺内容"],
        ),
    )

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(),
        verifier=verifier,
    ).invoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert "missing_requirements 必须为空" in result["error"]


def test_fatal_executor_error_bypasses_step_and_task_verifier() -> None:
    verifier = SequencedVerifier([True])

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(fatal=True),
        verifier=verifier,
    ).invoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert verifier.step_call_count == 0
    assert verifier.task_call_count == 0
    assert result["error"] == "fatal offline executor error"


def test_next_step_selection_is_dependency_aware_and_plan_ordered() -> None:
    plan = [
        PlanStep(
            id=10,
            description="Completed",
            status=PlanStepStatus.COMPLETED,
            success_criteria=["done"],
        ),
        PlanStep(
            id=30,
            description="First ready in plan order",
            depends_on=[10],
            success_criteria=["done"],
        ),
        PlanStep(
            id=20,
            description="Second ready in plan order",
            depends_on=[10],
            success_criteria=["done"],
        ),
    ]

    assert find_next_executable_step(plan) == 30


def test_blocked_pending_plan_returns_clear_error() -> None:
    plan = [
        PlanStep(
            id=2,
            description="Blocked",
            depends_on=[1],
            success_criteria=["done"],
        )
    ]

    with pytest.raises(ValueError, match="Plan 依赖阻塞"):
        find_next_executable_step(plan)


def test_advance_step_replaces_existing_result_without_duplicate() -> None:
    step = PlanStep(
        id=1,
        description="One",
        status=PlanStepStatus.RUNNING,
        success_criteria=["done"],
    )
    state = make_initial_state()
    state.update(
        {
            "task_spec": TaskSpec(goal="One"),
            "plan": [step],
            "current_step_id": 1,
            "step_outcome": StepExecutionOutcome(
                step_id=1,
                claimed_complete=True,
                summary="new summary",
                evidence=["new evidence"],
                output={"value": "new"},
            ),
            "step_results": [
                StepResult(
                    step_id=1,
                    summary="old summary",
                    output={"value": "old"},
                )
            ],
            "step_verification": StepVerificationResult(
                step_id=1,
                verified=True,
                checks=[
                    CriterionCheck(
                        criterion_index=0,
                        satisfied=True,
                        reason="verified",
                    )
                ],
            ),
            "status": TaskStatus.RUNNING,
        }
    )

    update = advance_step(state)

    assert update["plan"][0].status is PlanStepStatus.COMPLETED
    assert len(update["step_results"]) == 1
    assert update["step_results"][0].summary == "new summary"
    assert update["step_results"][0].output == {"value": "new"}
```

## 19. Pytest Actual Result

实际命令：

```powershell
conda run -n ai python -m pytest -q
```

实际输出：

```text
.......................................................................  [100%]
71 passed in 0.81s
```

结果：71 passed，0 failed。所有测试使用 Fake/Mock，无网络、LLM API、Browser 或外部服务依赖。

## 20. Multi-Step Offline Smoke Test

执行的是 `tests/test_phase5_graph.py` 中同一组 Fake Analyzer、TwoStepPlanner、RecordingExecutor、SequencedVerifier，通过真实 compiled graph.invoke 运行。

实际命令：

```powershell
D:\Anaconda\envs\ai\python.exe -c 'import json,sys; sys.stdout.reconfigure(encoding="utf-8"); sys.path[:0]=["src","tests"]; from test_phase5_graph import FakeAnalyzer,TwoStepPlanner,RecordingExecutor,SequencedVerifier,make_initial_state; from taskpilot.graph import build_graph; e=RecordingExecutor(); v=SequencedVerifier([True,True]); r=build_graph(analyzer=FakeAnalyzer(),planner=TwoStepPlanner(),executor=e,verifier=v).invoke(make_initial_state()); d={"input":r["user_input"],"plan":[{"id":s.id,"status":s.status.value,"retry_count":s.retry_count} for s in r["plan"]],"step_results":[x.model_dump(mode="json") for x in r["step_results"]],"step_2_dependency_results":[x.model_dump(mode="json") for sid,items in e.received_step_results if sid==2 for x in items],"current_step_id":r["current_step_id"],"task_verification":r["verification"].model_dump(mode="json"),"status":r["status"].value,"error":r["error"]}; print(json.dumps(d,ensure_ascii=False,indent=2))'
```

实际输出：

```json
{
  "input": "获取两个离线候选并整理结果",
  "plan": [
    {
      "id": 1,
      "status": "completed",
      "retry_count": 0
    },
    {
      "id": 2,
      "status": "completed",
      "retry_count": 0
    }
  ],
  "step_results": [
    {
      "step_id": 1,
      "summary": "Step 1 offline claim",
      "output": {
        "candidates": [
          {
            "name": "A"
          },
          {
            "name": "B"
          }
        ]
      },
      "evidence": [
        "step-1-evidence"
      ]
    },
    {
      "step_id": 2,
      "summary": "Step 2 offline claim",
      "output": {
        "names": [
          "A",
          "B"
        ]
      },
      "evidence": [
        "step-2-evidence"
      ]
    }
  ],
  "step_2_dependency_results": [
    {
      "step_id": 1,
      "summary": "Step 1 offline claim",
      "output": {
        "candidates": [
          {
            "name": "A"
          },
          {
            "name": "B"
          }
        ]
      },
      "evidence": [
        "step-1-evidence"
      ]
    }
  ],
  "current_step_id": null,
  "task_verification": {
    "completed": true,
    "reason": "离线任务条件全部满足",
    "missing_requirements": [],
    "next_action": null
  },
  "status": "completed",
  "error": null
}
```

验收结论：Step 1、Step 2 都经过各自 Verifier 后完成；StepResult 1 原样进入 Step 2 的依赖输入；最终 current_step_id 为 null；Task Verifier 接受后 Task 才为 completed。

## 21. Rejection → Retry → Accept Smoke Test

执行的是同一离线 Graph，Step Verifier 决策序列为 `[False, True]`，`max_step_attempts=3`。

实际命令：

```powershell
D:\Anaconda\envs\ai\python.exe -c 'import json,sys; sys.stdout.reconfigure(encoding="utf-8"); sys.path[:0]=["src","tests"]; from test_phase5_graph import FakeAnalyzer,OneStepPlanner,RecordingExecutor,SequencedVerifier,make_initial_state; from taskpilot.graph import build_graph; e=RecordingExecutor(); v=SequencedVerifier([False,True]); r=build_graph(analyzer=FakeAnalyzer(),planner=OneStepPlanner(),executor=e,verifier=v,max_step_attempts=3).invoke(make_initial_state()); fb=e.received_feedback[1][1]; d={"flow":["executor claim 1","verifier rejected","feedback injected","executor claim 2","verifier accepted"],"retry_count":r["plan"][0].retry_count,"injected_feedback":fb.feedback,"executor_call_count":e.call_count_by_step[1],"final_step_status":r["plan"][0].status.value,"task_status":r["status"].value,"error":r["error"]}; print(json.dumps(d,ensure_ascii=False,indent=2))'
```

实际输出：

```json
{
  "flow": [
    "executor claim 1",
    "verifier rejected",
    "feedback injected",
    "executor claim 2",
    "verifier accepted"
  ],
  "retry_count": 1,
  "injected_feedback": "请补充可核验的候选证据",
  "executor_call_count": 2,
  "final_step_status": "completed",
  "task_status": "completed",
  "error": null
}
```

第一次拒绝后 current Step 没有推进，retry_count 增加；第二次 Executor 收到具体 feedback，随后接受并正常完成。Executor 实际调用两次。

## 22. Boundary Checks

### Q1. Tool success 是否可以直接把 Step 设为 completed？

No。Tool success 只产生 `ToolCallRecord(status=success)`；状态转换不发生在 ToolRegistry 或 Executor。

### Q2. Executor 的 finish_step 是否可以直接把 Step 设为 completed？

No。`finish_step` 只生成 `StepExecutionOutcome(claimed_complete=True)`，仍是不可信 claim。

### Q3. 谁真正决定 Step completed？

Step Verifier。只有合法的 `StepVerificationResult(verified=True)` 被 Graph 接受后，`advance_step` 才设置 completed 并生成 StepResult。

### Q4. 所有 Step completed 是否自动 Task completed？

No。advance_step 在全部 Step completed 时路由到 `verify_task`，而不是直接设置 Task completed。

### Q5. 谁决定 Task completed？

Task-level Verifier。只有经确定性校验的 `VerificationResult.completed=True` 才能设置 TaskStatus.COMPLETED。

### Q6. Step 2 如何获得 Step 1 的结果？

Step 1 verified 后沉淀 `StepResult(step_id=1, output=...)`。Step 2 的 `depends_on=[1]`，因此 Executor context 的 dependency_results 只包含 StepResult 1。第 20 节真实输出展示了该对象。

### Q7. Verifier reject 后 Executor 是否知道原因？

Yes。同 Step rejection 保存在 `state.step_verification`；retry 路由再次进入 execute_step 时以 `verification_feedback` 传入，并由 build_executor_messages 仅在 step_id 匹配时注入。第 21 节展示实际反馈。

### Q8. Repeated rejection 是否可能无限循环？

No。`max_step_attempts` 默认 3；每次 rejection 增加 retry_count，达到上限立即 Step failed、Task failed、END。

### Q9. StepResult 是否来自未验证 Executor claim？

No。只有 `verified=True` 路由能进入 advance_step；未验证或被拒绝的 outcome 不会生成 StepResult。

## 23. Known Limitations

- Verifier 尚未执行任何真实 Tool 来主动核验证据，只能审查提供的结构化上下文。
- Planner 尚未动态重规划；重试耗尽或 Task Verification 拒绝后直接失败。
- depends_on 只用于串行、按计划顺序调度，没有并行执行。
- retry_count 只统计 Verifier rejection，不统计 fatal Executor/Verifier 调用异常。
- task_results 尚未由正式聚合器填充；当前主要跨步骤通道是 verified step_results。
- 没有 MCP、Browser/Playwright、真实 Search、production Filesystem Tool。
- 没有 HITL、Checkpoint/Recovery、持久 Trace、Vision fallback。
- Benchmark 仍只有指标清单，没有执行器或实验数据。
- 默认 CLI 没有注册生产 Tool，并且真实 Analyzer/Planner/Verifier 需要显式 LLM 配置。

## 24. Files Changed

Phase 5 新建：

- `src/taskpilot/verifier.py`：两层 Verifier、Prompt、结构化输出和确定性校验。
- `tests/test_verifier.py`：Verifier 模型、Prompt 与 validation 离线测试。
- `tests/test_phase5_graph.py`：多步交接、重试、上限、依赖、Task gate 与 fatal bypass 测试。
- `docs/review/phase_05.md`：本验收文档。

Phase 5 修改：

- `src/taskpilot/models.py`：新增 StepResult、CriterionCheck、StepVerificationResult，并扩展 outcome.output。
- `src/taskpilot/state.py`：新增 step_results 和 step_verification。
- `src/taskpilot/executor.py`：finish_step output、dependency results 与 feedback context。
- `src/taskpilot/graph.py`：Verifier、retry、advance、dependency selection、Task verification 条件路由。
- `src/taskpilot/cli.py`：初始化和展示 Phase 5 状态字段。
- `tests/test_models.py`、`tests/test_executor.py`：模型默认值、finish schema、上下文过滤测试。
- `tests/test_analyzer.py`、`tests/test_planner.py`、`tests/test_graph_smoke.py`：适配 Phase 5 Executor/Verifier 接口和最终状态语义。
- `README.md`：更新实际 Phase 5 能力、三层成功语义和未实现边界。

未删除或覆盖 Phase 5 范围外的用户修改。

## 25. git status --short

命令：

```powershell
git status --short
```

实际输出：

```text
warning: unable to access 'C:\Users\zc115/.config/git/ignore': Permission denied
warning: unable to access 'C:\Users\zc115/.config/git/ignore': Permission denied
 M .gitignore
 M README.md
 M docs/review/phase_03.md
 M pyproject.toml
 M src/taskpilot/cli.py
 M src/taskpilot/graph.py
 M src/taskpilot/models.py
 M src/taskpilot/state.py
 M tests/test_analyzer.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
 M tests/test_planner.py
?? docs/review/phase_04.md
?? docs/review/phase_05.md
?? src/taskpilot/executor.py
?? src/taskpilot/tools/
?? src/taskpilot/verifier.py
?? tests/test_executor.py
?? tests/test_phase5_graph.py
?? tests/test_tools.py
?? tests/test_verifier.py
```

其中 Phase 3/4 的未提交文件与修改继续如实保留；本阶段没有清理、reset 或覆盖它们。
