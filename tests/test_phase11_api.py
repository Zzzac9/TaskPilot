"""Phase 11 TaskPilotService 与薄 FastAPI 的离线验收。"""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Sequence, cast

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.errors import GraphRecursionError

from taskpilot.api import create_app
from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import FINISH_STEP_NAME
from taskpilot.mcp.config import MCPServerConfig, MCPTransport
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    VerificationResult,
)
from taskpilot.persistence import (
    ExecutionFaultPoint,
    InjectedCrash,
    PersistenceConfig,
)
from taskpilot.service import RuntimeComponents, TaskPilotService
from taskpilot.tools import ToolRegistry
from taskpilot.vision import VisionConfig, VisionTarget, VisionTargetRequest


class QueueFunctionCallingModel:
    """所有 Service 重建共享 response queue，但不保存 Task/Graph state。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = responses

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "QueueFunctionCallingModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if not self.responses:
            raise AssertionError("Fake model response queue exhausted")
        return self.responses.pop(0)


class FakeVisionTargeter:
    """API Chromium smoke 使用的 deterministic canvas targeter。"""

    def __init__(self) -> None:
        self.calls: list[VisionTargetRequest] = []

    async def locate(
        self,
        *,
        screenshot: bytes,
        request: VisionTargetRequest,
    ) -> VisionTarget:
        assert screenshot
        self.calls.append(request.model_copy(deep=True))
        return VisionTarget(
            found=True,
            x=201,
            y=145,
            confidence=0.96,
            reason="Canvas Action rectangle is visible",
        )


@dataclass
class ProviderCounters:
    mcp_connects: int = 0
    browser_launches: int = 0
    component_builds: int = 0
    tool_calls: int = 0


class CountingMCPProvider:
    """若 read-only GET 错误连接 MCP，计数会立即暴露。"""

    def __init__(self, counters: ProviderCounters) -> None:
        self.counters = counters

    async def __aenter__(self) -> "CountingMCPProvider":
        self.counters.mcp_connects += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def register_tools(self, registry: ToolRegistry) -> None:
        raise AssertionError("read-only State path must not list/register MCP tools")


@dataclass
class CountingTool:
    name: str = "local_counter"
    risk: str = "low"
    replay_safe: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)
    description: str = "Deterministic offline counter"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
                "api_key": {"type": "string"},
            },
            "required": ["value"],
            "additionalProperties": False,
        }
    )

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
    counter_path: Path = Path("counter.txt")

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        current = (
            int(self.counter_path.read_text(encoding="utf-8"))
            if self.counter_path.exists()
            else 0
        )
        current += 1
        self.counter_path.write_text(str(current), encoding="utf-8")
        self.calls.append(dict(arguments))
        return {"call_count": current, "value": arguments["value"]}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["Fixture completes"])


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Run one deterministic fixture",
                    success_criteria=["A deterministic observation exists"],
                )
            ]
        )


class TwoStepPlanner:
    """生成两个合法步骤，使 action-level Graph 稳定超过 25 次 transition。"""

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Run the first deterministic action sequence",
                    success_criteria=["First sequence has observations"],
                ),
                PlanStep(
                    id=2,
                    description="Run the second deterministic action sequence",
                    success_criteria=["Second sequence has observations"],
                    depends_on=[1],
                ),
            ]
        )


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="Fixture observation accepted",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="Fixture completed")


@dataclass
class CrashOnce:
    point: ExecutionFaultPoint
    raised: bool = False

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if point is self.point and not self.raised:
            self.raised = True
            raise InjectedCrash(f"{point.value}:{action_key}")


def tool_call(
    call_id: str,
    *,
    name: str = "local_counter",
    arguments: dict[str, Any] | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {"value": 1} if arguments is None else arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def finish(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": FINISH_STEP_NAME,
                "args": {
                    "summary": "The deterministic fixture completed.",
                    "evidence": ["The expected local observation exists."],
                    "output": {"completed": True},
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def components_factory(
    tool: CountingTool,
    responses: list[AIMessage],
):
    def factory(registry: ToolRegistry) -> RuntimeComponents:
        registry.register(tool)
        return RuntimeComponents(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor_model=cast(BaseChatModel, QueueFunctionCallingModel(responses)),
            verifier=FakeVerifier(),
        )

    return factory


def multi_step_components_factory(
    tool: CountingTool,
    responses: list[AIMessage],
):
    def factory(registry: ToolRegistry) -> RuntimeComponents:
        registry.register(tool)
        return RuntimeComponents(
            analyzer=FakeAnalyzer(),
            planner=TwoStepPlanner(),
            executor_model=cast(BaseChatModel, QueueFunctionCallingModel(responses)),
            verifier=FakeVerifier(),
        )

    return factory


def multi_step_responses() -> list[AIMessage]:
    responses: list[AIMessage] = []
    for step_id in (1, 2):
        responses.extend(
            tool_call(
                f"recursion-step-{step_id}-tool-{action}",
                arguments={"value": step_id * 10 + action},
            )
            for action in range(1, 5)
        )
        responses.append(finish(f"recursion-step-{step_id}-finish"))
    return responses


def service_for(
    tmp_path: Path,
    tool: CountingTool,
    responses: list[AIMessage],
    *,
    fault_injector: CrashOnce | None = None,
) -> TaskPilotService:
    return TaskPilotService(
        PersistenceConfig.from_state_dir(tmp_path),
        components_factory=components_factory(tool, responses),
        fault_injector=fault_injector,
    )


def browser_components_factory(
    responses: list[AIMessage],
    counters: ProviderCounters | None = None,
):
    def factory(registry: ToolRegistry) -> RuntimeComponents:
        if counters is not None:
            counters.component_builds += 1
        return RuntimeComponents(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor_model=cast(BaseChatModel, QueueFunctionCallingModel(responses)),
            verifier=FakeVerifier(),
        )

    return factory


def counting_browser_provider_factory(
    launches: list[BrowserToolProvider],
):
    def factory(
        config: BrowserConfig,
        vision_config: VisionConfig,
        vision_targeter: Any,
    ) -> BrowserToolProvider:
        provider = BrowserToolProvider(
            config,
            vision_config=vision_config,
            vision_targeter=vision_targeter,
        )
        launches.append(provider)
        return provider

    return factory


def browser_service_for(
    tmp_path: Path,
    responses: list[AIMessage],
    launches: list[BrowserToolProvider],
    *,
    vision_config: VisionConfig | None = None,
    vision_targeter: FakeVisionTargeter | None = None,
) -> TaskPilotService:
    return TaskPilotService(
        PersistenceConfig.from_state_dir(tmp_path),
        browser_allowed=True,
        browser_config=BrowserConfig(
            headless=True,
            action_timeout_ms=250,
            navigation_timeout_ms=5_000,
            download_dir=tmp_path / "downloads",
            artifact_dir=tmp_path / "screenshots",
        ),
        vision_config=vision_config,
        vision_targeter=vision_targeter,
        components_factory=browser_components_factory(responses),
        browser_provider_factory=counting_browser_provider_factory(launches),
    )


@asynccontextmanager
async def api_client(service: TaskPilotService) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(service)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://taskpilot.local",
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_service_direct_runtime_completes_and_reads_durable_state(
    tmp_path: Path,
) -> None:
    tool = CountingTool()
    service = service_for(
        tmp_path,
        tool,
        [tool_call("service-tool"), finish("service-finish")],
    )
    completed = await service.run_new_task("Direct service task")
    restored = await service.get_task(completed.state["task_id"])

    assert completed.state["status"].value == "completed"
    assert restored.state["task_id"] == completed.state["task_id"]
    assert restored.interrupt is None
    assert tool.calls == [{"value": 1}]


def test_service_graph_recursion_config_defaults_and_custom_values() -> None:
    service = TaskPilotService()
    custom = TaskPilotService(graph_recursion_limit=60)

    assert service.graph_recursion_limit == 500
    assert service._graph_config("default-task") == {
        "configurable": {"thread_id": "default-task"},
        "recursion_limit": 500,
    }
    assert custom._graph_config("custom-task")["recursion_limit"] == 60


@pytest.mark.parametrize("value", [0, -1])
def test_service_rejects_invalid_graph_recursion_limit(value: int) -> None:
    with pytest.raises(ValueError, match="graph_recursion_limit"):
        TaskPilotService(graph_recursion_limit=value)


@pytest.mark.asyncio
async def test_service_default_limit_completes_more_than_25_graph_transitions(
    tmp_path: Path,
) -> None:
    tool = CountingTool()
    service = TaskPilotService(
        PersistenceConfig.from_state_dir(tmp_path),
        components_factory=multi_step_components_factory(
            tool,
            multi_step_responses(),
        ),
        graph_recursion_limit=100,
    )

    completed = await service.run_new_task("Complete two long deterministic steps")

    assert completed.state["status"].value == "completed"
    assert [call["value"] for call in tool.calls] == [11, 12, 13, 14, 21, 22, 23, 24]


@pytest.mark.asyncio
async def test_service_limit_25_reproduces_graph_recursion_error(
    tmp_path: Path,
) -> None:
    service = TaskPilotService(
        PersistenceConfig.from_state_dir(tmp_path),
        components_factory=multi_step_components_factory(
            CountingTool(),
            multi_step_responses(),
        ),
        graph_recursion_limit=25,
    )

    with pytest.raises(GraphRecursionError):
        await service.run_new_task("Reproduce the former production limit")


@pytest.mark.asyncio
async def test_service_direct_get_is_side_effect_free_while_waiting(
    tmp_path: Path,
) -> None:
    tool = CountingTool(risk="high", replay_safe=False)
    service = service_for(tmp_path, tool, [tool_call("service-wait")])
    paused = await service.run_new_task("Direct waiting task")
    restored = await service.get_task(paused.state["task_id"])

    assert paused.state["status"].value == "waiting_approval"
    assert restored.interrupt is not None
    assert restored.interrupt["type"] == "tool_approval"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_post_tasks_completes_and_get_returns_same_task(tmp_path: Path) -> None:
    tool = CountingTool()
    service = service_for(
        tmp_path,
        tool,
        [tool_call("complete-tool"), finish("complete-finish")],
    )
    async with api_client(service) as client:
        created = await client.post("/tasks", json={"task": "Complete task"})
        task_id = created.json()["task_id"]
        fetched = await client.get(f"/tasks/{task_id}")

    assert created.status_code == 200
    assert created.json()["status"] == "completed"
    assert fetched.status_code == 200
    assert fetched.json()["task_id"] == task_id
    assert fetched.json()["goal"] == "Complete task"
    assert tool.calls == [{"value": 1}]


@pytest.mark.asyncio
async def test_unknown_task_is_404(tmp_path: Path) -> None:
    service = service_for(tmp_path, CountingTool(), [])
    async with api_client(service) as client:
        response = await client.get("/tasks/not-found")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_trace_summary_endpoint_returns_real_aggregation(tmp_path: Path) -> None:
    service = service_for(
        tmp_path,
        CountingTool(),
        [tool_call("trace-tool"), finish("trace-finish")],
    )
    async with api_client(service) as client:
        created = await client.post("/tasks", json={"task": "Trace task"})
        response = await client.get(f"/tasks/{created.json()['task_id']}/trace-summary")
    assert response.status_code == 200
    assert response.json()["tool_call_count"] == 1
    assert response.json()["event_count"] > 0


@pytest.mark.asyncio
async def test_resume_completed_task_is_409(tmp_path: Path) -> None:
    service = service_for(
        tmp_path,
        CountingTool(),
        [tool_call("done-tool"), finish("done-finish")],
    )
    async with api_client(service) as client:
        created = await client.post("/tasks", json={"task": "Done"})
        response = await client.post(
            f"/tasks/{created.json()['task_id']}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )
    assert response.status_code == 409
    assert "already completed" in response.json()["detail"]


@pytest.mark.asyncio
async def test_resume_failed_task_without_interrupt_is_409(tmp_path: Path) -> None:
    failed_response = AIMessage(content="", tool_calls=[])
    service = service_for(tmp_path, CountingTool(), [failed_response])
    async with api_client(service) as client:
        created = await client.post("/tasks", json={"task": "Fail deterministically"})
        assert created.json()["status"] == "failed"
        response = await client.post(
            f"/tasks/{created.json()['task_id']}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )
    assert response.status_code == 409
    assert "no resumable interrupt" in response.json()["detail"]


@pytest.mark.asyncio
async def test_high_risk_waits_and_preview_is_redacted(tmp_path: Path) -> None:
    tool = CountingTool(risk="high", replay_safe=False)
    service = service_for(
        tmp_path,
        tool,
        [
            tool_call(
                "secret-write",
                arguments={"value": 7, "api_key": "never-return-this"},
            ),
            finish("after-secret"),
        ],
    )
    async with api_client(service) as client:
        response = await client.post("/tasks", json={"task": "High risk task"})

    body = response.json()
    assert body["status"] == "waiting_approval"
    assert body["interrupt"]["risk_level"] == "high"
    assert body["interrupt"]["arguments_preview"]["api_key"] == "[REDACTED]"
    assert "never-return-this" not in response.text
    assert tool.calls == []


@pytest.mark.asyncio
async def test_api_approval_executes_frozen_action_once(tmp_path: Path) -> None:
    tool = CountingTool(risk="high", replay_safe=False)
    service = service_for(
        tmp_path,
        tool,
        [tool_call("approve-tool", arguments={"value": 9}), finish("approve-finish")],
    )
    async with api_client(service) as client:
        paused = await client.post("/tasks", json={"task": "Approve task"})
        task_id = paused.json()["task_id"]
        counter_before = len(tool.calls)
        resumed = await client.post(
            f"/tasks/{task_id}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )

    assert counter_before == 0
    assert tool.calls == [{"value": 9}]
    assert resumed.json()["task_id"] == task_id
    assert resumed.json()["status"] == "completed"
    print(
        "FASTAPI_HITL_SMOKE="
        + json.dumps(
            {
                "initial_status": paused.json()["status"],
                "counter_before": counter_before,
                "resume": "approve",
                "counter_after": len(tool.calls),
                "final_status": resumed.json()["status"],
            }
        )
    )


@pytest.mark.asyncio
async def test_api_rejection_never_executes_tool(tmp_path: Path) -> None:
    tool = CountingTool(risk="high", replay_safe=False)
    service = service_for(
        tmp_path,
        tool,
        [tool_call("reject-tool"), finish("reject-finish")],
    )
    async with api_client(service) as client:
        paused = await client.post("/tasks", json={"task": "Reject task"})
        response = await client.post(
            f"/tasks/{paused.json()['task_id']}/resume",
            json={
                "type": "tool_approval",
                "decision": "reject",
                "reason": "No write",
            },
        )
    assert response.json()["status"] == "completed"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_wrong_interrupt_type_is_409(tmp_path: Path) -> None:
    service = service_for(
        tmp_path,
        CountingTool(risk="high"),
        [tool_call("wrong-type")],
    )
    async with api_client(service) as client:
        paused = await client.post("/tasks", json={"task": "Wrong type"})
        response = await client.post(
            f"/tasks/{paused.json()['task_id']}/resume",
            json={"type": "ambiguous_tool_recovery", "choice": "retry"},
        )
    assert response.status_code == 409
    assert "type mismatch" in response.json()["detail"]


@pytest.mark.asyncio
async def test_resume_extra_fields_are_rejected_without_execution(
    tmp_path: Path,
) -> None:
    tool = CountingTool(risk="high")
    service = service_for(tmp_path, tool, [tool_call("extra-field")])
    async with api_client(service) as client:
        paused = await client.post("/tasks", json={"task": "No edits"})
        response = await client.post(
            f"/tasks/{paused.json()['task_id']}/resume",
            json={
                "type": "tool_approval",
                "decision": "approve",
                "arguments": {"value": 999},
            },
        )
    assert response.status_code == 422
    assert tool.calls == []


@pytest.mark.asyncio
async def test_get_task_has_no_tool_side_effect(tmp_path: Path) -> None:
    tool = CountingTool(risk="high")
    service = service_for(tmp_path, tool, [tool_call("get-safe")])
    async with api_client(service) as client:
        paused = await client.post("/tasks", json={"task": "GET is read only"})
        task_id = paused.json()["task_id"]
        for _ in range(3):
            fetched = await client.get(f"/tasks/{task_id}")
            assert fetched.json()["status"] == "waiting_approval"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_get_and_trace_summary_use_checkpoint_only_runtime(
    tmp_path: Path,
) -> None:
    """已知/未知 GET 都不能连接 MCP、启动 Browser 或构造 LLM runtime。"""

    config = PersistenceConfig.from_state_dir(tmp_path)
    seed_tool = CountingTool()
    seed = TaskPilotService(
        config,
        components_factory=components_factory(
            seed_tool,
            [tool_call("seed-readonly"), finish("seed-finish")],
        ),
    )
    completed = await seed.run_new_task("Seed durable state")
    task_id = completed.state["task_id"]

    counters = ProviderCounters()

    def forbidden_browser_factory(
        config: BrowserConfig,
        vision_config: VisionConfig,
        vision_targeter: Any,
    ) -> BrowserToolProvider:
        counters.browser_launches += 1
        raise AssertionError("read-only GET must not launch Browser")

    def forbidden_components(registry: ToolRegistry) -> RuntimeComponents:
        counters.component_builds += 1
        raise AssertionError("read-only GET must not build Host/LLM components")

    reader = TaskPilotService(
        config,
        mcp_configs=[
            MCPServerConfig(
                server_id="must-not-connect",
                transport=MCPTransport.STDIO,
                command="must-not-start.exe",
            )
        ],
        browser_allowed=True,
        components_factory=forbidden_components,
        mcp_provider_factory=lambda configs: CountingMCPProvider(counters),
        browser_provider_factory=forbidden_browser_factory,
    )
    async with api_client(reader) as client:
        known = await client.get(f"/tasks/{task_id}")
        summary = await client.get(f"/tasks/{task_id}/trace-summary")
        unknown = await client.get("/tasks/unknown-readonly")

    assert known.status_code == 200
    assert known.json()["status"] == "completed"
    assert summary.status_code == 200
    assert unknown.status_code == 404
    assert counters == ProviderCounters()


@pytest.mark.asyncio
async def test_create_schema_rejects_arbitrary_mcp_command(tmp_path: Path) -> None:
    service = service_for(tmp_path, CountingTool(), [])
    async with api_client(service) as client:
        response = await client.post(
            "/tasks",
            json={
                "task": "Unsafe config",
                "mcp": {"command": "arbitrary.exe", "args": ["--run"]},
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_client_cannot_enable_browser_when_host_disallows_it(
    tmp_path: Path,
) -> None:
    service = service_for(tmp_path, CountingTool(), [])
    async with api_client(service) as client:
        response = await client.post(
            "/tasks",
            json={"task": "Browser request", "enable_browser": True},
        )
    assert response.status_code == 422
    assert "disabled by the Host" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_state_survives_service_and_app_recreation(tmp_path: Path) -> None:
    counter_path = tmp_path / "durable-counter.txt"
    responses = [tool_call("durable-tool"), finish("durable-finish")]
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool_a = FileCounterTool(
        risk="high",
        replay_safe=False,
        counter_path=counter_path,
    )
    service_a = TaskPilotService(
        config,
        components_factory=components_factory(tool_a, responses),
    )
    async with api_client(service_a) as client:
        paused = await client.post("/tasks", json={"task": "Durable approval"})
        task_id = paused.json()["task_id"]

    tool_b = FileCounterTool(
        risk="high",
        replay_safe=False,
        counter_path=counter_path,
    )
    service_b = TaskPilotService(
        config,
        components_factory=components_factory(tool_b, responses),
    )
    async with api_client(service_b) as client:
        restored = await client.get(f"/tasks/{task_id}")
        resumed = await client.post(
            f"/tasks/{task_id}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )

    assert restored.json()["status"] == "waiting_approval"
    assert restored.json()["interrupt"]["type"] == "tool_approval"
    assert resumed.json()["status"] == "completed"
    assert counter_path.read_text(encoding="utf-8") == "1"
    print(
        "FASTAPI_DURABLE_RESUME_SMOKE="
        + json.dumps(
            {
                "service_recreated": True,
                "pending_interrupt_restored": True,
                "final_status": resumed.json()["status"],
            }
        )
    )


@pytest.mark.asyncio
async def test_recovery_interrupt_can_retry_via_api(tmp_path: Path) -> None:
    responses = [tool_call("recovery-retry"), finish("recovery-finish")]
    counter = FileCounterTool(
        risk="low",
        replay_safe=False,
        counter_path=tmp_path / "recovery-retry.txt",
    )
    crashing = service_for(
        tmp_path,
        counter,
        responses,
        fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
    )
    with pytest.raises(InjectedCrash):
        await crashing.run_new_task("Recovery retry")
    restored = service_for(tmp_path, counter, responses)
    interrupted = await restored.continue_task(
        (await _only_task_id(restored.persistence_config)).strip()
    )
    task_id = interrupted.state["task_id"]
    assert interrupted.interrupt is not None
    async with api_client(restored) as client:
        response = await client.post(
            f"/tasks/{task_id}/resume",
            json={"type": "ambiguous_tool_recovery", "choice": "retry"},
        )
    assert response.json()["status"] == "completed"
    assert counter.counter_path.read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_recovery_interrupt_can_abort_via_api(tmp_path: Path) -> None:
    responses = [tool_call("recovery-abort")]
    counter = FileCounterTool(
        risk="low",
        replay_safe=False,
        counter_path=tmp_path / "recovery-abort.txt",
    )
    crashing = service_for(
        tmp_path,
        counter,
        responses,
        fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
    )
    with pytest.raises(InjectedCrash):
        await crashing.run_new_task("Recovery abort")
    restored = service_for(tmp_path, counter, responses)
    task_id = (await _only_task_id(restored.persistence_config)).strip()
    interrupted = await restored.continue_task(task_id)
    assert interrupted.interrupt is not None
    async with api_client(restored) as client:
        response = await client.post(
            f"/tasks/{task_id}/resume",
            json={
                "type": "ambiguous_tool_recovery",
                "choice": "abort",
                "reason": "Do not repeat",
            },
        )
    assert response.json()["status"] == "failed"
    assert not counter.counter_path.exists()


async def _only_task_id(config: PersistenceConfig) -> str:
    """从测试 SQLite checkpoint 的 thread_id 列中读取唯一任务标识。"""

    import aiosqlite

    async with aiosqlite.connect(config.checkpoint_db_path) as connection:
        cursor = await connection.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    return str(rows[0][0])


def _latest_browser_text(
    snapshot: Any, selector_tool: str = "browser_extract_text"
) -> str:
    records = [
        record
        for record in snapshot.state.get("tool_calls", [])
        if record.tool_name == selector_tool and record.result is not None
    ]
    assert records
    return str(records[-1].result["text"])


@pytest.mark.asyncio
async def test_real_browser_high_risk_approval_reuses_task_scoped_runtime(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    launches: list[BrowserToolProvider] = []
    responses = [
        tool_call(
            "browser-hitl-nav",
            name="browser_navigate",
            arguments={"url": f"{browser_site_url}/index.html"},
        ),
        tool_call(
            "browser-hitl-click",
            name="browser_click",
            arguments={
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Submit profile",
                }
            },
        ),
        tool_call(
            "browser-hitl-status",
            name="browser_extract_text",
            arguments={"locator": {"strategy": "css", "value": "#submit-status"}},
        ),
        finish("browser-hitl-finish"),
    ]
    service = browser_service_for(tmp_path, responses, launches)
    async with api_client(service) as client:
        paused = await client.post(
            "/tasks",
            json={"task": "Approve a real browser submit", "enable_browser": True},
        )
        task_id = paused.json()["task_id"]
        runtime_identity = service.live_browser_runtime_identity(task_id)
        assert runtime_identity is not None
        assert service.has_live_browser_runtime(task_id)
        provider = launches[0]
        _, page = provider.session.get_page()
        before = await page.locator("#submit-status").inner_text()

        resumed = await client.post(
            f"/tasks/{task_id}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )
        final = await service.get_task(task_id)

        assert len(launches) == 1
        assert id(provider.session) == runtime_identity
        assert before == "Profile not submitted."
        assert _latest_browser_text(final) == "Profile submitted."
        assert resumed.json()["status"] == "completed"
        assert not service.has_live_browser_runtime(task_id)
        assert provider.session.is_started is False
    print(
        "FASTAPI_BROWSER_HITL_SMOKE="
        + json.dumps(
            {
                "waiting_approval": paused.json()["status"] == "waiting_approval",
                "same_live_browser_runtime": len(launches) == 1,
                "side_effect_before_approval": False,
                "side_effect_after_approval": True,
                "final_status": resumed.json()["status"],
            }
        )
    )


@pytest.mark.asyncio
async def test_real_canvas_vision_approval_via_api_uses_same_live_runtime(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    launches: list[BrowserToolProvider] = []
    targeter = FakeVisionTargeter()
    responses = [
        tool_call(
            "vision-api-nav",
            name="browser_navigate",
            arguments={"url": f"{browser_site_url}/canvas.html"},
        ),
        tool_call(
            "vision-api-click",
            name="browser_click",
            arguments={
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Canvas Action",
                }
            },
        ),
        tool_call(
            "vision-api-status",
            name="browser_extract_text",
            arguments={"locator": {"strategy": "css", "value": "#canvas-status"}},
        ),
        finish("vision-api-finish"),
    ]
    service = browser_service_for(
        tmp_path,
        responses,
        launches,
        vision_config=VisionConfig(enabled=True),
        vision_targeter=targeter,
    )
    async with api_client(service) as client:
        paused = await client.post(
            "/tasks",
            json={"task": "Approve Canvas Action", "enable_browser": True},
        )
        task_id = paused.json()["task_id"]
        identity = service.live_browser_runtime_identity(task_id)
        assert identity is not None
        provider = launches[0]
        _, page = provider.session.get_page()
        before = await page.locator("#canvas-status").inner_text()
        vision_before = len(targeter.calls)

        resumed = await client.post(
            f"/tasks/{task_id}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )
        final = await service.get_task(task_id)

        assert before == "Canvas not clicked."
        assert vision_before == 0
        assert len(targeter.calls) == 1
        assert len(launches) == 1
        assert id(provider.session) == identity
        assert _latest_browser_text(final) == "Canvas clicked."
        assert resumed.json()["status"] == "completed"
        assert not service.has_live_browser_runtime(task_id)
        assert provider.session.is_started is False
    print(
        "FASTAPI_VISION_HITL_SMOKE="
        + json.dumps(
            {
                "waiting_approval": True,
                "same_live_browser_runtime": True,
                "side_effect_before_approval": False,
                "vision_called_before_approval": False,
                "side_effect_after_approval": True,
                "final_status": resumed.json()["status"],
            }
        )
    )


@pytest.mark.asyncio
async def test_real_canvas_vision_reject_never_clicks_and_cleans_runtime(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    launches: list[BrowserToolProvider] = []
    targeter = FakeVisionTargeter()
    responses = [
        tool_call(
            "vision-reject-nav",
            name="browser_navigate",
            arguments={"url": f"{browser_site_url}/canvas.html"},
        ),
        tool_call(
            "vision-reject-click",
            name="browser_click",
            arguments={
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Canvas Action",
                }
            },
        ),
        tool_call(
            "vision-reject-status",
            name="browser_extract_text",
            arguments={"locator": {"strategy": "css", "value": "#canvas-status"}},
        ),
        finish("vision-reject-finish"),
    ]
    service = browser_service_for(
        tmp_path,
        responses,
        launches,
        vision_config=VisionConfig(enabled=True),
        vision_targeter=targeter,
    )
    async with api_client(service) as client:
        paused = await client.post(
            "/tasks",
            json={"task": "Reject Canvas Action", "enable_browser": True},
        )
        task_id = paused.json()["task_id"]
        provider = launches[0]
        rejected = await client.post(
            f"/tasks/{task_id}/resume",
            json={
                "type": "tool_approval",
                "decision": "reject",
                "reason": "Do not click canvas",
            },
        )
        final = await service.get_task(task_id)

        assert rejected.json()["status"] == "completed"
        assert targeter.calls == []
        assert _latest_browser_text(final) == "Canvas not clicked."
        assert not service.has_live_browser_runtime(task_id)
        assert provider.session.is_started is False


@pytest.mark.asyncio
async def test_recreated_service_fails_closed_for_pending_browser_approval(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    responses = [
        tool_call(
            "restart-browser-nav",
            name="browser_navigate",
            arguments={"url": f"{browser_site_url}/index.html"},
        ),
        tool_call(
            "restart-browser-click",
            name="browser_click",
            arguments={
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Submit profile",
                }
            },
        ),
    ]
    launches_a: list[BrowserToolProvider] = []
    service_a = browser_service_for(tmp_path, responses, launches_a)
    async with api_client(service_a) as client:
        paused = await client.post(
            "/tasks",
            json={"task": "Browser restart boundary", "enable_browser": True},
        )
        task_id = paused.json()["task_id"]
        provider_a = launches_a[0]
        _, page = provider_a.session.get_page()
        before = await page.locator("#submit-status").inner_text()
        assert service_a.has_live_browser_runtime(task_id)

    assert before == "Profile not submitted."
    assert provider_a.session.is_started is False
    assert not service_a.has_live_browser_runtime(task_id)

    launches_b: list[BrowserToolProvider] = []
    service_b = browser_service_for(tmp_path, responses, launches_b)
    async with api_client(service_b) as client:
        restored = await client.get(f"/tasks/{task_id}")
        resume = await client.post(
            f"/tasks/{task_id}/resume",
            json={"type": "tool_approval", "decision": "approve"},
        )

    assert restored.status_code == 200
    assert restored.json()["status"] == "waiting_approval"
    assert resume.status_code == 409
    assert (
        resume.json()["detail"]
        == "live browser environment for pending action is no longer available"
    )
    assert launches_b == []


@pytest.mark.asyncio
async def test_real_localhost_browser_task_via_api(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    responses = [
        tool_call(
            "api-browser-nav",
            name="browser_navigate",
            arguments={"url": f"{browser_site_url}/index.html"},
        ),
        tool_call("api-browser-observe", name="browser_observe", arguments={}),
        finish("api-browser-finish"),
    ]

    def browser_components(registry: ToolRegistry) -> RuntimeComponents:
        return RuntimeComponents(
            analyzer=FakeAnalyzer(),
            planner=FakePlanner(),
            executor_model=cast(BaseChatModel, QueueFunctionCallingModel(responses)),
            verifier=FakeVerifier(),
        )

    service = TaskPilotService(
        PersistenceConfig.from_state_dir(tmp_path),
        browser_allowed=True,
        browser_config=BrowserConfig(
            headless=True,
            action_timeout_ms=1_000,
            navigation_timeout_ms=5_000,
            download_dir=tmp_path / "downloads",
            artifact_dir=tmp_path / "screenshots",
        ),
        components_factory=browser_components,
    )
    async with api_client(service) as client:
        response = await client.post(
            "/tasks",
            json={"task": "Open localhost", "enable_browser": True},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "completed", response.text
    print(
        "FASTAPI_BROWSER_SMOKE="
        + json.dumps(
            {
                "http_status": response.status_code,
                "task_status": response.json()["status"],
                "browser_tool_calls": 2,
            }
        )
    )
