# TaskPilot Phase 6 Review

## 1. Phase Goal

Phase 6 的目标是把唯一的 Tool Runtime 迁移为 async，并使用当前环境中的官方 MCP Python SDK 动态发现、适配和调用真实 MCP Tool。

实际完成：

- Tool Protocol、ToolRegistry invocation、TaskExecutor 和 LangGraph execute_step 的 async 迁移；
- CLI 顶层使用一次 `asyncio.run(async_main())`，Graph 内使用 `await graph.ainvoke(...)`；
- 官方 MCP SDK v2 的 STDIO 与 Streamable HTTP client lifecycle；
- 多 Server 长连接、initialize、完整分页 list_tools、namespace、动态注册；
- MCP result JSON 归一化、metadata 保留和 is_error 失败语义；
- 真实本地 MCP v2 STDIO Server 的 discovery、call、Executor 和 error recovery 测试。

明确没有实现 Playwright/Browser、production Search、production Filesystem Agent、HITL/RiskPolicy、Checkpoint/Recovery、Dynamic Replanning、FastAPI、Vision 或 Benchmark execution。

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
│       ├── phase_05.md
│       └── phase_06.md
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
│       ├── mcp/
│       │   ├── __init__.py
│       │   ├── adapter.py
│       │   ├── config.py
│       │   └── provider.py
│       └── tools/
│           ├── __init__.py
│           ├── base.py
│           └── registry.py
└── tests/
    ├── fixtures/
    │   ├── mcp_config.json
    │   └── mcp_server.py
    ├── test_analyzer.py
    ├── test_executor.py
    ├── test_graph_smoke.py
    ├── test_mcp.py
    ├── test_mcp_integration.py
    ├── test_models.py
    ├── test_phase5_graph.py
    ├── test_planner.py
    ├── test_tools.py
    └── test_verifier.py
```

目录树排除了 `.git/`、`__pycache__/`、`.pytest_cache/` 和构建缓存。

## 3. 为什么 Phase 6 迁移 async

MCP STDIO、Streamable HTTP 和未来浏览器执行都是 I/O-bound 且具有 async 生命周期。若同步 Tool 在每次调用内部使用 `asyncio.run()`，会反复创建 event loop，并在已有 loop（例如 async LangGraph 或未来 Web Host）中直接报错。

因此本阶段只有一套 Tool Runtime：

```text
async Tool.invoke
→ await ToolRegistry.invoke
→ await TaskExecutor.execute
→ async execute_step node
→ await graph.ainvoke
```

`asyncio.run()` 只存在于 CLI 最外层进程入口，不在 Tool、Registry、Executor、Provider 或 Graph node 内。

## 4. Async Tool Protocol 完整源码

### src/taskpilot/tools/base.py

```python
"""与 LangChain BaseTool 解耦的轻量工具协议。"""

from copy import deepcopy
from typing import Any, Protocol


class Tool(Protocol):
    """TaskPilot Tool Runtime 所需的最小工具接口。"""

    name: str
    description: str
    input_schema: dict[str, Any]

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """异步调用已通过 JSON Schema 校验的 I/O 工具。"""

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

不存在 SyncTool + AsyncTool 双体系。

## 5. Async ToolRegistry 完整源码

### src/taskpilot/tools/registry.py

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

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        """校验参数后异步调用工具，不吞掉工具自身异常。"""

        tool = self.get(name)
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise ToolArgumentsError(
                f"工具 {name} 参数不符合 input_schema{location}: {exc.message}"
            ) from exc
        return await tool.invoke(arguments)
```

register/get/list_tools/list_function_schemas 仍是同步内存操作；只有实际 invocation 使用 await。Unknown、JSON Schema 和 Tool 自身异常语义保持不变。

## 6. TaskExecutor Async 修改

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

    async def execute(
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

    async def execute(
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
                result = await self._registry.invoke(action_name, arguments)
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

关键变化是 `async def execute`、`await model_with_tools.ainvoke(...)` 和 `await registry.invoke(...)`。每轮一个 Action、finish_step、ToolMessage/tool_call_id、普通 Tool error 反馈、fatal 结构错误、动作上限、dependency StepResults 和 verifier feedback 均保留。

## 7. Graph Async 调用变化

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

### src/taskpilot/cli.py

```python
"""TaskPilot Phase 6 的 async Host/CLI 入口。"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from taskpilot.executor import TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.mcp.provider import MCPToolProvider
from taskpilot.models import TaskStatus
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


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


async def async_main(argv: Sequence[str] | None = None) -> int:
    """在可选 MCP 生命周期内异步运行一次 TaskPilot Graph。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot Phase 6 graph.")
    parser.add_argument("task", help="Task description")
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="JSON file containing MCP server configurations",
    )
    args = parser.parse_args(argv)

    if args.mcp_config is None:
        # 默认没有生产 Tool；执行层会明确失败，不会生成假的外部结果。
        result = await build_graph().ainvoke(create_initial_state(args.task))
    else:
        registry = ToolRegistry()
        configs = load_mcp_server_configs(args.mcp_config)
        # MCP session 在整个 Graph 执行期间保持连接，Graph 完成后统一关闭。
        async with MCPToolProvider(configs) as provider:
            await provider.register_tools(registry)
            executor = TaskExecutor(registry=registry)
            result = await build_graph(executor=executor).ainvoke(
                create_initial_state(args.task)
            )
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 顶层创建一次事件循环；Graph node 内不会创建 event loop。"""

    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
```

Graph 中只有 execute_step 因真实 I/O 改为 async；Analyzer、Planner、Verifier 暂时保持已有同步模型接口。测试和 Host 使用 `ainvoke`。MCP 不属于 Graph node。

## 8. MCPServerConfig

### src/taskpilot/mcp/config.py

```python
"""MCP Server transport 的轻量、安全配置模型。"""

import json
from enum import Enum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class MCPTransport(str, Enum):
    """Phase 6 支持的非 deprecated MCP transports。"""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPServerConfig(BaseModel):
    """单个 MCP Server 的连接配置。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    server_id: str
    transport: MCPTransport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    # SecretStr 确保 repr 和 model_dump(mode="json") 不泄露显式传入的值。
    env: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    url: str | None = None

    @field_validator("server_id")
    @classmethod
    def validate_server_id(cls, value: str) -> str:
        """server_id 去除首尾空白后必须非空。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("server_id 不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerConfig":
        """确保每种 transport 只使用自己必需的连接字段。"""

        if self.transport is MCPTransport.STDIO:
            if not self.command or not self.command.strip():
                raise ValueError("STDIO MCP Server 必须提供 command")
            if self.url is not None:
                raise ValueError("STDIO MCP Server 不能提供 url")
        else:
            if not self.url or not self.url.startswith(("http://", "https://")):
                raise ValueError(
                    "Streamable HTTP MCP Server 必须提供 http(s) url"
                )
            if self.command is not None or self.args or self.env:
                raise ValueError(
                    "Streamable HTTP MCP Server 不能提供 command、args 或 env"
                )
        return self

    def resolved_env(self) -> dict[str, str]:
        """只解开用户显式配置的环境变量，不复制整个 os.environ。"""

        return {
            name: secret.get_secret_value() for name, secret in self.env.items()
        }


class MCPConfigFile(BaseModel):
    """CLI MCP JSON 文件的顶层结构。"""

    servers: list[MCPServerConfig]


def load_mcp_server_configs(path: Path) -> list[MCPServerConfig]:
    """从 JSON 文件读取 MCP Server 配置。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    config_file = MCPConfigFile.model_validate(raw)
    return config_file.servers
```

支持：

- `transport=stdio`：server_id、command、args、env；
- `transport=streamable_http`：server_id、url；
- transport 字段互斥校验；
- env 使用 SecretStr，dump/repr 不泄漏；
- resolved_env 只返回显式配置，不复制 os.environ；
- CLI JSON 顶层格式为 `{"servers": [...]}`。

## 9. MCPToolAdapter 完整源码

### src/taskpilot/mcp/adapter.py

```python
"""把动态发现的 MCP Tool 适配为 TaskPilot async Tool。"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.types import CallToolResult
from pydantic import BaseModel

from taskpilot.config import redact_sensitive_values


_TOOL_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_FUNCTION_NAME_LENGTH = 64


class MCPToolExecutionError(RuntimeError):
    """MCP Server 以 is_error=true 返回工具业务失败。"""

    def __init__(self, tool_name: str, result: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.result = result
        detail = json.dumps(result, ensure_ascii=False, default=str)
        super().__init__(
            f"MCP 工具 {tool_name} 返回 is_error=true: {detail}"
        )


class MCPTransportError(RuntimeError):
    """MCP transport 或 session 调用本身失败。"""


def _sanitize_name_part(value: str) -> str:
    """把任意 MCP 标识稳定转换为 Function Calling 安全名称片段。"""

    normalized = _TOOL_NAME_PATTERN.sub("_", value).strip("_")
    if normalized:
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"unnamed_{digest}"


def build_public_tool_name(server_id: str, remote_tool_name: str) -> str:
    """生成 deterministic、最长 64 字符的 namespaced public name。"""

    raw_name = (
        f"{_sanitize_name_part(server_id)}__"
        f"{_sanitize_name_part(remote_tool_name)}"
    )
    if len(raw_name) <= _MAX_FUNCTION_NAME_LENGTH:
        return raw_name
    digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
    prefix_length = _MAX_FUNCTION_NAME_LENGTH - len(digest) - 1
    return f"{raw_name[:prefix_length]}_{digest}"


def _json_serializable(value: Any) -> Any:
    """把 MCP/Pydantic 值转换为稳定的 JSON-compatible representation。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=False)
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def normalize_call_tool_result(result: CallToolResult) -> dict[str, Any]:
    """保留 structured content 与所有 content block 类型。"""

    return {
        "structured_content": _json_serializable(result.structured_content),
        "content": [_json_serializable(block) for block in result.content],
        "is_error": result.is_error,
    }


@dataclass
class MCPToolAdapter:
    """持有长生命周期 ClientSession 的 TaskPilot Tool 实现。"""

    name: str
    remote_tool_name: str
    server_id: str
    description: str
    input_schema: dict[str, Any]
    session: ClientSession = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """用远端原始名称调用 MCP Tool，并规范化 Observation。"""

        try:
            result = await self.session.call_tool(
                self.remote_tool_name,
                arguments=arguments,
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            raise MCPTransportError(
                f"MCP 工具 {self.name} transport/session 调用失败: "
                f"{type(exc).__name__}: {detail}"
            ) from exc
        if not isinstance(result, CallToolResult):
            raise MCPTransportError(
                f"MCP 工具 {self.name} 返回不支持的结果类型: "
                f"{type(result).__name__}"
            )

        normalized = normalize_call_tool_result(result)
        if result.is_error:
            raise MCPToolExecutionError(self.name, normalized)
        return normalized
```

Adapter 保存 public name、remote tool name、server_id、description、input schema、session 和 metadata。调用远端时发送 remote name，而不是 namespaced public name。

## 10. MCPToolProvider 完整源码

### src/taskpilot/mcp/provider.py

```python
"""多个 MCP Server 的长生命周期连接、发现与注册。"""

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams, Tool as MCPTool

from taskpilot.config import redact_sensitive_values
from taskpilot.mcp.adapter import MCPToolAdapter, build_public_tool_name
from taskpilot.mcp.config import MCPServerConfig, MCPTransport
from taskpilot.tools import ToolRegistry


class MCPConfigurationError(ValueError):
    """MCP 配置、namespace 或注册冲突。"""


class MCPConnectionError(RuntimeError):
    """MCP transport 建连或 initialize 失败。"""


@dataclass(frozen=True)
class MCPDiscoveryInfo:
    """供检查与测试使用的发现映射，不参与 Tool 选择。"""

    server_id: str
    remote_tool_name: str
    public_tool_name: str


class MCPToolProvider:
    """Host 侧 MCP 连接基础设施；不参与 Graph、Planner 或 Verifier。"""

    def __init__(self, configs: Sequence[MCPServerConfig]) -> None:
        self._configs = list(configs)
        self._validate_server_ids()
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._adapters: list[MCPToolAdapter] = []
        self._discovery_info: list[MCPDiscoveryInfo] = []

    @property
    def connected_server_ids(self) -> list[str]:
        """返回当前仍处于 provider lifecycle 内的 server IDs。"""

        return list(self._sessions)

    @property
    def discovery_info(self) -> list[MCPDiscoveryInfo]:
        """返回动态发现产生的 namespace 映射副本。"""

        return list(self._discovery_info)

    async def __aenter__(self) -> "MCPToolProvider":
        """一次连接并 initialize 所有配置的 MCP Servers。"""

        if self._stack is not None:
            raise RuntimeError("MCPToolProvider 不能重复进入 context")
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for config in self._configs:
                try:
                    await self._connect(config)
                except Exception as exc:
                    detail = redact_sensitive_values(str(exc))
                    for secret in config.resolved_env().values():
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    raise MCPConnectionError(
                        f"MCP Server {config.server_id} 连接或 initialize 失败: "
                        f"{type(exc).__name__}: {detail}"
                    ) from exc
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            self._sessions.clear()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool | None:
        """按 LIFO 顺序统一关闭 session 与 transport/subprocess。"""

        stack = self._stack
        self._stack = None
        self._sessions.clear()
        if stack is None:
            return None
        return await stack.__aexit__(exc_type, exc, traceback)

    async def discover_tools(self) -> list[MCPToolAdapter]:
        """完整分页调用 list_tools，并创建 TaskPilot adapters。"""

        if self._stack is None:
            raise RuntimeError("discover_tools 必须在 MCPToolProvider context 内调用")
        if self._adapters:
            return list(self._adapters)

        adapters: list[MCPToolAdapter] = []
        public_names: set[str] = set()
        discovery_info: list[MCPDiscoveryInfo] = []
        for config in self._configs:
            session = self._sessions[config.server_id]
            for remote_tool in await self._list_all_tools(session):
                public_name = build_public_tool_name(
                    config.server_id,
                    remote_tool.name,
                )
                if public_name in public_names:
                    raise MCPConfigurationError(
                        "MCP Tool namespace 冲突: "
                        f"public_tool_name={public_name}"
                    )
                public_names.add(public_name)
                adapters.append(
                    self._make_adapter(
                        config.server_id,
                        public_name,
                        remote_tool,
                        session,
                    )
                )
                discovery_info.append(
                    MCPDiscoveryInfo(
                        server_id=config.server_id,
                        remote_tool_name=remote_tool.name,
                        public_tool_name=public_name,
                    )
                )

        self._adapters = adapters
        self._discovery_info = discovery_info
        return list(adapters)

    async def register_tools(self, registry: ToolRegistry) -> list[MCPToolAdapter]:
        """发现并注册 adapters；不会静默覆盖 Local 或其他 MCP Tool。"""

        adapters = await self.discover_tools()
        existing_names = {tool.name for tool in registry.list_tools()}
        conflicts = sorted(
            adapter.name for adapter in adapters if adapter.name in existing_names
        )
        if conflicts:
            raise MCPConfigurationError(
                f"MCP Tool 与现有 Registry 名称冲突: {conflicts}"
            )
        for adapter in adapters:
            registry.register(adapter)
        return adapters

    async def _connect(self, config: MCPServerConfig) -> None:
        """根据当前 MCP SDK v2 transport API 建立并初始化 session。"""

        if self._stack is None:  # pragma: no cover - 仅防御错误内部调用
            raise RuntimeError("MCPToolProvider context 尚未进入")
        if config.transport is MCPTransport.STDIO:
            parameters = StdioServerParameters(
                command=config.command or "",
                args=config.args,
                env=config.resolved_env(),
            )
            streams = await self._stack.enter_async_context(
                stdio_client(parameters)
            )
        else:
            streams = await self._stack.enter_async_context(
                streamable_http_client(config.url or "")
            )
        session = await self._stack.enter_async_context(ClientSession(*streams))
        await session.initialize()
        self._sessions[config.server_id] = session

    @staticmethod
    async def _list_all_tools(session: ClientSession) -> list[MCPTool]:
        """跟随 next_cursor 直到 MCP Server 的最后一页。"""

        tools: list[MCPTool] = []
        cursor: str | None = None
        while True:
            params = (
                PaginatedRequestParams(cursor=cursor)
                if cursor is not None
                else None
            )
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tools

    @staticmethod
    def _make_adapter(
        server_id: str,
        public_name: str,
        remote_tool: MCPTool,
        session: ClientSession,
    ) -> MCPToolAdapter:
        """保留 SDK 实际暴露的 metadata，不执行 Phase 8 风险策略。"""

        metadata = {
            "title": remote_tool.title,
            "annotations": (
                remote_tool.annotations.model_dump(mode="json", by_alias=False)
                if remote_tool.annotations is not None
                else None
            ),
            "output_schema": remote_tool.output_schema,
            "icons": (
                [icon.model_dump(mode="json", by_alias=False) for icon in remote_tool.icons]
                if remote_tool.icons is not None
                else None
            ),
            "execution": (
                remote_tool.execution.model_dump(mode="json", by_alias=False)
                if remote_tool.execution is not None
                else None
            ),
            "meta": remote_tool.meta,
        }
        return MCPToolAdapter(
            name=public_name,
            remote_tool_name=remote_tool.name,
            server_id=server_id,
            description=remote_tool.description or remote_tool.title or "",
            input_schema=remote_tool.input_schema,
            session=session,
            metadata=metadata,
        )

    def _validate_server_ids(self) -> None:
        server_ids = [config.server_id for config in self._configs]
        if len(server_ids) != len(set(server_ids)):
            raise MCPConfigurationError("MCP server_id 必须唯一")
        namespace_ids = [
            build_public_tool_name(config.server_id, "tool").rsplit("__", 1)[0]
            for config in self._configs
        ]
        if len(namespace_ids) != len(set(namespace_ids)):
            raise MCPConfigurationError(
                "MCP server_id 清理后产生 namespace 冲突"
            )
```

### src/taskpilot/mcp/__init__.py

```python
"""官方 MCP SDK 到 TaskPilot Tool Runtime 的基础设施适配层。"""

from taskpilot.mcp.adapter import (
    MCPToolAdapter,
    MCPToolExecutionError,
    MCPTransportError,
    build_public_tool_name,
    normalize_call_tool_result,
)
from taskpilot.mcp.config import (
    MCPServerConfig,
    MCPTransport,
    load_mcp_server_configs,
)
from taskpilot.mcp.provider import (
    MCPConnectionError,
    MCPConfigurationError,
    MCPToolProvider,
)

__all__ = [
    "MCPConfigurationError",
    "MCPConnectionError",
    "MCPServerConfig",
    "MCPToolAdapter",
    "MCPToolExecutionError",
    "MCPToolProvider",
    "MCPTransport",
    "MCPTransportError",
    "build_public_tool_name",
    "load_mcp_server_configs",
    "normalize_call_tool_result",
]
```

Provider 只负责连接、initialize、发现、适配、注册和生命周期，不选择工具、不调用 LLM、不参与 Planner/Verifier、不修改 TaskState。

## 11. STDIO Lifecycle

实际 SDK v2 路径：

```text
StdioServerParameters(command, args, explicit env)
→ AsyncExitStack.enter_async_context(stdio_client(parameters))
→ AsyncExitStack.enter_async_context(ClientSession(*streams))
→ await session.initialize()
→ discovery / graph execution / calls while session remains alive
→ close ClientSession
→ close stdio transport and subprocess
```

官方 SDK 的 `stdio_client` 自身只继承安全 allowlist，再叠加配置显式 env；TaskPilot 不把 LLM_API_KEY 传给第三方 Server。Provider 连接错误会替换显式 MCP env secrets 和当前 LLM key。

## 12. Streamable HTTP Lifecycle

代码使用当前非 deprecated 的：

```python
streams = await stack.enter_async_context(
    streamable_http_client(config.url)
)
session = await stack.enter_async_context(ClientSession(*streams))
await session.initialize()
```

配置和 lifecycle 使用完全离线 fake transport/session 测试覆盖。Phase 6 没有启动真实 HTTP Server，以避免在 Windows pytest 中引入端口和进程关闭的 flaky 行为；真实 STDIO 集成是强制验收路径。没有使用 deprecated SSE。

## 13. Tool Namespace 规则

规则为：

```text
{sanitize(server_id)}__{sanitize(remote_tool_name)}

web + search         → web__search
filesystem + read    → filesystem__read
web-a + search       → web-a__search
web-b + search       → web-b__search
```

非法字符稳定替换为下划线；全非法片段使用 SHA-256 短摘要；超过 64 字符时以摘要稳定截断。Provider 检查 server_id、清理后的 namespace 和最终 public name 冲突；Registry 也拒绝覆盖现有 Local Tool。

## 14. MCP list_tools Discovery

客户端没有硬编码 echo/add。Provider 在每个已 initialize 的 session 上调用 `list_tools`，从 MCP Tool 对象读取 name、description、input_schema 和实际 metadata，构造 adapter 后注册到 Registry。

测试 fixture 暴露的工具名称只存在于 Server 代码中；客户端通过真实 subprocess 的 list_tools 后才得到 `demo__echo`、`demo__add` 和 `demo__fail_tool`。

## 15. Pagination Handling

`_list_all_tools` 从 `params=None` 开始，读取 `ListToolsResult.next_cursor`。只要 cursor 非空，就构造 `PaginatedRequestParams(cursor=...)` 请求下一页，直到 cursor 为 None。单元测试使用两页响应验证实际请求顺序为 `[None, "page-2"]`。

## 16. MCP Result Normalization

`CallToolResult` 被转换为：

```json
{
  "structured_content": {},
  "content": [
    {"type": "text", "text": "..."},
    {"type": "image", "data": "...", "mime_type": "image/png"}
  ],
  "is_error": false
}
```

每个 Pydantic content block 使用 `model_dump(mode="json")`，不假设都是 TextContent。structured_content 同样转换为 JSON-compatible 值。

## 17. MCP is_error Handling

Adapter 先规范化结果，再检查 `result.is_error`。为 true 时抛 `MCPToolExecutionError`，其中保留规范化 error result。

Executor 沿用普通 Tool exception 路径：

```text
MCP is_error=true
→ MCPToolExecutionError
→ ToolCallRecord.status=failed
→ ToolMessage.status=error
→ model may choose another action
→ Task is not immediately failed
```

transport/session Python exception 则包装为脱敏的 `MCPTransportError`。本阶段不自动 reconnect。

## 18. MCP Metadata Preservation

Adapter.metadata 原样保留当前 SDK Tool 实际暴露的：

- title；
- annotations（含 read_only_hint、destructive_hint、idempotent_hint、open_world_hint）；
- output_schema；
- icons；
- execution；
- meta。

Phase 6 不根据 annotations 阻止调用，也没有提前实现 HITL/RiskPolicy。

## 19. Local + MCP Tool Coexistence

实际测试 Registry 同时注册 `local_calculator` 和 `demo__echo`；Function Calling schemas 同时包含二者。Executor 只依赖统一 Tool Protocol/Registry，不知道 Tool 是 Local 还是 MCP。

命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q -s tests/test_mcp.py::test_local_and_mcp_tools_coexist_in_function_schemas
```

实际输出：

```text
LOCAL_MCP_COEXISTENCE=local_calculator,demo__echo
.
1 passed in 0.66s
```

## 20. Full Relevant Tests

### tests/test_tools.py

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

    async def invoke(self, arguments: dict[str, Any]) -> Any:
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

    async def invoke(self, arguments: dict[str, Any]) -> Any:
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


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(UnknownToolError, match="未注册的工具: missing"):
        await ToolRegistry().invoke("missing", {})


@pytest.mark.asyncio
async def test_arguments_are_validated_before_invocation() -> None:
    registry = ToolRegistry()
    registry.register(LookupTool())

    with pytest.raises(ToolArgumentsError, match="参数不符合 input_schema"):
        await registry.invoke("lookup", {"unexpected": 1})


@pytest.mark.asyncio
async def test_tool_returns_result_and_function_schema() -> None:
    registry = ToolRegistry()
    registry.register(LookupTool())

    result = await registry.invoke("lookup", {"query": "gym"})
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


@pytest.mark.asyncio
async def test_tool_exception_is_not_swallowed() -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())

    with pytest.raises(RuntimeError, match="offline tool failure"):
        await registry.invoke("failing", {})
```

### tests/test_executor.py

```python
"""Phase 4 Executor Function Calling Runtime 的离线测试。"""

import json
from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
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

    async def invoke(self, arguments: dict[str, Any]) -> Any:
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

    async def invoke(self, arguments: dict[str, Any]) -> Any:
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

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
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


@pytest.mark.asyncio
async def test_executor_runs_tool_then_finish_step_with_linked_tool_message() -> None:
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

    result = await executor.execute(
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


@pytest.mark.asyncio
async def test_tool_failure_becomes_observation_and_loop_can_continue() -> None:
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

    result = await executor.execute(
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


@pytest.mark.asyncio
async def test_tool_error_does_not_make_graph_task_failed() -> None:
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

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
    ).ainvoke(make_initial_state())

    assert result["status"] is TaskStatus.COMPLETED
    assert result["tool_calls"][0].status is ToolCallStatus.FAILED
    assert result["tool_calls"][1].status is ToolCallStatus.SUCCESS
    assert result["step_outcome"] is None
    assert result["step_results"][0].summary == (
        "Current step has enough offline evidence."
    )
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["error"] is None


@pytest.mark.asyncio
async def test_action_limit_returns_incomplete_outcome() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("lookup", {"query": "gym"}, "limit-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
        max_actions_per_step=1,
    )

    result = await executor.execute(
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


@pytest.mark.asyncio
async def test_multiple_tool_calls_are_rejected() -> None:
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

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert "实际数量: 2" in result.outcome.error
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_unknown_tool_call_returns_clear_error() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("missing", {"query": "gym"}, "unknown-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert result.outcome.error == "未注册的工具: missing"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_invalid_tool_arguments_return_clear_error() -> None:
    model = FakeFunctionCallingModel(
        [tool_call("lookup", {}, "invalid-1")]
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.fatal_error is True
    assert "参数不符合 input_schema" in result.outcome.error
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_missing_action_is_not_treated_as_completion() -> None:
    model = FakeFunctionCallingModel([AIMessage(content="I am done")])
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "实际数量: 0" in result.outcome.error


@pytest.mark.asyncio
async def test_finish_step_requires_nonempty_evidence() -> None:
    response = tool_call(
        FINISH_STEP_NAME,
        {"summary": "Done", "evidence": [], "output": {}},
        "empty-evidence-1",
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "finish_step 参数无效" in result.outcome.error


@pytest.mark.asyncio
async def test_finish_step_requires_structured_output_object() -> None:
    response = tool_call(
        FINISH_STEP_NAME,
        {"summary": "Done", "evidence": ["Evidence"]},
        "missing-output-1",
    )
    executor = TaskExecutor(
        registry=make_registry(LookupTool()),
        model=cast(BaseChatModel, FakeFunctionCallingModel([response])),
    )

    result = await executor.execute(
        task_spec=make_task_spec(),
        step=make_step(),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome.claimed_complete is False
    assert result.fatal_error is True
    assert "output" in result.outcome.error


@pytest.mark.asyncio
async def test_empty_registry_reports_execution_unavailable_without_model_call() -> None:
    model = FakeFunctionCallingModel([])
    executor = TaskExecutor(
        registry=make_registry(),
        model=cast(BaseChatModel, model),
    )

    result = await executor.execute(
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

    async def execute(
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


@pytest.mark.asyncio
async def test_graph_verifies_and_completes_single_step() -> None:
    executor = FakeExecutor()
    graph = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
    )

    result = await graph.ainvoke(make_initial_state())

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

    async def execute(
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


@pytest.mark.asyncio
async def test_multi_step_graph_hands_off_verified_result_and_completes() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([True, True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=TwoStepPlanner(),
        executor=executor,
        verifier=verifier,
    ).ainvoke(make_initial_state())

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


@pytest.mark.asyncio
async def test_rejection_retries_same_step_with_feedback_then_advances() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([False, True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=executor,
        verifier=verifier,
        max_step_attempts=3,
    ).ainvoke(make_initial_state())

    assert executor.call_count_by_step[1] == 2
    assert result["plan"][0].retry_count == 1
    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert executor.received_feedback[0] == (1, None)
    retry_feedback = executor.received_feedback[1][1]
    assert retry_feedback is not None
    assert retry_feedback.verified is False
    assert retry_feedback.feedback == "请补充可核验的候选证据"
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeated_rejection_stops_at_retry_bound() -> None:
    executor = RecordingExecutor()
    verifier = SequencedVerifier([False, False, False, True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=executor,
        verifier=verifier,
        max_step_attempts=3,
    ).ainvoke(make_initial_state())

    assert executor.call_count_by_step[1] == 3
    assert verifier.step_call_count == 3
    assert verifier.task_call_count == 0
    assert result["plan"][0].retry_count == 3
    assert result["plan"][0].status is PlanStepStatus.FAILED
    assert result["status"] is TaskStatus.FAILED
    assert "max_step_attempts=3" in result["error"]


@pytest.mark.asyncio
async def test_task_verifier_rejection_fails_task_after_steps_complete() -> None:
    verifier = SequencedVerifier(
        [True],
        task_result=VerificationResult(
            completed=False,
            reason="最终格式仍缺失",
            missing_requirements=["CSV 输出"],
            next_action="replan",
        ),
    )

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(),
        verifier=verifier,
    ).ainvoke(make_initial_state())

    assert result["plan"][0].status is PlanStepStatus.COMPLETED
    assert result["verification"].completed is False
    assert result["status"] is TaskStatus.FAILED
    assert "next_action=replan" in result["error"]


@pytest.mark.asyncio
async def test_inconsistent_task_verification_fails_validation() -> None:
    verifier = SequencedVerifier(
        [True],
        task_result=VerificationResult(
            completed=True,
            reason="矛盾",
            missing_requirements=["仍缺内容"],
        ),
    )

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(),
        verifier=verifier,
    ).ainvoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert "missing_requirements 必须为空" in result["error"]


@pytest.mark.asyncio
async def test_fatal_executor_error_bypasses_step_and_task_verifier() -> None:
    verifier = SequencedVerifier([True])

    result = await build_graph(
        analyzer=FakeAnalyzer(),
        planner=OneStepPlanner(),
        executor=RecordingExecutor(fatal=True),
        verifier=verifier,
    ).ainvoke(make_initial_state())

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

### tests/test_mcp.py

```python
"""MCP config、namespace、归一化和 transport lifecycle 的离线测试。"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, cast

import pytest
from mcp import ClientSession
from mcp.types import (
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool as RemoteTool,
    ToolAnnotations,
)
from pydantic import ValidationError

import taskpilot.mcp.provider as provider_module
from taskpilot.mcp import (
    MCPConnectionError,
    MCPConfigurationError,
    MCPServerConfig,
    MCPToolAdapter,
    MCPToolExecutionError,
    MCPToolProvider,
    build_public_tool_name,
    normalize_call_tool_result,
)
from taskpilot.tools import ToolRegistry


def test_stdio_and_streamable_http_config_validation() -> None:
    stdio = MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command="python",
        args=["server.py"],
    )
    http = MCPServerConfig(
        server_id="remote",
        transport="streamable_http",
        url="http://127.0.0.1:8765/mcp",
    )

    assert stdio.command == "python"
    assert http.url == "http://127.0.0.1:8765/mcp"

    with pytest.raises(ValidationError, match="必须提供 command"):
        MCPServerConfig(server_id="bad", transport="stdio")
    with pytest.raises(ValidationError, match=r"必须提供 http\(s\) url"):
        MCPServerConfig(server_id="bad", transport="streamable_http")
    with pytest.raises(ValidationError, match="不能提供 command"):
        MCPServerConfig(
            server_id="bad",
            transport="streamable_http",
            url="https://example.invalid/mcp",
            command="python",
        )


def test_stdio_env_is_explicit_and_secret_in_dumps() -> None:
    config = MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command="python",
        env={"DEMO_TOKEN": "phase-6-secret"},
    )

    assert config.resolved_env() == {"DEMO_TOKEN": "phase-6-secret"}
    assert "phase-6-secret" not in repr(config)
    assert "phase-6-secret" not in str(config.model_dump(mode="json"))
    assert "LLM_API_KEY" not in config.resolved_env()


def test_public_tool_name_is_namespaced_safe_and_deterministic() -> None:
    first = build_public_tool_name("web server", "search.tools/v1")
    second = build_public_tool_name("web server", "search.tools/v1")
    long_name = build_public_tool_name("server" * 20, "tool" * 30)

    assert first == "web_server__search_tools_v1"
    assert second == first
    assert build_public_tool_name("web-a", "search") != (
        build_public_tool_name("web-b", "search")
    )
    assert len(long_name) == 64
    assert all(character.isalnum() or character in "_-" for character in first)


def test_provider_rejects_duplicate_and_sanitized_server_ids() -> None:
    duplicate = [
        MCPServerConfig(server_id="web", transport="stdio", command="python"),
        MCPServerConfig(server_id="web", transport="stdio", command="python"),
    ]
    collision = [
        MCPServerConfig(server_id="web.one", transport="stdio", command="python"),
        MCPServerConfig(server_id="web_one", transport="stdio", command="python"),
    ]

    with pytest.raises(MCPConfigurationError, match="server_id 必须唯一"):
        MCPToolProvider(duplicate)
    with pytest.raises(MCPConfigurationError, match="namespace 冲突"):
        MCPToolProvider(collision)


def test_result_normalization_preserves_multiple_content_types() -> None:
    result = CallToolResult(
        structuredContent={"answer": 3},
        content=[
            TextContent(text="three"),
            ImageContent(data="aW1hZ2U=", mimeType="image/png"),
        ],
    )

    normalized = normalize_call_tool_result(result)

    assert normalized["structured_content"] == {"answer": 3}
    assert normalized["content"][0] == {
        "type": "text",
        "text": "three",
        "annotations": None,
        "meta": None,
    }
    assert normalized["content"][1]["type"] == "image"
    assert normalized["content"][1]["mime_type"] == "image/png"
    assert normalized["is_error"] is False


class FakeCallSession:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        return self.result


@pytest.mark.asyncio
async def test_adapter_calls_remote_name_not_public_namespace() -> None:
    session = FakeCallSession(
        CallToolResult(
            content=[TextContent(text="hello")],
            structuredContent={"echoed": "hello"},
        )
    )
    adapter = MCPToolAdapter(
        name="demo__echo",
        remote_tool_name="echo",
        server_id="demo",
        description="Echo",
        input_schema={"type": "object"},
        session=cast(ClientSession, session),
    )

    result = await adapter.invoke({"text": "hello"})

    assert session.calls == [("echo", {"text": "hello"})]
    assert result["structured_content"] == {"echoed": "hello"}


@pytest.mark.asyncio
async def test_adapter_raises_for_mcp_is_error_result() -> None:
    session = FakeCallSession(
        CallToolResult(
            content=[TextContent(text="remote failure detail")],
            isError=True,
        )
    )
    adapter = MCPToolAdapter(
        name="demo__fail",
        remote_tool_name="fail",
        server_id="demo",
        description="Fail",
        input_schema={"type": "object"},
        session=cast(ClientSession, session),
    )

    with pytest.raises(MCPToolExecutionError, match="is_error=true") as error:
        await adapter.invoke({})

    assert error.value.result["is_error"] is True
    assert "remote failure detail" in str(error.value)


class FakePaginatedSession:
    def __init__(self) -> None:
        self.cursors: list[str | None] = []

    async def list_tools(self, *, params=None) -> ListToolsResult:
        cursor = None if params is None else params.cursor
        self.cursors.append(cursor)
        if cursor is None:
            return ListToolsResult(
                tools=[
                    RemoteTool(
                        name="first",
                        description="First",
                        inputSchema={"type": "object"},
                    )
                ],
                nextCursor="page-2",
            )
        return ListToolsResult(
            tools=[
                RemoteTool(
                    name="second",
                    description="Second",
                    inputSchema={"type": "object"},
                )
            ]
        )


@pytest.mark.asyncio
async def test_list_tools_follows_all_pagination_cursors() -> None:
    session = FakePaginatedSession()

    tools = await MCPToolProvider._list_all_tools(cast(ClientSession, session))

    assert [tool.name for tool in tools] == ["first", "second"]
    assert session.cursors == [None, "page-2"]


class LocalAsyncTool:
    name = "local_calculator"
    description = "Local async calculator"
    input_schema = {"type": "object", "additionalProperties": False}

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return {"local": True}


def test_local_and_mcp_tools_coexist_in_function_schemas() -> None:
    adapter = MCPToolAdapter(
        name="demo__echo",
        remote_tool_name="echo",
        server_id="demo",
        description="MCP echo",
        input_schema={"type": "object"},
        session=cast(ClientSession, FakeCallSession(CallToolResult(content=[]))),
    )
    registry = ToolRegistry()
    registry.register(LocalAsyncTool())
    registry.register(adapter)

    names = [
        schema["function"]["name"]
        for schema in registry.list_function_schemas()
    ]

    assert names == ["local_calculator", "demo__echo"]
    print("LOCAL_MCP_COEXISTENCE=" + ",".join(names))


@pytest.mark.asyncio
async def test_connection_error_redacts_explicit_stdio_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def failing_transport(parameters) -> AsyncIterator[tuple[str, str]]:
        raise RuntimeError(f"spawn rejected token={parameters.env['TOKEN']}")
        yield ("unreachable", "unreachable")  # pragma: no cover

    monkeypatch.setattr(provider_module, "stdio_client", failing_transport)
    config = MCPServerConfig(
        server_id="broken",
        transport="stdio",
        command="missing",
        env={"TOKEN": "private-mcp-token"},
    )

    with pytest.raises(MCPConnectionError) as error:
        async with MCPToolProvider([config]):
            pass

    assert "private-mcp-token" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


class FakeHTTPClientSession:
    instances: list["FakeHTTPClientSession"] = []

    def __init__(self, *streams: Any) -> None:
        self.streams = streams
        self.initialized = False
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> "FakeHTTPClientSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True

    async def initialize(self) -> None:
        self.initialized = True


@pytest.mark.asyncio
async def test_streamable_http_transport_lifecycle_is_constructed_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []
    transport_closed = False

    @asynccontextmanager
    async def fake_http_transport(url: str) -> AsyncIterator[tuple[str, str]]:
        nonlocal transport_closed
        opened_urls.append(url)
        try:
            yield ("read-stream", "write-stream")
        finally:
            transport_closed = True

    FakeHTTPClientSession.instances.clear()
    monkeypatch.setattr(
        provider_module,
        "streamable_http_client",
        fake_http_transport,
    )
    monkeypatch.setattr(provider_module, "ClientSession", FakeHTTPClientSession)
    config = MCPServerConfig(
        server_id="http-demo",
        transport="streamable_http",
        url="http://127.0.0.1:8765/mcp",
    )

    async with MCPToolProvider([config]) as provider:
        assert provider.connected_server_ids == ["http-demo"]
        session = FakeHTTPClientSession.instances[0]
        assert session.streams == ("read-stream", "write-stream")
        assert session.initialized is True
        assert session.closed is False

    assert opened_urls == ["http://127.0.0.1:8765/mcp"]
    assert session.closed is True
    assert transport_closed is True
    assert provider.connected_server_ids == []


def test_mcp_config_file_loader() -> None:
    config_path = Path(__file__).parent / "fixtures" / "mcp_config.json"

    from taskpilot.mcp.config import load_mcp_server_configs

    configs = load_mcp_server_configs(config_path)

    assert len(configs) == 1
    assert configs[0].server_id == "demo"
```

### tests/test_mcp_integration.py

```python
"""官方 MCP SDK + 真实本地 STDIO subprocess 的 Phase 6 集成测试。"""

import json
import sys
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.mcp import (
    MCPServerConfig,
    MCPToolExecutionError,
    MCPToolProvider,
)
from taskpilot.models import PlanStep, TaskSpec, ToolCallStatus
from taskpilot.tools import ToolRegistry


SERVER_PATH = Path(__file__).parent / "fixtures" / "mcp_server.py"


def make_stdio_config() -> MCPServerConfig:
    """使用当前 pytest 解释器启动同一环境中的官方 MCP Server。"""

    return MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command=sys.executable,
        args=["-u", str(SERVER_PATH)],
    )


@pytest.mark.asyncio
async def test_real_stdio_discovery_registration_call_and_shutdown() -> None:
    registry = ToolRegistry()
    provider = MCPToolProvider([make_stdio_config()])

    async with provider:
        adapters = await provider.register_tools(registry)
        discovered = {
            item.remote_tool_name: item.public_tool_name
            for item in provider.discovery_info
        }

        assert provider.connected_server_ids == ["demo"]
        assert set(discovered) == {"echo", "add", "fail_tool"}
        assert discovered["echo"] == "demo__echo"
        assert discovered["add"] == "demo__add"
        assert {adapter.name for adapter in adapters} == set(discovered.values())

        schemas = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in registry.list_function_schemas()
        }
        assert schemas["demo__echo"]["required"] == ["text"]
        assert set(schemas["demo__add"]["required"]) == {"a", "b"}

        result = await registry.invoke("demo__add", {"a": 2, "b": 3})
        assert result["structured_content"] == {"sum": 5}
        assert result["is_error"] is False

        echo_adapter = next(item for item in adapters if item.name == "demo__echo")
        assert echo_adapter.remote_tool_name == "echo"
        assert echo_adapter.server_id == "demo"
        assert echo_adapter.metadata["annotations"]["read_only_hint"] is True
        assert echo_adapter.metadata["annotations"]["destructive_hint"] is False
        print(
            "STDIO_DISCOVERY_SMOKE="
            + json.dumps(
                {
                    "connected": provider.connected_server_ids,
                    "discovered": discovered,
                    "add_required": schemas["demo__add"]["required"],
                    "add_result": result,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )

    assert provider.connected_server_ids == []


@pytest.mark.asyncio
async def test_real_stdio_mcp_is_error_is_not_tool_success() -> None:
    registry = ToolRegistry()

    async with MCPToolProvider([make_stdio_config()]) as provider:
        await provider.register_tools(registry)

        with pytest.raises(MCPToolExecutionError) as error:
            await registry.invoke("demo__fail_tool", {})

        assert error.value.result["is_error"] is True
        assert "intentional MCP failure" in str(error.value)
        print(
            "MCP_ERROR_SMOKE="
            + json.dumps(
                {
                    "public_tool_name": "demo__fail_tool",
                    "raised": type(error.value).__name__,
                    "is_error": error.value.result["is_error"],
                    "content": error.value.result["content"],
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )


class FakeAsyncFunctionCallingModel:
    """给真实 async Executor 提供可预测的原生 Tool Calls。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tools: list[dict[str, Any]] = []
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeAsyncFunctionCallingModel":
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
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


def finish_call(call_id: str) -> AIMessage:
    return tool_call(
        FINISH_STEP_NAME,
        {
            "summary": "The real MCP echo result was observed.",
            "evidence": ["demo__echo returned structured_content"],
            "output": {"echoed": "hello-mcp"},
        },
        call_id,
    )


def make_step() -> PlanStep:
    return PlanStep(
        id=1,
        description="Echo a value through MCP",
        success_criteria=["The echoed value is observed"],
    )


@pytest.mark.asyncio
async def test_executor_registry_real_mcp_end_to_end() -> None:
    registry = ToolRegistry()
    model = FakeAsyncFunctionCallingModel(
        [
            tool_call("demo__echo", {"text": "hello-mcp"}, "mcp-call-1"),
            finish_call("finish-call-2"),
        ]
    )

    async with MCPToolProvider([make_stdio_config()]) as provider:
        await provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
        )
        result = await executor.execute(
            task_spec=TaskSpec(goal="Echo through real MCP"),
            step=make_step(),
            task_results={},
            tool_calls=[],
        )

    assert result.outcome.claimed_complete is True
    assert result.outcome.output == {"echoed": "hello-mcp"}
    assert result.fatal_error is False
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "mcp-call-1"
    assert result.tool_calls[0].tool_name == "demo__echo"
    assert result.tool_calls[0].status is ToolCallStatus.SUCCESS
    assert result.tool_calls[0].result["structured_content"] == {
        "echoed": "hello-mcp"
    }

    second_round = model.invocations[1]
    tool_message = second_round[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "mcp-call-1"
    assert tool_message.status == "success"
    observation = json.loads(str(tool_message.content))
    assert observation["result"]["structured_content"] == {
        "echoed": "hello-mcp"
    }
    print(
        "EXECUTOR_MCP_SMOKE="
        + json.dumps(
            {
                "tool_name": result.tool_calls[0].tool_name,
                "call_id": result.tool_calls[0].call_id,
                "record_status": result.tool_calls[0].status.value,
                "tool_message_call_id": tool_message.tool_call_id,
                "tool_message_status": tool_message.status,
                "structured_content": observation["result"][
                    "structured_content"
                ],
                "claimed_complete": result.outcome.claimed_complete,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_mcp_error_becomes_failed_record_and_executor_continues() -> None:
    registry = ToolRegistry()
    model = FakeAsyncFunctionCallingModel(
        [
            tool_call("demo__fail_tool", {}, "mcp-fail-1"),
            tool_call("demo__echo", {"text": "hello-mcp"}, "mcp-echo-2"),
            finish_call("finish-after-error-3"),
        ]
    )

    async with MCPToolProvider([make_stdio_config()]) as provider:
        await provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
        )
        result = await executor.execute(
            task_spec=TaskSpec(goal="Recover from one MCP tool error"),
            step=make_step(),
            task_results={},
            tool_calls=[],
        )

    assert result.outcome.claimed_complete is True
    assert result.fatal_error is False
    assert [record.status for record in result.tool_calls] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCESS,
    ]
    assert "is_error=true" in result.tool_calls[0].error
    assert "intentional MCP failure" in result.tool_calls[0].error
    error_message = model.invocations[1][-1]
    assert isinstance(error_message, ToolMessage)
    assert error_message.tool_call_id == "mcp-fail-1"
    assert error_message.status == "error"
    assert "intentional MCP failure" in str(error_message.content)
    print(
        "EXECUTOR_MCP_ERROR_RECOVERY_SMOKE="
        + json.dumps(
            {
                "record_statuses": [
                    record.status.value for record in result.tool_calls
                ],
                "error_tool_message_status": error_message.status,
                "executor_continued": result.outcome.claimed_complete,
                "fatal_error": result.fatal_error,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
```

### tests/fixtures/mcp_server.py

```python
"""Phase 6 离线集成测试使用的最小官方 MCP v2 STDIO Server。"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


server = MCPServer("taskpilot-phase-6-demo")


@server.tool(
    description="Echo text through the real local MCP subprocess.",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    structured_output=True,
)
def echo(text: str) -> dict[str, str]:
    """返回输入文本，证明请求确实到达独立 MCP Server。"""

    return {"echoed": text}


@server.tool(
    description="Add two integers in the real local MCP subprocess.",
    structured_output=True,
)
def add(a: int, b: int) -> dict[str, int]:
    """返回两个整数的和。"""

    return {"sum": a + b}


@server.tool(description="Always fail to exercise MCP is_error handling.")
def fail_tool() -> str:
    """故意抛错；官方 Server 会转换为 is_error=true。"""

    raise ToolError("intentional MCP failure")


if __name__ == "__main__":
    server.run(transport="stdio")
```

### tests/fixtures/mcp_config.json

```json
{
  "servers": [
    {
      "server_id": "demo",
      "transport": "stdio",
      "command": "python",
      "args": ["server.py"]
    }
  ]
}
```

## 21. Pytest Actual Result

命令：

```powershell
conda run -n ai python -m pytest -q
```

实际输出：

```text
........................................................................ [ 82%]
...............                                                          [100%]
87 passed in 3.79s
```

结果：87 passed，0 failed。测试不访问互联网、不使用 API Key、不启动 Browser。

## 22. 真实 STDIO Discovery Smoke

命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q -s tests/test_mcp_integration.py::test_real_stdio_discovery_registration_call_and_shutdown
```

实际输出：

```text
STDIO_DISCOVERY_SMOKE={"add_required": ["a", "b"], "add_result": {"content": [{"annotations": null, "meta": null, "text": "{\n  \"sum\": 5\n}", "type": "text"}], "is_error": false, "structured_content": {"sum": 5}}, "connected": ["demo"], "discovered": {"add": "demo__add", "echo": "demo__echo", "fail_tool": "demo__fail_tool"}}
.
1 passed in 2.09s
```

这条测试真实启动独立官方 MCPServer subprocess，完成 initialize、动态 list_tools、schema 检查、adapter 注册、远端 add 调用和 context 退出关闭。

## 23. 真实 MCP Call Smoke

第 22 节输出中的 `add_result` 来自：

```text
TaskPilot ToolRegistry
→ MCPToolAdapter(name="demo__add", remote_tool_name="add")
→ ClientSession.call_tool("add", {"a": 2, "b": 3})
→ real MCP subprocess
→ structured_content {"sum": 5}
```

客户端调用前只通过 discovery 获得工具和 schema，没有在 Provider/Registry 中硬编码 add。

## 24. Executor → MCP End-to-End Smoke

命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q -s tests/test_mcp_integration.py::test_executor_registry_real_mcp_end_to_end
```

实际输出：

```text
EXECUTOR_MCP_SMOKE={"call_id": "mcp-call-1", "claimed_complete": true, "record_status": "success", "structured_content": {"echoed": "hello-mcp"}, "tool_message_call_id": "mcp-call-1", "tool_message_status": "success", "tool_name": "demo__echo"}
.
1 passed in 2.06s
```

真实链路是 Fake Function Calling Model → async TaskExecutor → ToolRegistry → MCPToolAdapter → ClientSession → real STDIO MCPServer → normalized Observation → ToolMessage → finish_step。call_id 在 AI tool call、ToolCallRecord 和 ToolMessage 中一致。

## 25. MCP Error Smoke

### Adapter is_error

命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q -s tests/test_mcp_integration.py::test_real_stdio_mcp_is_error_is_not_tool_success
```

实际输出：

```text
[09/01/26 11:56:54] INFO     Tool 'fail_tool' failed: 'Error      server.py:438
                             executing tool fail_tool:                         
                             intentional MCP failure'                          
MCP_ERROR_SMOKE={"content": [{"annotations": null, "meta": null, "text": "Error executing tool fail_tool: intentional MCP failure", "type": "text"}], "is_error": true, "public_tool_name": "demo__fail_tool", "raised": "MCPToolExecutionError"}
.
1 passed in 2.08s
```

### Executor recovery

命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q -s tests/test_mcp_integration.py::test_mcp_error_becomes_failed_record_and_executor_continues
```

实际输出：

```text
[09/01/26 11:56:54] INFO     Tool 'fail_tool' failed: 'Error      server.py:438
                             executing tool fail_tool:                         
                             intentional MCP failure'                          
EXECUTOR_MCP_ERROR_RECOVERY_SMOKE={"error_tool_message_status": "error", "executor_continued": true, "fatal_error": false, "record_statuses": ["failed", "success"]}
.
1 passed in 2.00s
```

MCP error 是 failed Tool record，不是 success，也不直接使 Task/Step fatal。Fake model 下一轮调用 echo 成功，随后 finish_step。

## 26. Known Limitations

- 没有自动 reconnect、session recovery 或 retry transport policy。
- Streamable HTTP 有真实 SDK lifecycle 代码和离线 construction/lifecycle 测试，但没有真实 HTTP server integration test。
- HTTP auth/header policy 尚未实现；配置中没有硬编码 token。
- MCP v2 的 input-required/elicitation 结果当前作为不支持的结果类型失败；HITL 属于后续阶段。
- Tool annotations 只保存，不执行 risk policy。
- 没有 MCP Tool schema 版本缓存或动态刷新；Provider lifecycle 内首次发现后缓存 adapters。
- Graph 仍是串行 Step 调度，没有 Dynamic Replanning。
- 没有 Playwright/Browser、真实 Search、production Filesystem Agent。
- 没有 Checkpoint/Recovery、Trace persistence、Vision 或 Benchmark execution。

## 27. Files Changed

Phase 6 新建：

- `src/taskpilot/mcp/__init__.py`：MCP 基础设施公开入口。
- `src/taskpilot/mcp/config.py`：双 transport 安全配置。
- `src/taskpilot/mcp/adapter.py`：namespace、result normalization 与 async adapter。
- `src/taskpilot/mcp/provider.py`：多 Server 长生命周期、分页发现和注册。
- `tests/fixtures/mcp_server.py`：官方 MCP v2 真实本地 STDIO Server。
- `tests/fixtures/mcp_config.json`：CLI config loader fixture。
- `tests/test_mcp.py`：config、namespace、pagination、normalization、HTTP lifecycle、coexistence 测试。
- `tests/test_mcp_integration.py`：真实 STDIO discovery/call/Executor/error tests。
- `docs/review/phase_06.md`：本验收文档。

Phase 6 修改：

- `src/taskpilot/tools/base.py`：唯一 Tool Protocol 改为 async。
- `src/taskpilot/tools/registry.py`：invocation 改为 await。
- `src/taskpilot/executor.py`：async execute、model.ainvoke、async registry call。
- `src/taskpilot/graph.py`：execute_step async。
- `src/taskpilot/cli.py`：async Host、可选 MCP JSON 和 provider lifecycle。
- `tests/test_tools.py`、`tests/test_executor.py`、`tests/test_analyzer.py`、`tests/test_graph_smoke.py`、`tests/test_planner.py`、`tests/test_phase5_graph.py`：迁移 async，保留旧语义断言。
- `pyproject.toml`：新增官方 `mcp>=2.1,<3` 和单一 async 测试框架 `pytest-asyncio>=0.23`。
- `README.md`：记录 Phase 6 实际能力、Host layering 和边界。

没有删除或 reset 既有 Phase 3–5 用户改动，也没有 commit/push。

## 28. git status --short

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
?? docs/review/phase_06.md
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/tools/
?? src/taskpilot/verifier.py
?? tests/fixtures/
?? tests/test_executor.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase5_graph.py
?? tests/test_tools.py
?? tests/test_verifier.py
```

Git 的全局 ignore permission warning 来自主机配置；不影响仓库内状态记录。

## 29. Installed MCP SDK Version

检查命令：

```powershell
conda run -n ai python -c "import importlib.metadata as m,inspect; from mcp import ClientSession,StdioServerParameters; from mcp.client.stdio import stdio_client; from mcp.client.streamable_http import streamable_http_client; print('mcp='+m.version('mcp')); print('ClientSession='+str(inspect.signature(ClientSession))); print('initialize='+str(inspect.signature(ClientSession.initialize))); print('list_tools='+str(inspect.signature(ClientSession.list_tools))); print('call_tool='+str(inspect.signature(ClientSession.call_tool))); print('StdioServerParameters='+str(inspect.signature(StdioServerParameters))); print('stdio_client='+str(inspect.signature(stdio_client))); print('streamable_http_client='+str(inspect.signature(streamable_http_client)))"
```

实际输出：

```text
mcp=2.1.1
ClientSession=(read_stream: 'ReadStream[SessionMessage | Exception] | None' = None, write_stream: 'WriteStream[SessionMessage] | None' = None, read_timeout_seconds: 'float | None' = None, sampling_callback: 'SamplingFnT | None' = None, elicitation_callback: 'ElicitationFnT | None' = None, list_roots_callback: 'ListRootsFnT | None' = None, logging_callback: 'LoggingFnT | None' = None, message_handler: 'MessageHandlerFnT | None' = None, client_info: 'types.Implementation | None' = None, *, log_level: 'types.LoggingLevel | None' = None, sampling_capabilities: 'types.SamplingCapability | None' = None, extensions: 'dict[str, dict[str, Any]] | None' = None, result_claims: 'Mapping[str, Sequence[ResultClaim[Any]]] | None' = None, notification_bindings: 'Sequence[NotificationBinding[Any]] | None' = None, dispatcher: 'Dispatcher[Any] | None' = None) -> 'None'
initialize=(self) -> 'types.InitializeResult'
list_tools=(self, *, params: 'types.PaginatedRequestParams | None' = None) -> 'types.ListToolsResult'
call_tool=(self, name: 'str', arguments: 'dict[str, Any] | None' = None, read_timeout_seconds: 'float | None' = None, progress_callback: 'ProgressFnT | None' = None, *, input_responses: 'types.InputResponses | None' = None, request_state: 'str | None' = None, meta: 'RequestParamsMeta | None' = None, allow_input_required: 'bool' = False, allow_claimed: 'bool' = False) -> 'types.CallToolResult | types.InputRequiredResult | types.Result'
StdioServerParameters=(*, command: str, args: list[str] = <factory>, env: dict[str, str] | None = None, cwd: str | pathlib.Path | None = None, encoding: str = 'utf-8', encoding_error_handler: Literal['strict', 'ignore', 'replace'] = 'strict') -> None
stdio_client=(server: mcp.client.stdio.StdioServerParameters, errlog: <class 'TextIO'> = <_io.TextIOWrapper name='<stderr>' mode='w' encoding='utf-8'>) -> collections.abc.AsyncGenerator[tuple[mcp.shared._stream_protocols.ReadStream[mcp.shared.message.SessionMessage | Exception], mcp.shared._stream_protocols.WriteStream[mcp.shared.message.SessionMessage]], None]
streamable_http_client=(url: 'str', *, http_client: 'httpx2.AsyncClient | None' = None, terminate_on_close: 'bool' = True) -> 'AsyncGenerator[TransportStreams, None]'
```

实际使用 `mcp 2.1.1`。v2 已把旧 FastMCP 改名为 `mcp.server.mcpserver.MCPServer`；本实现没有导入旧 FastMCP，也没有使用 deprecated SSE。

## 30. Boundary Checks

### Q1. MCP Tool 是不是在客户端硬编码的？

No。生产客户端只调用真实 `session.list_tools()`；echo/add/fail_tool 只定义在测试 Server，Provider 动态读取。

### Q2. Executor 是否知道某 Tool 来自 MCP？

No。Executor 只看统一 async Tool Protocol 和 ToolRegistry。MCPToolAdapter 对它就是普通 Tool。

### Q3. 两个 MCP Server 都存在 search 时怎么办？

public names 分别为 `server_a__search` 和 `server_b__search`。清理或截断后若仍碰撞，Provider 明确抛 MCPConfigurationError，不静默覆盖。

### Q4. MCP 的 is_error=true 是否被记录成 Tool success？

No。Adapter 抛 MCPToolExecutionError；Executor 写 failed ToolCallRecord 和 error ToolMessage。

### Q5. 每次 MCP Tool Call 是否重新连接 Server？

No。AsyncExitStack 在 Provider context 入口统一连接和 initialize；整个 Graph/Executor 运行期间复用 session，退出才关闭。

### Q6. MCP 是否被错误地做成 LangGraph node？

No。层级是 Host/CLI → MCPToolProvider → ToolRegistry → TaskExecutor → build_graph。Graph 没有 MCP node。

### Q7. 为什么 Tool Runtime 改成 async？

真实外部工具是 I/O-bound，MCP 和未来 Playwright 都有 async lifecycle。统一 async 避免每次调用 asyncio.run、event-loop 嵌套和阻塞设计。

### Q8. 是否真的通过官方 MCP SDK 跑过真实 STDIO Server？

Yes。第 22–25 节记录真实命令和 subprocess 输出；使用官方 MCPServer、stdio_client、ClientSession.initialize/list_tools/call_tool。

### Q9. TaskPilot 是否同时支持 Local Tool 和 MCP Tool？

Yes。第 19 节实际 Registry 输出为 `local_calculator,demo__echo`，二者同时生成 Function schemas。
