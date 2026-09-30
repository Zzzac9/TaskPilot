# TaskPilot Phase 3 Review

本文档记录 Phase 3（Initial Planner）完成时工作区中的真实实现，用于人工架构与代码验收。文档内容以当前源码、离线测试和实际 Graph smoke test 为依据。

## 1. Phase Goal

本阶段目标是把 Phase 2 生成的 `TaskSpec` 拆解为结构化、与具体工具无关的初始计划，并把计划写入 LangGraph 的 `TaskState`。

实际完成内容：

- 为 `PlanStep` 增加步骤级 `success_criteria`。
- 新增只包含 `steps` 的 `TaskPlan`。
- 新增 `TaskPlannerLike`、真实 `TaskPlanner` 和 Structured Output 调用。
- 新增轻量、确定性的 `validate_plan()`。
- 新增 `plan_task` 节点，Graph 扩展到 Initial Planner。
- `build_graph()` 支持同时注入 Analyzer 与 Planner。
- CLI 可以输出 `TaskSpec`、完整 Plan 和 `current_step_id`。
- Planner 失败时记录错误、清空计划，并对错误文本中的 API Key 脱敏。
- 增加完全离线的 Planner、Graph、Prompt boundary 和校验测试。

本阶段明确没有实现：Executor、Tool Calling、工具选择、MCP、Playwright、Verifier、Dynamic Replanning、HITL、Checkpoint / Recovery、SQLite、FastAPI、Vision、调度器、并行执行和 Benchmark execution。

## 2. Final Directory Tree

以下目录树排除了运行测试产生的缓存文件：

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
│       └── phase_03.md
├── src/
│   └── taskpilot/
│       ├── __init__.py
│       ├── analyzer.py
│       ├── cli.py
│       ├── config.py
│       ├── graph.py
│       ├── llm.py
│       ├── models.py
│       ├── planner.py
│       └── state.py
└── tests/
    ├── test_analyzer.py
    ├── test_graph_smoke.py
    ├── test_models.py
    └── test_planner.py
```

## 3. Architecture Flow

当前实际编译并运行的 Graph：

```text
START
→ initialize_task
→ analyze_task
→ plan_task
→ END
```

- `initialize_task`：仅在任务状态为 `created` 时把状态推进为 `running`，不分析或执行任务。
- `analyze_task`：读取 `user_input`，调用注入的 `TaskAnalyzerLike`，并把结构化 `TaskSpec` 写入 `state.task_spec`。失败时写入 `state.error` 并把状态设为 `failed`。
- `plan_task`：读取 `state.task_spec`，调用注入的 `TaskPlannerLike`，把 `TaskPlan.steps` 写入 `state.plan`，并把第一个 `pending` Step 的 ID 写入 `current_step_id`。成功后任务仍保持 `running`。

`END` 只表示当前 Phase 3 Graph 已经运行到末端，不表示用户任务已经完成。当前没有 Executor 节点。

## 4. Data Model Changes

### PlanStep

`PlanStep` 当前字段：

- `id: int`：步骤标识。Planner Prompt 要求从 1 顺序递增，代码校验保证唯一，但目前没有执行器消费该 ID。
- `description: str`：当前步骤需要达成的任务级子目标。它不应包含具体函数调用、工具参数、URL、CSS selector 等执行细节。
- `status: PlanStepStatus`：步骤生命周期状态，默认 `pending`。当前 Initial Planner 生成并保存该值，尚未有 Executor 实际推进状态。
- `retry_count: int`：重试计数，默认 0。**当前仅保存，尚未用于实际执行控制。**
- `depends_on: list[int]`：当前步骤依赖的其他 Step ID。当前用于结构校验和表达先后关系，**尚未参与调度或实际执行控制。**
- `success_criteria: list[str]`：只描述当前 Step 在什么情况下算完成，而不是复制整个任务的所有 `completion_criteria`。当前要求非空，**尚未被 Verifier 实际检查。**

`depends_on` 和 `success_criteria` 均使用 `Field(default_factory=list)`，不同模型实例之间不会共享可变列表。

### TaskPlan

`TaskPlan` 只有一个字段：

- `steps: list[PlanStep]`：Initial Planner 的 Structured Output。没有 metadata、confidence、execution policy、estimated cost 或单独的 graph edge 对象。

真实 `TaskPlanner` 获得 Structured Output 后调用 `validate_plan()`；校验通过后才返回 `TaskPlan`。

### TaskState 中与 Plan 有关的字段

- `task_spec: TaskSpec | None`：Planner 的输入。缺失时 `plan_task` 失败，并且不会调用 Planner。
- `plan: list[PlanStep]`：保存 `TaskPlan.steps`，而不是嵌套保存整个 `TaskPlan`。
- `current_step_id: int | None`：规划成功后设置为第一个 `pending` Step 的 ID。当前只保存起始执行位置，尚未实际驱动 Executor。
- `status: TaskStatus`：规划成功仍为 `running`；Planner 失败变为 `failed`。
- `error: str | None`：记录 Analyzer 或 Planner 的清晰错误。

## 5. Full Relevant Source Code

以下代码块是生成本文档时当前工作区中的完整文件内容，不是 git diff。

### `src/taskpilot/models.py`

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

### `src/taskpilot/planner.py`

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
        return task_plan
```

### `src/taskpilot/graph.py`

```python
"""TaskPilot Phase 3 的 LangGraph 工作流。"""

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.models import PlanStepStatus, TaskStatus
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.state import TaskState, TaskStateUpdate


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


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
) -> CompiledStateGraph:
    """构建并编译包含 Analyzer 与 Initial Planner 的 Phase 3 状态图。"""

    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
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
    # 当前阶段严格保持 START → initialize_task → analyze_task → plan_task → END。
    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_edge("plan_task", END)
    return builder.compile()
```

### `src/taskpilot/state.py`

Phase 3 没有改变 `TaskState` 的字段形状，但 Planner 正式使用了之前预留的 `plan` 和 `current_step_id`，因此完整列出以便 Review。

```python
"""TaskPilot 使用的 LangGraph 状态结构。"""

from typing import Any, TypedDict

from taskpilot.models import (
    PlanStep,
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
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    verification: VerificationResult | None
    status: TaskStatus
    error: str | None
```

### `src/taskpilot/config.py`

Phase 3 在该文件中增加了错误文本的 API Key 脱敏逻辑。

```python
"""TaskPilot 的轻量配置模型。"""

import os

from pydantic import BaseModel


class Settings(BaseModel):
    """为本地运行预留的基础配置。"""

    environment: str = "development"
    log_level: str = "INFO"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取当前阶段所需配置。"""

        return cls(
            environment=os.getenv("TASKPILOT_ENVIRONMENT", "development"),
            log_level=os.getenv("TASKPILOT_LOG_LEVEL", "INFO"),
            llm_api_key=os.getenv("LLM_API_KEY"),
            llm_base_url=os.getenv("LLM_BASE_URL"),
            llm_model=os.getenv("LLM_MODEL"),
        )


def redact_sensitive_values(message: str) -> str:
    """从错误信息中移除当前环境已知的敏感配置值。"""

    api_key = os.getenv("LLM_API_KEY")
    if api_key:
        return message.replace(api_key, "[REDACTED]")
    return message
```

### `src/taskpilot/cli.py`

```python
"""TaskPilot 的最小命令行 smoke test。"""

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
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """运行一次 Phase 3 图，并输出 TaskSpec 与初始计划。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot Initial Planner.")
    parser.add_argument("task", help="Task description")
    args = parser.parse_args(argv)

    # CLI 使用真实 Analyzer 与 Planner；离线测试通过 build_graph 注入 Fake。
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
    if result["error"]:
        print(f"error={result['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### `tests/test_planner.py`

```python
"""Initial Planner 与 Phase 3 状态图的离线测试。"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.graph import build_graph
from taskpilot.models import PlanStep, TaskPlan, TaskSpec, TaskStatus
from taskpilot.planner import PLANNER_SYSTEM_PROMPT, TaskPlanner, validate_plan
from taskpilot.state import TaskState


USER_INPUT = "找3家香港岛月费低于500港币的健身房，并生成CSV。"


def make_task_spec() -> TaskSpec:
    """构造 Fake Analyzer 的固定输出。"""

    return TaskSpec(
        goal="查找满足条件的健身房并生成CSV",
        constraints={
            "location": "香港岛",
            "max_monthly_price_hkd": 500,
            "count": 3,
        },
        expected_output="csv",
        completion_criteria=[
            "至少返回3家符合要求的健身房",
            "候选位于香港岛且月费不超过500港币",
            "最终输出为CSV",
        ],
    )


def make_task_plan() -> TaskPlan:
    """构造工具无关且依赖合法的固定计划。"""

    return TaskPlan(
        steps=[
            PlanStep(
                id=1,
                description="发现一批与目标地点相关、可进一步核实的候选健身房",
                success_criteria=["获得一批与目标地点相关的候选"],
            ),
            PlanStep(
                id=2,
                description="获取候选健身房的价格、地址和营业时间",
                depends_on=[1],
                success_criteria=[
                    "获得候选的价格信息",
                    "获得候选的地址信息",
                    "获得候选的营业时间信息",
                ],
            ),
            PlanStep(
                id=3,
                description="根据地点、价格和数量要求筛选最终候选",
                depends_on=[2],
                success_criteria=[
                    "最终候选数量不少于3家",
                    "最终候选满足地点和价格约束",
                ],
            ),
            PlanStep(
                id=4,
                description="把最终候选整理为CSV结果",
                depends_on=[3],
                success_criteria=["CSV包含最终候选及其关键信息"],
            ),
        ]
    )


def make_initial_state() -> TaskState:
    """构造 Phase 3 图调用所需的初始状态。"""

    return {
        "task_id": "task-phase-3",
        "user_input": USER_INPUT,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


class FakeAnalyzer:
    """记录调用次数并返回固定 TaskSpec。"""

    def __init__(self) -> None:
        self.call_count = 0

    def analyze(self, user_input: str) -> TaskSpec:
        assert user_input == USER_INPUT
        self.call_count += 1
        return make_task_spec()


class FakePlanner:
    """记录调用次数并返回固定 TaskPlan。"""

    def __init__(self) -> None:
        self.call_count = 0

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        assert task_spec == make_task_spec()
        self.call_count += 1
        return make_task_plan()


def test_planner_writes_plan_to_graph_state() -> None:
    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
    ).invoke(make_initial_state())

    assert result["task_spec"] == make_task_spec()
    assert result["plan"] == make_task_plan().steps
    assert result["current_step_id"] == 1
    assert result["status"] is TaskStatus.RUNNING
    assert result["error"] is None


def test_graph_calls_analyzer_and_planner_once() -> None:
    analyzer = FakeAnalyzer()
    planner = FakePlanner()

    build_graph(analyzer=analyzer, planner=planner).invoke(make_initial_state())

    assert analyzer.call_count == 1
    assert planner.call_count == 1


def test_task_planner_uses_structured_output() -> None:
    structured_model = Mock()
    structured_model.invoke.return_value = make_task_plan()
    model = Mock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    result = TaskPlanner(model=model).plan(make_task_spec())

    assert result == make_task_plan()
    model.with_structured_output.assert_called_once_with(TaskPlan)
    structured_model.invoke.assert_called_once()


def test_valid_plan_is_tool_agnostic_and_dependencies_are_legal() -> None:
    task_plan = make_task_plan()

    validate_plan(task_plan)

    descriptions = " ".join(step.description.lower() for step in task_plan.steps)
    assert "browser_open" not in descriptions
    assert "search(" not in descriptions
    assert "css selector" not in descriptions
    assert [step.depends_on for step in task_plan.steps] == [[], [1], [2], [3]]


def test_plan_step_success_criteria_are_not_shared() -> None:
    first = PlanStep(id=1, description="First")
    second = PlanStep(id=2, description="Second")

    first.success_criteria.append("First is complete")

    assert first.success_criteria == ["First is complete"]
    assert second.success_criteria == []


def test_validate_plan_rejects_duplicate_step_ids() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(id=1, description="First", success_criteria=["First done"]),
            PlanStep(id=1, description="Second", success_criteria=["Second done"]),
        ]
    )

    with pytest.raises(ValueError, match="id 必须唯一"):
        validate_plan(task_plan)


def test_validate_plan_rejects_missing_dependency() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(id=1, description="First", success_criteria=["First done"]),
            PlanStep(
                id=2,
                description="Second",
                depends_on=[99],
                success_criteria=["Second done"],
            ),
        ]
    )

    with pytest.raises(ValueError, match="不存在的依赖 99"):
        validate_plan(task_plan)


def test_validate_plan_rejects_self_dependency() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(id=1, description="First", success_criteria=["First done"]),
            PlanStep(
                id=2,
                description="Second",
                depends_on=[2],
                success_criteria=["Second done"],
            ),
        ]
    )

    with pytest.raises(ValueError, match="不能依赖自身"):
        validate_plan(task_plan)


def test_validate_plan_rejects_forward_dependency() -> None:
    task_plan = TaskPlan(
        steps=[
            PlanStep(
                id=1,
                description="First",
                depends_on=[2],
                success_criteria=["First done"],
            ),
            PlanStep(id=2, description="Second", success_criteria=["Second done"]),
        ]
    )

    with pytest.raises(ValueError, match="必须出现在当前步骤之前"):
        validate_plan(task_plan)


def test_validate_plan_rejects_empty_steps() -> None:
    with pytest.raises(ValueError, match="steps 不能为空"):
        validate_plan(TaskPlan(steps=[]))


def test_validate_plan_requires_step_success_criteria() -> None:
    task_plan = TaskPlan(steps=[PlanStep(id=1, description="First")])

    with pytest.raises(ValueError, match="缺少 success_criteria"):
        validate_plan(task_plan)


def test_planner_failure_is_recorded_without_plan() -> None:
    class FailingPlanner:
        def plan(self, task_spec: TaskSpec) -> TaskPlan:
            raise RuntimeError("planner unavailable")

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FailingPlanner(),
    ).invoke(make_initial_state())

    assert result["status"] is TaskStatus.FAILED
    assert result["error"] == (
        "Task Planner 调用失败: RuntimeError: planner unavailable"
    )
    assert result["plan"] == []
    assert result["current_step_id"] is None


def test_planner_failure_redacts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "phase-3-secret-key")

    class FailingPlanner:
        def plan(self, task_spec: TaskSpec) -> TaskPlan:
            raise RuntimeError("request failed for phase-3-secret-key")

    result = build_graph(
        analyzer=FakeAnalyzer(),
        planner=FailingPlanner(),
    ).invoke(make_initial_state())

    assert "phase-3-secret-key" not in result["error"]
    assert "[REDACTED]" in result["error"]


def test_planner_prompt_preserves_planning_boundary() -> None:
    assert "只负责" in PLANNER_SYSTEM_PROMPT
    assert "不执行任务" in PLANNER_SYSTEM_PROMPT
    assert "不得包含" in PLANNER_SYSTEM_PROMPT
    assert "具体工具调用" in PLANNER_SYSTEM_PROMPT
    assert "success_criteria" in PLANNER_SYSTEM_PROMPT
    assert "不得添加 TaskSpec 中不存在的硬约束" in PLANNER_SYSTEM_PROMPT
    assert "不得虚构" in PLANNER_SYSTEM_PROMPT
```

## 6. Planner Prompt

当前实际使用的 `PLANNER_SYSTEM_PROMPT` 全文如下：

```text
你是 Initial Planner，只负责根据输入的 TaskSpec 创建计划，不重新解释或修改用户目标，也不执行任务。
使用完成任务所需的最少合理步骤；每个 PlanStep 描述任务级子目标，不得包含 search、browser、filesystem 等具体工具调用、函数参数或选择器，除非某个工具名称本身就是用户明确要求处理的任务对象。
每一步都必须提供只针对该步骤的、可验证的 success_criteria；整个计划最终应覆盖 TaskSpec.completion_criteria。
合理填写 depends_on，步骤 ID 必须唯一、稳定，并从 1 顺序递增；初始步骤保持 pending 状态。
不得添加 TaskSpec 中不存在的硬约束，不得虚构尚未执行获得的数据，也不得声称已经搜索、打开、读取或生成任何内容。
Planner 只做规划，不做执行。输出必须符合 TaskPlan schema。
```

约束对应关系：

- **plan only / do not execute**：首句和末句都明确 Planner 只创建计划，不执行任务。
- **不生成具体 Tool Calls**：第二句禁止具体工具调用、函数参数和选择器；仅保留“工具名称本身就是用户要求处理的对象”这一语义例外。
- **success_criteria**：第三句要求每一步提供只针对该步骤的可验证成功条件。
- **depends_on**：第四句要求合理填写依赖，并要求 ID 唯一、稳定、顺序递增。
- **不虚构约束**：第五句禁止增加 `TaskSpec` 中不存在的硬约束。
- **不虚构已获得数据**：第五句禁止虚构尚未执行获得的数据，也禁止声称已经搜索、打开、读取或生成内容。
- **避免过度拆解**：第二句要求使用完成任务所需的最少合理步骤。

## 7. Plan Validation

`validate_plan()` 当前实际检查：

1. **Empty plan**：`TaskPlan.steps` 不能为空。
2. **Duplicate step IDs**：所有 `PlanStep.id` 必须唯一。
3. **Nonexistent dependency**：每个 `depends_on` 引用的 ID 必须存在于同一个 Plan。
4. **Self dependency**：Step 不能依赖自身。
5. **Forward dependency**：依赖 Step 必须出现在当前 Step 之前；当前实现按输出顺序判断。
6. **Missing step success criteria**：每个 Step 的 `success_criteria` 列表必须非空。

校验失败时抛出带明确原因的 `ValueError`，不会静默修复模型输出。

当前没有实现的校验或能力：

- 不判断 `description` 或 `success_criteria` 的自然语言是否语义正确、可执行或真正可验证。
- 不用确定性代码判断 Plan 是否完整覆盖全部 `TaskSpec.completion_criteria`；该要求目前由 Prompt 约束。
- 不强制 Step ID 必须为正数、连续数字或从 1 开始；该要求目前由 Prompt 约束。
- 不检查同一 `depends_on` 列表中的重复 ID。
- 不构建通用 DAG、拓扑排序或并行调度器。
- 不自动重试、修复或重新生成非法 Plan。

## 8. Offline Test Results

实际执行命令：

```powershell
conda run -n ai python -m pytest -q
```

实际 pytest 输出：

```text
........................                                                 [100%]
24 passed in 0.64s
```

- Passed: 24
- Failed: 0
- 测试不读取真实 API Key，不调用网络、浏览器或外部服务。

## 9. Graph Smoke Test

使用 `tests/test_planner.py` 中的 `FakeAnalyzer` 和 `FakePlanner` 实际执行了一次 `graph.invoke`。

### 输入任务

```text
找3家香港岛月费低于500港币的健身房，并生成CSV。
```

### Fake Analyzer 返回的 TaskSpec

```json
{
  "goal": "查找满足条件的健身房并生成CSV",
  "constraints": {
    "location": "香港岛",
    "max_monthly_price_hkd": 500,
    "count": 3
  },
  "expected_output": "csv",
  "completion_criteria": [
    "至少返回3家符合要求的健身房",
    "候选位于香港岛且月费不超过500港币",
    "最终输出为CSV"
  ]
}
```

### Fake Planner 返回的完整 TaskPlan

#### Step 1

- `id`: `1`
- `description`: `发现一批与目标地点相关、可进一步核实的候选健身房`
- `depends_on`: `[]`
- `success_criteria`: `["获得一批与目标地点相关的候选"]`
- `status`: `pending`
- `retry_count`: `0`

#### Step 2

- `id`: `2`
- `description`: `获取候选健身房的价格、地址和营业时间`
- `depends_on`: `[1]`
- `success_criteria`: `["获得候选的价格信息", "获得候选的地址信息", "获得候选的营业时间信息"]`
- `status`: `pending`
- `retry_count`: `0`

#### Step 3

- `id`: `3`
- `description`: `根据地点、价格和数量要求筛选最终候选`
- `depends_on`: `[2]`
- `success_criteria`: `["最终候选数量不少于3家", "最终候选满足地点和价格约束"]`
- `status`: `pending`
- `retry_count`: `0`

#### Step 4

- `id`: `4`
- `description`: `把最终候选整理为CSV结果`
- `depends_on`: `[3]`
- `success_criteria`: `["CSV包含最终候选及其关键信息"]`
- `status`: `pending`
- `retry_count`: `0`

### 最终状态

`state.plan` 与上述 `TaskPlan.steps` 相同。实际输出摘要：

```text
edges=__start__->initialize_task, analyze_task->plan_task, initialize_task->analyze_task, plan_task->__end__
status=running
current_step_id=1
error=None
```

Planner 与 Analyzer 在一次普通 `graph.invoke` 中各调用一次。Graph 没有执行上述任何 Step。

## 10. Boundary Checks

### Q1. Planner 是否生成具体工具调用？

**No。** Fake Plan 的 `description` 中没有 `search(...)`、`browser_open(...)`、`click(...)`、`filesystem.write(...)`、CSS selector 或工具参数。自动测试还明确断言不存在 `browser_open`、`search(` 和 `css selector`。真实 Planner Prompt 同样禁止这些内容。

### Q2. Planner 是否只描述任务子目标，而不是工具动作？

**Yes。** 例如 Step 2 是“获取候选健身房的价格、地址和营业时间”，它描述需要达成的信息目标，没有规定通过浏览器、搜索 API 或文件系统完成。

### Q3. success_criteria 是否描述 Step 成功条件，而不是 Task 的所有 Completion Criteria？

**Yes。** Step 2 只要求获得价格、地址和营业时间；它没有复制任务级的“至少返回3家”“位于香港岛”“最终输出为CSV”。Step 4 只负责 CSV 结果结构。`validate_plan()` 只检查非空，语义边界主要由 Prompt 和人工 Review 保证。

### Q4. Planner 是否可能修改 TaskSpec 中用户原始约束？

Planner 接收 `TaskSpec` 的 JSON 作为只读模型输入，代码没有修改该 Pydantic 对象。Prompt 明确要求“不重新解释或修改用户目标”以及“不得添加 TaskSpec 中不存在的硬约束”。但是当前没有确定性语义比较器来证明 LLM 输出没有隐式改写约束，因此仍需依靠 Structured Output、Prompt 和后续 Review/Verifier。

### Q5. Planner 是否虚构尚未执行得到的数据？

Prompt 明确禁止“虚构尚未执行获得的数据”，并禁止声称已经搜索、打开、读取或生成任何内容。Fake Plan 只表达待完成子目标，没有出现具体健身房名称、地址、价格或营业时间等尚未获取的数据。当前没有独立事实检测器。

## 11. Error Handling

Planner 出错时 `plan_task` 当前实际行为：

- `status` 设置为 `TaskStatus.FAILED`。
- 清晰错误写入 `state.error`，格式包含组件、异常类型和脱敏后的异常文本。
- `state.plan` 设置为空列表，不保留可能无效或部分生成的 Plan。
- `current_step_id` 设置为 `None`。
- 如果 `task_spec` 缺失，不调用 Planner，并记录 `Task Planner 无法运行: task_spec 缺失`；如果已有 Analyzer 错误，则保留原错误。
- `LLM_API_KEY` 如果出现在异常文本中，会替换为 `[REDACTED]`。

当前没有 automatic retry、replanning、fallback planner 或 recovery。

## 12. Dependencies

**No new runtime dependencies in Phase 3.**

Phase 3 复用 Phase 2 已有的 LLM 层和 `langchain-openai`。当前 `pyproject.toml` 中的项目依赖为：

```toml
dependencies = [
    "langgraph>=0.2",
    "langchain-openai>=0.2",
    "pydantic>=2",
    "pytest>=7",
]
```

没有加入 Playwright、MCP、FastAPI、数据库 ORM、Vision SDK 或任何 Executor/Tool Runtime 依赖。

## 13. Known Limitations

- Planner 只生成 Initial Plan，尚未执行任何 Step。
- `depends_on` 只保存和校验关系，尚未参与调度。
- `current_step_id` 只被初始化，尚未由 Executor 推进。
- `success_criteria` 尚未被 Verifier 检查。
- `retry_count` 尚未被实际使用。
- 没有 Tool Runtime、工具选择或 Tool Calling。
- 没有 Dynamic Replanning、retry、fallback 或 recovery。
- 没有 DAG scheduler、拓扑调度或并行执行。
- 没有真实 LLM 集成结果；完成阶段时环境中没有可用的 `LLM_API_KEY` 和 `LLM_MODEL`，因此没有发送请求或伪造结果。
- 没有真实 Benchmark 实现或数据。
- Prompt 约束无法提供形式化保证；例如“最少合理步骤”“不隐式修改约束”“覆盖全部 completion criteria”仍需后续人工或 Verifier 检查。

## 14. Files Changed

以下是 Phase 3 的逻辑变更清单。由于 Phase 2 尚未单独提交，最终 Git 状态同时包含之前阶段的未提交文件。

### 新建文件

- `src/taskpilot/planner.py`：Initial Planner、Protocol、Prompt、Structured Output 和 deterministic validation。
- `tests/test_planner.py`：Planner、Graph、Prompt boundary、计划校验、调用次数和错误处理测试。
- `docs/review/phase_03.md`：本阶段人工验收文档。

### 修改文件

- `src/taskpilot/models.py`：`PlanStep` 新增 `success_criteria`；新增 `TaskPlan`。
- `src/taskpilot/graph.py`：新增 `plan_task`，Graph 接入 Planner，支持 Planner 注入和失败状态处理。
- `src/taskpilot/cli.py`：打印 Plan、Step 依赖、步骤成功条件和 `current_step_id`。
- `src/taskpilot/config.py`：增加错误信息 API Key 脱敏。
- `tests/test_models.py`：测试 `success_criteria` 默认值及实例隔离。
- `tests/test_analyzer.py`：为 Phase 3 Graph 测试注入最小 Fake Planner。
- `tests/test_graph_smoke.py`：smoke test 扩展到 `plan_task`。
- `README.md`：更新 Phase 3 Implementation Status、Graph 和未实现边界。

未修改 `state.py` 的字段定义；Phase 3 开始实际写入其已有的 `plan` 和 `current_step_id` 字段。未修改 `llm.py` 或 `analyzer.py` 的 Phase 2 实现。

## 15. Git Status

在本文档创建完成后执行：

```powershell
git status --short
```

实际输出：

```text
warning: unable to access 'C:\Users\zc115/.config/git/ignore': Permission denied
warning: unable to access 'C:\Users\zc115/.config/git/ignore': Permission denied
 M .env.example
 M README.md
 M pyproject.toml
 M src/taskpilot/__init__.py
 M src/taskpilot/cli.py
 M src/taskpilot/config.py
 M src/taskpilot/graph.py
 M src/taskpilot/models.py
 M src/taskpilot/state.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
?? docs/
?? src/taskpilot/analyzer.py
?? src/taskpilot/llm.py
?? src/taskpilot/planner.py
?? tests/test_analyzer.py
?? tests/test_planner.py
```

前两行是当前环境无法读取用户级 Git ignore 文件的警告，不影响仓库状态读取。`?? docs/` 包含本文件 `docs/review/phase_03.md`。
