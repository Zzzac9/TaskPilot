"""CLI 与 HTTP 共用的薄应用层 Runtime Service。"""

from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from taskpilot.analyzer import TaskAnalyzerLike
from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.context import ContextBuilder
from taskpilot.executor import TaskExecutor
from taskpilot.finalizer import (
    FinalAnswerGenerator,
    FinalAnswerGeneratorLike,
    VerifiedResultsRenderer,
)
from taskpilot.graph import build_graph
from taskpilot.mcp.config import MCPServerConfig
from taskpilot.mcp.provider import MCPToolProvider
from taskpilot.models import TaskStatus
from taskpilot.persistence import (
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.persistence.models import ExecutionFaultInjector
from taskpilot.planner import TaskPlannerLike
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.replanner import TaskReplannerLike
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry
from taskpilot.trace import SQLiteTraceRecorder, TraceSummary, summarize_task_trace
from taskpilot.verifier import TaskVerifierLike
from taskpilot.vision import VisionConfig, VisionTargeterLike


class TaskServiceError(RuntimeError):
    """HTTP/CLI 可以转换为各自展示形式的 Service 错误。"""


class TaskNotFoundError(TaskServiceError):
    """durable checkpoint 中不存在指定 task_id。"""


class TaskConflictError(TaskServiceError):
    """当前任务状态不接受请求的恢复动作。"""


class TaskConfigurationError(TaskServiceError):
    """客户端请求了 Host 未启用的能力。"""


@dataclass(frozen=True)
class RuntimeComponents:
    """测试或 Host 可替换的推理组件；工作流拓扑仍由 build_graph 定义。"""

    analyzer: TaskAnalyzerLike | None = None
    planner: TaskPlannerLike | None = None
    executor_model: BaseChatModel | None = None
    verifier: TaskVerifierLike | None = None
    replanner: TaskReplannerLike | None = None
    finalizer: FinalAnswerGeneratorLike | None = None


RuntimeComponentsFactory = Callable[[ToolRegistry], RuntimeComponents]


class MCPProviderLike(Protocol):
    """Service 只需要的 MCP provider lifecycle/registration 边界。"""

    async def __aenter__(self) -> "MCPProviderLike": ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    async def register_tools(self, registry: ToolRegistry) -> Any: ...


MCPProviderFactory = Callable[[Sequence[MCPServerConfig]], MCPProviderLike]
BrowserProviderFactory = Callable[
    [BrowserConfig, VisionConfig, VisionTargeterLike | None], BrowserToolProvider
]


@dataclass(frozen=True)
class TaskRunSnapshot:
    """一次同步运行或只读查询得到的 durable task view。"""

    state: dict[str, Any]
    interrupt: dict[str, Any] | None
    next_nodes: tuple[str, ...]


@dataclass
class _OpenRuntime:
    graph: CompiledStateGraph
    trace_recorder: SQLiteTraceRecorder
    stack: AsyncExitStack
    browser_provider: BrowserToolProvider | None = None
    closed: bool = False

    async def aclose(self) -> None:
        """幂等关闭本 runtime 的 Browser/MCP/SQLite 资源。"""

        if self.closed:
            return
        self.closed = True
        await self.stack.aclose()


def _browser_provider_factory(
    config: BrowserConfig,
    vision_config: VisionConfig,
    vision_targeter: VisionTargeterLike | None,
) -> BrowserToolProvider:
    return BrowserToolProvider(
        config,
        vision_config=vision_config,
        vision_targeter=vision_targeter,
    )


def create_initial_state(
    user_input: str,
    *,
    task_id: str | None = None,
    browser_enabled: bool = False,
) -> TaskState:
    """创建包含所有 durable 控制字段的新任务状态。"""

    return {
        "task_id": task_id or str(uuid4()),
        "user_input": user_input,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "final_answer": None,
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
        "browser_enabled": browser_enabled,
        "status": TaskStatus.CREATED,
        "error": None,
    }


class TaskPilotService:
    """管理 durable Graph，并为等待审批的 Browser task 保留 live runtime。"""

    def __init__(
        self,
        persistence_config: PersistenceConfig | None = None,
        *,
        mcp_configs: Sequence[MCPServerConfig] = (),
        browser_allowed: bool = False,
        browser_config: BrowserConfig | None = None,
        vision_config: VisionConfig | None = None,
        vision_targeter: VisionTargeterLike | None = None,
        components_factory: RuntimeComponentsFactory | None = None,
        mcp_provider_factory: MCPProviderFactory = MCPToolProvider,
        browser_provider_factory: BrowserProviderFactory = _browser_provider_factory,
        max_step_attempts: int = 3,
        max_replans: int = 2,
        graph_recursion_limit: int = 500,
        fault_injector: ExecutionFaultInjector | None = None,
    ) -> None:
        if graph_recursion_limit < 1:
            raise ValueError("graph_recursion_limit 必须至少为 1")
        self.persistence_config = persistence_config or PersistenceConfig()
        self.mcp_configs = tuple(mcp_configs)
        self.browser_allowed = browser_allowed
        self.browser_config = browser_config or BrowserConfig()
        self.vision_config = vision_config or VisionConfig()
        self.vision_targeter = vision_targeter
        self.components_factory = components_factory
        self.mcp_provider_factory = mcp_provider_factory
        self.browser_provider_factory = browser_provider_factory
        self.max_step_attempts = max_step_attempts
        self.max_replans = max_replans
        self.graph_recursion_limit = graph_recursion_limit
        self.fault_injector = fault_injector
        # Page/BrowserContext 只在当前 Service lifespan 内按 task_id 隔离保存。
        self._live_runtimes: dict[str, _OpenRuntime] = {}

    async def __aenter__(self) -> "TaskPilotService":
        """资源按 task 建立；进入 lifespan 本身不连接外部 Provider。"""

        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """FastAPI shutdown 时关闭所有仍等待人工处理的 task runtime。"""

        runtimes = list(self._live_runtimes.values())
        self._live_runtimes.clear()
        for runtime in reversed(runtimes):
            await runtime.aclose()

    def has_live_browser_runtime(self, task_id: str) -> bool:
        """供 Host/验收检查 task-scoped Browser runtime 是否仍存活。"""

        runtime = self._live_runtimes.get(task_id)
        return (
            runtime is not None
            and not runtime.closed
            and runtime.browser_provider is not None
            and runtime.browser_provider.session.is_started
        )

    def live_browser_runtime_identity(self, task_id: str) -> int | None:
        """返回当前进程内 session identity；不写入 durable State。"""

        runtime = self._live_runtimes.get(task_id)
        if runtime is None or runtime.browser_provider is None or runtime.closed:
            return None
        return id(runtime.browser_provider.session)

    async def run_new_task(
        self,
        task: str,
        *,
        enable_browser: bool = False,
    ) -> TaskRunSnapshot:
        """同步运行新任务，直到完成、失败或 LangGraph interrupt。"""

        if enable_browser and not self.browser_allowed:
            raise TaskConfigurationError("Browser capability is disabled by the Host")
        state = create_initial_state(task, browser_enabled=enable_browser)
        task_id = state["task_id"]
        if not enable_browser:
            async with self._open_runtime(enable_browser=False) as runtime:
                config = self._graph_config(task_id)
                await runtime.graph.ainvoke(state, config=config)
                return await self._read_snapshot(runtime.graph, config, task_id)

        runtime = await self._create_runtime(enable_browser=True)
        self._live_runtimes[task_id] = runtime
        try:
            config = self._graph_config(task_id)
            await runtime.graph.ainvoke(state, config=config)
            snapshot = await self._read_snapshot(runtime.graph, config, task_id)
        except BaseException:
            await self._close_live_runtime(task_id, runtime)
            raise
        await self._finalize_live_runtime(task_id, runtime, snapshot)
        return snapshot

    async def resume_task(
        self,
        task_id: str,
        *,
        interrupt_type: str,
        response: dict[str, Any],
    ) -> TaskRunSnapshot:
        """校验当前 durable interrupt 后，仅提交人工 decision/choice。"""

        current = await self.get_task(task_id)
        status = self._status_value(current.state.get("status"))
        if status == TaskStatus.COMPLETED.value:
            raise TaskConflictError("task already completed")
        if current.interrupt is None:
            if status == TaskStatus.FAILED.value:
                raise TaskConflictError("failed task has no resumable interrupt")
            raise TaskConflictError("task has no pending interrupt")
        actual_type = str(current.interrupt.get("type") or "")
        if actual_type != interrupt_type:
            raise TaskConflictError(
                f"interrupt type mismatch: expected {actual_type}, got {interrupt_type}"
            )
        clean_response = self._validate_resume_response(interrupt_type, response)
        enable_browser = bool(current.state.get("browser_enabled", False))
        if enable_browser and not self.browser_allowed:
            raise TaskConfigurationError(
                "Task requires Browser but this Host has Browser disabled"
            )
        live_runtime = self._live_runtimes.get(task_id)
        if (
            enable_browser
            and live_runtime is None
            and self._interrupt_targets_browser(current.interrupt)
        ):
            raise TaskConflictError(
                "live browser environment for pending action is no longer available"
            )
        if live_runtime is not None:
            try:
                config = self._graph_config(task_id)
                await live_runtime.graph.ainvoke(
                    Command(resume=clean_response), config=config
                )
                snapshot = await self._read_snapshot(
                    live_runtime.graph, config, task_id
                )
            except BaseException:
                await self._close_live_runtime(task_id, live_runtime)
                raise
            await self._finalize_live_runtime(task_id, live_runtime, snapshot)
            return snapshot
        async with self._open_runtime(enable_browser=enable_browser) as runtime:
            config = self._graph_config(task_id)
            await runtime.graph.ainvoke(Command(resume=clean_response), config=config)
            return await self._read_snapshot(runtime.graph, config, task_id)

    async def continue_task(self, task_id: str) -> TaskRunSnapshot:
        """从非 interrupt 的 durable next node 续跑，供 CLI crash recovery 使用。"""

        current = await self.get_task(task_id)
        if current.interrupt is not None:
            return current
        status = self._status_value(current.state.get("status"))
        if status == TaskStatus.COMPLETED.value:
            raise TaskConflictError("task already completed")
        if status == TaskStatus.FAILED.value and not current.next_nodes:
            raise TaskConflictError(
                "task already failed; automatic restart is disabled"
            )
        if not current.next_nodes:
            raise TaskConflictError("task has no durable continuation")
        enable_browser = bool(current.state.get("browser_enabled", False))
        live_runtime = self._live_runtimes.get(task_id)
        if live_runtime is not None:
            try:
                config = self._graph_config(task_id)
                await live_runtime.graph.ainvoke(None, config=config)
                snapshot = await self._read_snapshot(
                    live_runtime.graph, config, task_id
                )
            except BaseException:
                await self._close_live_runtime(task_id, live_runtime)
                raise
            await self._finalize_live_runtime(task_id, live_runtime, snapshot)
            return snapshot
        if enable_browser and self._state_has_pending_browser_action(current.state):
            raise TaskConflictError(
                "live browser environment for pending action is no longer available"
            )
        async with self._open_runtime(enable_browser=enable_browser) as runtime:
            config = self._graph_config(task_id)
            await runtime.graph.ainvoke(None, config=config)
            return await self._read_snapshot(runtime.graph, config, task_id)

    async def get_task(self, task_id: str) -> TaskRunSnapshot:
        """只读 durable snapshot；不连接 MCP/Browser，也不构造 Host 组件。"""

        async with self._open_state_runtime() as graph:
            config = self._graph_config(task_id)
            return await self._read_snapshot(graph, config, task_id)

    async def get_trace_summary(self, task_id: str) -> TraceSummary:
        """先确认任务存在，再读取现有 Trace 的确定性聚合。"""

        await self.get_task(task_id)
        async with SQLiteTraceRecorder(
            self.persistence_config.trace_db_path
        ) as recorder:
            return await summarize_task_trace(recorder, task_id)

    @asynccontextmanager
    async def _open_runtime(
        self, *, enable_browser: bool
    ) -> AsyncIterator[_OpenRuntime]:
        """每次调用用 AsyncExitStack 构造并完整关闭外部资源。"""

        runtime = await self._create_runtime(enable_browser=enable_browser)
        try:
            yield runtime
        finally:
            await runtime.aclose()

    async def _create_runtime(self, *, enable_browser: bool) -> _OpenRuntime:
        """构造可临时使用或跨 Browser approval 保留的完整 runtime。"""

        registry = ToolRegistry()
        stack = AsyncExitStack()
        await stack.__aenter__()
        browser_provider: BrowserToolProvider | None = None
        try:
            if self.mcp_configs:
                mcp_provider = await stack.enter_async_context(
                    self.mcp_provider_factory(self.mcp_configs)
                )
                await mcp_provider.register_tools(registry)
            if enable_browser:
                browser_provider = await stack.enter_async_context(
                    self.browser_provider_factory(
                        self.browser_config,
                        self.vision_config,
                        self.vision_targeter,
                    )
                )
                browser_provider.register_tools(registry)

            checkpoint_provider = await stack.enter_async_context(
                SQLiteCheckpointProvider(self.persistence_config)
            )
            journal = await stack.enter_async_context(
                SQLiteActionJournal(self.persistence_config.action_journal_db_path)
            )
            trace = await stack.enter_async_context(
                SQLiteTraceRecorder(self.persistence_config.trace_db_path)
            )
            if checkpoint_provider.checkpointer is None:  # pragma: no cover
                raise RuntimeError("SQLite checkpointer 未初始化")

            components = (
                self.components_factory(registry)
                if self.components_factory is not None
                else RuntimeComponents()
            )
            context_builder = ContextBuilder()
            executor = TaskExecutor(
                registry=registry,
                model=components.executor_model,
                policy=DefaultRiskPolicy(),
                context_builder=context_builder,
            )
            graph = build_graph(
                analyzer=components.analyzer,
                planner=components.planner,
                executor=executor,
                verifier=components.verifier,
                max_step_attempts=self.max_step_attempts,
                checkpointer=checkpoint_provider.checkpointer,
                action_journal=journal,
                fault_injector=self.fault_injector,
                replanner=components.replanner,
                max_replans=self.max_replans,
                context_builder=context_builder,
                trace_recorder=trace,
                finalizer=(
                    components.finalizer
                    or (
                        FinalAnswerGenerator()
                        if self.components_factory is None
                        else VerifiedResultsRenderer()
                    )
                ),
            )
            return _OpenRuntime(
                graph=graph,
                trace_recorder=trace,
                stack=stack,
                browser_provider=browser_provider,
            )
        except BaseException:
            await stack.aclose()
            raise

    @asynccontextmanager
    async def _open_state_runtime(self) -> AsyncIterator[CompiledStateGraph]:
        """只打开 checkpoint，并构建只供 aget_state 使用的最小 Graph。"""

        async with SQLiteCheckpointProvider(self.persistence_config) as checkpoint:
            if checkpoint.checkpointer is None:  # pragma: no cover
                raise RuntimeError("SQLite checkpointer 未初始化")
            graph = build_graph(
                executor=TaskExecutor(registry=ToolRegistry()),
                checkpointer=checkpoint.checkpointer,
            )
            yield graph

    async def _finalize_live_runtime(
        self,
        task_id: str,
        runtime: _OpenRuntime,
        snapshot: TaskRunSnapshot,
    ) -> None:
        """只在非终态 interrupt 时保留 Browser；其他路径立即清理。"""

        status = self._status_value(snapshot.state.get("status"))
        should_retain = snapshot.interrupt is not None and status not in {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
        }
        if not should_retain:
            await self._close_live_runtime(task_id, runtime)

    async def _close_live_runtime(
        self,
        task_id: str,
        runtime: _OpenRuntime,
    ) -> None:
        if self._live_runtimes.get(task_id) is runtime:
            self._live_runtimes.pop(task_id, None)
        await runtime.aclose()

    @staticmethod
    def _interrupt_targets_browser(interrupt: dict[str, Any]) -> bool:
        return str(interrupt.get("tool_name") or "").startswith("browser_")

    @staticmethod
    def _state_has_pending_browser_action(state: dict[str, Any]) -> bool:
        action = state.get("pending_executor_action")
        if action is None:
            return False
        tool_name = (
            action.get("tool_name")
            if isinstance(action, dict)
            else getattr(action, "tool_name", None)
        )
        return str(tool_name or "").startswith("browser_")

    @staticmethod
    async def _read_snapshot(
        graph: CompiledStateGraph,
        config: dict[str, Any],
        task_id: str,
    ) -> TaskRunSnapshot:
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise TaskNotFoundError(f"unknown task_id: {task_id}")
        interrupts = [item.value for task in snapshot.tasks for item in task.interrupts]
        interrupt_value = interrupts[0] if interrupts else None
        if interrupt_value is not None and not isinstance(interrupt_value, dict):
            interrupt_value = {"type": "unknown", "reason": str(interrupt_value)}
        return TaskRunSnapshot(
            state=dict(snapshot.values),
            interrupt=interrupt_value,
            next_nodes=tuple(snapshot.next),
        )

    @staticmethod
    def _validate_resume_response(
        interrupt_type: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = (
            {"decision", "reason"}
            if interrupt_type == "tool_approval"
            else {"choice", "reason"}
            if interrupt_type == "ambiguous_tool_recovery"
            else set()
        )
        required = (
            "decision"
            if interrupt_type == "tool_approval"
            else "choice"
            if interrupt_type == "ambiguous_tool_recovery"
            else None
        )
        if required is None:
            raise TaskConflictError(f"unsupported interrupt type: {interrupt_type}")
        extras = set(response) - allowed
        if extras:
            raise TaskConflictError(
                "resume cannot modify frozen action fields: "
                + ", ".join(sorted(extras))
            )
        if required not in response:
            raise TaskConflictError(f"resume response missing {required}")
        return dict(response)

    def _graph_config(self, task_id: str) -> dict[str, Any]:
        """返回所有生产入口共用的 LangGraph 运行配置。"""

        return {
            "configurable": {"thread_id": task_id},
            "recursion_limit": self.graph_recursion_limit,
        }

    @staticmethod
    def _status_value(value: Any) -> str:
        return value.value if isinstance(value, TaskStatus) else str(value)
