# TaskPilot Phase 4 Review

本文档记录 Phase 4（Executor + Tool Runtime）完成时的真实实现，用于人工架构与代码验收。内容依据当前工作区源码、完全离线测试和实际 `graph.invoke` 输出生成。

## 1. Phase Goal

本阶段目标是实现一个最小但真实的单步骤 Executor Runtime：模型通过原生 Function Calling 在外部 Tool 与内部 `finish_step` 之间选择一个动作，Runtime 执行 Tool、把 Observation 作为正确的 `ToolMessage` 回写，并在显式 `finish_step` 或动作上限处停止。

实际完成：

- 与 LangChain `BaseTool` 解耦、以 JSON Schema 为核心的 `Tool` Protocol。
- `ToolRegistry` 注册、查询、Function schema 转换、参数校验和调用。
- 原生 `model.bind_tools(...)` Function Calling loop，没有正则或手工 Action 文本解析。
- 外部 Tool 的成功/失败 `ToolCallRecord`。
- `AIMessage.tool_calls[].id` 与 `ToolMessage.tool_call_id` 的严格关联。
- 内部 `finish_step` schema 和 `StepExecutionOutcome`。
- 可配置 `max_actions_per_step`，默认 5。
- 每轮严格一个动作；零个或多个 Tool Calls 均明确失败。
- `execute_step` 节点，只处理 `current_step_id` 对应的一个 Step。
- 完全离线的 Fake Tool、Fake Function Calling Model 和 Graph 测试。
- 修复 Phase 3 Fake Plan 中先按价格筛选、后获取价格的语义倒置。

明确没有实现：Verifier、Step advancement、Dynamic Replanning、MCP、Playwright、Browser、真实 Search、真实 Filesystem Tool、HITL、Checkpoint / Recovery、SQLite、FastAPI、Vision 和 Benchmark execution。

## 2. Final Directory Tree

以下目录树排除 `__pycache__` 和 `.pytest_cache`：

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
│       └── phase_04.md
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
│       └── tools/
│           ├── __init__.py
│           ├── base.py
│           └── registry.py
└── tests/
    ├── test_analyzer.py
    ├── test_executor.py
    ├── test_graph_smoke.py
    ├── test_models.py
    ├── test_planner.py
    └── test_tools.py
```

## 3. Architecture Flow

当前实际 Graph：

```text
START
→ initialize_task
→ analyze_task
→ plan_task
→ execute_step
→ END
```

- `initialize_task`：把 `created` 任务推进为 `running`。
- `analyze_task`：把自然语言输入转换为 `TaskSpec`。
- `plan_task`：把 `TaskSpec` 转换为经过校验的 `PlanStep[]`，设置第一个 pending Step 为 `current_step_id`。
- `execute_step`：只定位当前 Step，把 `pending` 改为 `running`，调用注入的 Executor，追加新 `ToolCallRecord` 并保存 `step_outcome`。

`execute_step` 不推进 `current_step_id`，不把 Step 改为 `completed`。`END` 仅表示本阶段 Graph 执行结束，不表示 Step 或 Task 已经验证完成。

## 4. Phase 4 Data Model Changes

### StepExecutionOutcome

- `step_id`：本次 Executor 处理的 Step ID。
- `claimed_complete`：Executor 是否显式调用 `finish_step` 并声称当前 Step 已具备完成条件。它不是 Verifier 结论。
- `summary`：Executor 对本次 Step 执行的摘要。
- `evidence`：支持该完成声明的证据列表，使用安全的 `default_factory`。
- `action_count`：本次 loop 已消费的模型动作轮数，外部 Tool 与 `finish_step` 都计数。
- `error`：结构错误、动作上限或执行层不可用时的清晰错误。

### TaskState

新增 `step_outcome: StepExecutionOutcome | None`。`tool_calls` 继续保存原始外部 Tool 调用记录；`task_results` 仍然是与最终目标直接相关的提炼结果，二者没有合并。

### ToolCallRecord

复用既有模型。Phase 4 开始真实写入：

- 成功 Tool：`status=success`，保存 `result`。
- 失败 Tool：`status=failed`，保存脱敏后的 `error`。
- `call_id` 使用模型原生 `tool_call_id`；Runtime 要求非空且不能与本 Step 既有调用重复。

## 5. Full Tool Abstraction and ToolRegistry Source

### `src/taskpilot/tools/__init__.py`

```python
"""TaskPilot 的工具抽象与注册表。"""

from taskpilot.tools.base import Tool, tool_to_function_schema
from taskpilot.tools.registry import (
    DuplicateToolError,
    ToolArgumentsError,
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
)

__all__ = [
    "DuplicateToolError",
    "Tool",
    "ToolArgumentsError",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "tool_to_function_schema",
]
```

### `src/taskpilot/tools/base.py`

```python
"""与 LangChain BaseTool 解耦的轻量工具协议。"""

from copy import deepcopy
from typing import Any, Protocol


class Tool(Protocol):
    """TaskPilot Tool Runtime 所需的最小工具接口。"""

    name: str
    description: str
    input_schema: dict[str, Any]

    def invoke(self, arguments: dict[str, Any]) -> Any:
        """使用通过 JSON Schema 校验的参数调用工具。"""

        ...


def tool_to_function_schema(tool: Tool) -> dict[str, Any]:
    """把 TaskPilot Tool 转换为 OpenAI-compatible Function schema。"""

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": deepcopy(tool.input_schema),
        },
    }
```

### `src/taskpilot/tools/registry.py`

```python
"""本地 ToolRegistry 与 JSON Schema 参数校验。"""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from taskpilot.tools.base import Tool, tool_to_function_schema


class ToolRegistryError(ValueError):
    """ToolRegistry 可预期配置或调用错误的基类。"""


class DuplicateToolError(ToolRegistryError):
    """注册重复工具名称时抛出。"""


class UnknownToolError(ToolRegistryError):
    """请求未注册工具时抛出。"""


class ToolArgumentsError(ToolRegistryError):
    """工具参数不符合 input_schema 时抛出。"""


class ToolRegistry:
    """保存可用工具并负责调用前的确定性参数校验。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册名称唯一且 input_schema 合法的工具。"""

        if tool.name == "finish_step":
            raise DuplicateToolError("finish_step 是 Executor 内部控制动作，不能注册")
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具名称已注册: {tool.name}")
        try:
            Draft202012Validator.check_schema(tool.input_schema)
        except SchemaError as exc:
            raise ToolRegistryError(
                f"工具 {tool.name} 的 input_schema 无效: {exc.message}"
            ) from exc
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """按名称返回工具；未知名称会明确失败。"""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"未注册的工具: {name}") from exc

    def list_tools(self) -> list[Tool]:
        """按注册顺序返回工具列表副本。"""

        return list(self._tools.values())

    def list_function_schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI-compatible Function schemas。"""

        return [tool_to_function_schema(tool) for tool in self._tools.values()]

    def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        """校验参数后调用工具，不吞掉工具自身异常。"""

        tool = self.get(name)
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise ToolArgumentsError(
                f"工具 {name} 参数不符合 input_schema{location}: {exc.message}"
            ) from exc
        return tool.invoke(arguments)
```

## 6. Full Executor Source

### `src/taskpilot/executor.py`

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
            },
            "required": ["summary", "evidence"],
            "additionalProperties": False,
        },
    },
}

EXECUTOR_SYSTEM_PROMPT = """你是 Task Executor，只执行当前 PlanStep，不执行未来步骤，也不修改 Task Goal 或 Constraints。
根据当前环境 Observation 决定下一动作；优先使用可用 Tool 获取事实，不得虚构外部结果。
每轮必须且最多选择一个动作：调用一个外部 Tool，或调用内部 finish_step。
Tool 失败后可以根据错误 Observation 调整下一动作，单次 Tool 失败不等于 Step 或 Task 失败。
只有当前 Step 的 success_criteria 已有充分 evidence 时才能调用 finish_step。
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
    ) -> ExecutorRunResult:
        """只执行传入的当前步骤。"""

        ...


def build_executor_messages(
    *,
    task_spec: TaskSpec,
    step: PlanStep,
    task_results: dict[str, Any],
    tool_calls: Sequence[ToolCallRecord],
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

## 7. Graph Source Including execute_step

### `src/taskpilot/graph.py`

```python
"""TaskPilot Phase 4 的 LangGraph 工作流。"""

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.executor import (
    ExecutorRunResult,
    TaskExecutor,
    TaskExecutorLike,
)
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    TaskStatus,
)
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.state import TaskState, TaskStateUpdate
from taskpilot.tools.registry import ToolRegistry


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
        # 当前阶段只记录清晰错误，不在这里引入重试或恢复机制。
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
    """调用 Planner，并将结构化步骤写回任务状态。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Task Planner 无法运行: task_spec 缺失",
        }

    try:
        task_plan = planner.plan(task_spec)
    except Exception as exc:
        # 当前阶段只记录错误，不自动重试、重规划或恢复。
        error_detail = redact_sensitive_values(str(exc))
        return {
            "plan": [],
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Planner 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    current_step_id = next(
        (
            step.id
            for step in task_plan.steps
            if step.status == PlanStepStatus.PENDING
        ),
        None,
    )
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
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Executor 无法运行: current_step_id 缺失",
        }
    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Executor 无法运行: task_spec 缺失",
        }

    matching_indices = [
        index
        for index, step in enumerate(state["plan"])
        if step.id == current_step_id
    ]
    if len(matching_indices) != 1:
        return {
            "status": TaskStatus.FAILED,
            "error": (
                "Executor 无法定位唯一当前步骤: "
                f"current_step_id={current_step_id}"
            ),
        }

    step_index = matching_indices[0]
    current_step = state["plan"][step_index]
    if current_step.status not in {
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    }:
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Executor 不能执行状态为 {current_step.status.value} 的步骤 "
                f"{current_step.id}"
            ),
        }

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


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
) -> CompiledStateGraph:
    """构建并编译包含单步骤 Executor 的 Phase 4 状态图。"""

    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = (
        executor
        if executor is not None
        else TaskExecutor(registry=ToolRegistry())
    )
    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node(
        "analyze_task",
        partial(analyze_task, analyzer=active_analyzer),
    )
    builder.add_node(
        "plan_task",
        partial(plan_task, planner=active_planner),
    )
    builder.add_node(
        "execute_step",
        partial(execute_step, executor=active_executor),
    )
    # Phase 4 每次只执行 current_step_id 对应的一个步骤。
    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_edge("plan_task", "execute_step")
    builder.add_edge("execute_step", END)
    return builder.compile()
```

## 8. StepExecutionOutcome Definition

当前实际定义：

```python
class StepExecutionOutcome(BaseModel):
    """Executor 对当前步骤执行结果的结构化声明。"""

    step_id: int
    claimed_complete: bool = False
    summary: str | None = None
    evidence: list[str] = Field(default_factory=list)
    action_count: int = 0
    error: str | None = None
```

`claimed_complete=True` 只来自合法的显式 `finish_step`。Executor 不修改 `PlanStep.status` 为 `completed`。

## 9. finish_step Schema and Design

`finish_step` 是 Executor 内部控制动作，不是外部 Tool，`ToolRegistry.register()` 明确拒绝该名称。

当前 schema：

```python
FINISH_STEP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish_step",
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
            },
            "required": ["summary", "evidence"],
            "additionalProperties": False,
        },
    },
}
```

没有 Tool Call 不能被解释成完成；一轮返回零个 Tool Calls 会产生结构错误。只有 schema 合法的 `finish_step` 才生成 `claimed_complete=True` 的 Outcome。

## 10. Executor System Prompt

当前实际 Prompt 全文：

```text
你是 Task Executor，只执行当前 PlanStep，不执行未来步骤，也不修改 Task Goal 或 Constraints。
根据当前环境 Observation 决定下一动作；优先使用可用 Tool 获取事实，不得虚构外部结果。
每轮必须且最多选择一个动作：调用一个外部 Tool，或调用内部 finish_step。
Tool 失败后可以根据错误 Observation 调整下一动作，单次 Tool 失败不等于 Step 或 Task 失败。
只有当前 Step 的 success_criteria 已有充分 evidence 时才能调用 finish_step。
finish_step 只表示 Executor 声称当前 Step 已具备完成条件，最终是否完成由未来 Verifier 判断。
不要直接把 PlanStep 或整个 Task 声称为 completed。
```

Prompt 明确限制当前 Step、禁止修改目标与约束、禁止虚构事实、每轮一个动作、允许从 Tool Error 调整、要求 evidence，并把最终完成判断留给未来 Verifier。

## 11. Function Calling Loop

真实 `TaskExecutor` 首先把 Registry 的 Function schemas 与 `finish_step` schema 一起传给 `model.bind_tools(...)`。随后最多执行 `max_actions_per_step` 轮：

1. 调用绑定工具后的模型。
2. 要求返回 `AIMessage` 且 `tool_calls` 数量严格等于 1。
3. 检查 `tool_call_id` 非空、在当前执行历史中唯一。
4. 如果是 `finish_step`，确定性校验参数并返回完成声明。
5. 如果是外部 Tool，检查工具存在并通过 JSON Schema 参数校验。
6. 调用工具，创建 success/failed `ToolCallRecord`。
7. 依次把原始 `AIMessage` 和对应 `ToolMessage` 追加到临时 messages。
8. 进入下一轮。

没有正则、`eval`、Markdown Action 解析或手工截取 JSON 字符串。工具动作直接读取 `AIMessage.tool_calls`。

## 12. tool_call_id to ToolMessage Mapping

外部调用采用模型返回的原生 ID：

```text
AIMessage.tool_calls[0].id
        │
        ├── ToolCallRecord.call_id
        └── ToolMessage.tool_call_id
```

Runtime 先把带原始 Tool Call 的 `AIMessage` 写入 messages，再写入 `ToolMessage`。离线 smoke test 中三处 ID 均为 `smoke-tool-1`，第二轮模型因此看到合法的 assistant-tool 配对，而不是普通 user 文本结果。

## 13. Tool Error Behavior

- `ToolRegistry` 不吞掉 Tool 自身异常。
- `TaskExecutor` 捕获外部 Tool 异常，创建 `status=failed` 的 `ToolCallRecord`。
- 失败信息被写成 `ToolMessage(status="error")`，并保留相同 `tool_call_id`。
- 模型可以在下一轮选择备用 Tool 或其他动作。
- 单次普通 Tool Error 不自动把 Task 设为 `failed`。
- 未知工具、非法参数、多 Tool Calls、缺失 action/ID 等结构错误属于 fatal executor error，Graph 可把 Task 设为 `failed`。
- API Key 若出现在异常文本中会替换为 `[REDACTED]`。

## 14. max_actions Behavior

`max_actions_per_step` 构造参数默认值为 5，必须至少为 1。每次模型动作计数一次，包括外部 Tool 和 `finish_step`。

达到上限但没有合法 `finish_step` 时：

```text
claimed_complete = false
error = "Executor action limit reached: <limit>"
fatal_error = false
```

已完成的外部 Tool records 会保留。Graph 保持 Task 为 `running`，把错误放在 `step_outcome.error`，不会进入无限循环。

## 15. Full Relevant Tests

### `tests/test_tools.py`

```python
"""TaskPilot Tool abstraction 与 ToolRegistry 的离线测试。"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from taskpilot.tools import (
    DuplicateToolError,
    ToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
)


LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@dataclass
class LookupTool:
    """只返回固定数据的离线测试工具。"""

    name: str = "lookup"
    description: str = "Look up fixed offline items."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: dict(LOOKUP_SCHEMA)
    )

    def invoke(self, arguments: dict[str, Any]) -> Any:
        return {
            "query": arguments["query"],
            "items": [{"name": "offline-item"}],
        }


@dataclass
class FailingTool:
    """用于验证工具异常透明传播。"""

    name: str = "failing"
    description: str = "Always fails offline."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any]) -> Any:
        raise RuntimeError("offline tool failure")


def test_register_and_list_tool() -> None:
    registry = ToolRegistry()
    tool = LookupTool()

    registry.register(tool)

    assert registry.get("lookup") is tool
    assert registry.list_tools() == [tool]


def test_duplicate_tool_name_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(LookupTool())

    with pytest.raises(DuplicateToolError, match="工具名称已注册: lookup"):
        registry.register(LookupTool())


def test_finish_step_cannot_be_registered_as_external_tool() -> None:
    tool = LookupTool(name="finish_step")

    with pytest.raises(DuplicateToolError, match="内部控制动作"):
        ToolRegistry().register(tool)


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(UnknownToolError, match="未注册的工具: missing"):
        ToolRegistry().invoke("missing", {})


def test_arguments_are_validated_before_invocation() -> None:
    registry = ToolRegistry()
    registry.register(LookupTool())

    with pytest.raises(ToolArgumentsError, match="参数不符合 input_schema"):
        registry.invoke("lookup", {"unexpected": 1})


def test_tool_returns_result_and_function_schema() -> None:
    registry = ToolRegistry()
    registry.register(LookupTool())

    result = registry.invoke("lookup", {"query": "gym"})
    schemas = registry.list_function_schemas()

    assert result == {
        "query": "gym",
        "items": [{"name": "offline-item"}],
    }
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up fixed offline items.",
                "parameters": LOOKUP_SCHEMA,
            },
        }
    ]


def test_tool_exception_is_not_swallowed() -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())

    with pytest.raises(RuntimeError, match="offline tool failure"):
        registry.invoke("failing", {})
```

### `tests/test_executor.py`

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
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
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
    ).invoke(make_initial_state())

    assert result["status"] is TaskStatus.RUNNING
    assert result["tool_calls"][0].status is ToolCallStatus.FAILED
    assert result["tool_calls"][1].status is ToolCallStatus.SUCCESS
    assert result["step_outcome"].claimed_complete is True
    assert result["plan"][0].status is PlanStepStatus.RUNNING
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
        {"summary": "Done", "evidence": []},
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
    assert "verification" not in context


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
                PlanStep(
                    id=2,
                    description="Prepare a later result",
                    depends_on=[1],
                    success_criteria=["Later result is prepared"],
                ),
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
    ) -> ExecutorRunResult:
        self.executed_step_ids.append(step.id)
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step.id,
                claimed_complete=True,
                summary="Fake executor claim",
                evidence=["Fake evidence"],
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


def make_initial_state() -> TaskState:
    return {
        "task_id": "phase-4-graph",
        "user_input": "Find offline candidates",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def test_graph_executes_only_current_step_without_completing_it() -> None:
    executor = FakeExecutor()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
    )

    result = graph.invoke(make_initial_state())

    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("plan_task", "execute_step") in edges
    assert ("execute_step", "__end__") in edges
    assert executor.executed_step_ids == [1]
    assert result["status"] is TaskStatus.RUNNING
    assert result["current_step_id"] == 1
    assert result["plan"][0].status is PlanStepStatus.RUNNING
    assert result["plan"][0].status is not PlanStepStatus.COMPLETED
    assert result["plan"][1].status is PlanStepStatus.PENDING
    assert result["step_outcome"].claimed_complete is True
    assert result["step_outcome"].step_id == 1
    assert result["tool_calls"][0].call_id == "graph-call-1"
    assert result["error"] is None
```

## 16. Offline Test Results

实际命令：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
conda run --no-capture-output -n ai python -m pytest -q
```

最终实际输出：

```text
.............................................                            [100%]
45 passed in 0.72s
```

- Passed: 45
- Failed: 0

测试完全离线，不要求 `LLM_API_KEY`，不调用网络、Browser、MCP 或 Search API。

另行验证真实 `ChatOpenAI.bind_tools([FINISH_STEP_SCHEMA])` 的本地构造行为：

```text
native_bind_tools=RunnableBinding
network_called=false
```

## 17. Offline Executor Graph Smoke Test

使用 Fake Analyzer + Fake Planner + 真实 `TaskExecutor` + Fake Function Calling Model + Fake Lookup Tool 执行一次 `graph.invoke`。

输入：

```text
Find offline candidates
```

TaskSpec：

```json
{"goal":"Find offline candidates","constraints":{"count":2},"expected_output":null,"completion_criteria":["Return two candidates"]}
```

Plan：

```json
[
  {"id":1,"description":"Discover candidates that can be verified","status":"running","retry_count":0,"depends_on":[],"success_criteria":["At least two candidates are available"]},
  {"id":2,"description":"Prepare a later result","status":"pending","retry_count":0,"depends_on":[1],"success_criteria":["Later result is prepared"]}
]
```

ToolCallRecord：

```json
{"call_id":"smoke-tool-1","step_id":1,"tool_name":"lookup","arguments":{"query":"offline gyms"},"status":"success","result":{"query":"offline gyms","items":["candidate-a","candidate-b"]},"error":null}
```

StepExecutionOutcome：

```json
{"step_id":1,"claimed_complete":true,"summary":"Current step has enough offline evidence.","evidence":["Two fixed candidates were returned."],"action_count":2,"error":null}
```

最终摘要：

```text
current_step_id=1
status=running
error=None
model_rounds=2
tool_message_id=smoke-tool-1
```

只有 Step 1 从 `pending` 变为 `running`；Step 2 仍为 `pending`。`current_step_id` 没有推进，Step 1 没有变为 `completed`。

## 18. Boundary Checks

### Q1. Executor 是否真的使用模型原生 Function Calling，而不是正则解析？

**Yes。** 真实代码调用 `model.bind_tools(schemas)`，并直接读取 `AIMessage.tool_calls`。本地构造返回 `RunnableBinding`；测试使用 Fake Chat Model 模拟同一接口，没有 Action 文本、正则或 `eval`。

### Q2. Planner 是否仍然不指定具体工具？

**Yes。** 修正后的 Fake Plan Step 1 是“发现一批与目标地点相关、可进一步核实的候选健身房”，Step 2 是“获取候选的价格、地址和营业时间”。Plan 描述 WHAT，不包含 `lookup(...)`、browser 或 selector。具体 Tool 由 Executor 根据可用 Registry 选择。

### Q3. Tool Success 是否会被错误地当成 Step Success？

**No。** Tool success 只产生 `ToolCallRecord(status=success)` 和 Observation。只有后续显式 `finish_step` 才产生 `claimed_complete=True`，而这仍不是 Verifier 结论。

### Q4. finish_step 是否直接把 PlanStep 标记 completed？

**No。** Graph 在开始执行时只执行 `pending → running`。smoke test 中 Outcome 已 claimed complete，但 Step status 仍为 `running`。

### Q5. Tool Error 是否一定导致 Task Failed？

**No。** 普通 Tool 异常被记录并回写为 error ToolMessage。测试验证主 Tool 失败后调用备用 Tool，再 `finish_step`，最终 Task 仍为 `running`。结构错误可以使 Task failed。

### Q6. 是否存在无限 Action Loop？

**No。** `range(max_actions_per_step)` 提供明确上限，默认 5；达到上限返回 incomplete Outcome。

### Q7. Executor 是否一次执行了未来多个 PlanStep？

**No。** `execute_step` 只查找 `current_step_id` 对应的唯一 Step，并只把该 Step 传给 Executor。测试验证第二个未来 Step 保持 pending。

## 19. Known Limitations

- `claimed_complete` 尚未被 Step Verifier 校验。
- 没有 Step advancement；`current_step_id` 不会移动到下一步。
- 没有 Task Verifier，Task 不会因本阶段执行而标记 completed。
- 没有 Dynamic Replanning、retry manager 或 recovery。
- 没有 MCP、Playwright、Browser、真实 Search 或生产 Filesystem Tool。
- CLI 默认 Registry 为空，会诚实报告执行层没有外部工具。
- 当前只支持每轮一个 Tool Call，不支持 parallel tool calling。
- `ToolCallRecord.call_id` 依赖模型提供原生 ID；缺失或重复会失败，不会自动改写 assistant message。
- 上下文没有 token budget、压缩、向量记忆或长期记忆。
- Tool result 保存在内存状态中，没有持久化或 checkpoint。
- 没有真实 Benchmark execution。

## 20. Files Changed

### 新建

- `src/taskpilot/executor.py`：Function Calling Executor loop、context builder、finish_step 和执行结果封装。
- `src/taskpilot/tools/__init__.py`：公开 Tool Runtime API。
- `src/taskpilot/tools/base.py`：Tool Protocol 和 Function schema 转换。
- `src/taskpilot/tools/registry.py`：注册、查询、参数校验和调用。
- `tests/test_tools.py`：ToolRegistry 离线测试。
- `tests/test_executor.py`：Executor、消息关联、错误、上限和 Graph 离线测试。
- `docs/review/phase_04.md`：本阶段验收文档。

### 修改

- `.gitignore`：移除整目录 `docs/` 忽略规则，使阶段验收文档可以纳入 Git；保留 `docs/_build/` 忽略。
- `pyproject.toml`：新增 `jsonschema>=4`。
- `src/taskpilot/models.py`：新增 `StepExecutionOutcome`。
- `src/taskpilot/state.py`：新增 `step_outcome`。
- `src/taskpilot/graph.py`：新增 `execute_step` 和 Executor 注入。
- `src/taskpilot/cli.py`：输出 execution outcome；无生产 Tool 时明确失败。
- `README.md`：更新 Phase 4 实现与未实现边界。
- `tests/test_models.py`：增加 Outcome 可变默认值测试。
- `tests/test_analyzer.py`、`tests/test_planner.py`、`tests/test_graph_smoke.py`：为 Phase 4 Graph 注入离线 Fake Executor。
- `tests/test_planner.py`：修复 Phase 3 Fake Plan 的步骤语义顺序。
- `docs/review/phase_03.md`：同步修正 Fake Plan 示例语义。

没有创建 Verifier、advance_step、Replanner、MCPClient、Playwright、BrowserAgent、HumanApproval、CheckpointStore、RecoveryManager 或 Trace backend 空壳。

## 21. Git Status

文档完成后执行：

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
?? src/taskpilot/executor.py
?? src/taskpilot/tools/
?? tests/test_executor.py
?? tests/test_tools.py
```

前两行是当前环境无法读取用户级 Git ignore 文件的警告，不影响仓库状态读取。Phase 4 删除了仓库末尾的 `docs/` 忽略规则，因此 `phase_04.md` 可以正常出现在 Git 状态中；`docs/_build/` 仍被忽略。
