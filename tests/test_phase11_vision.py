"""Phase 11 DOM-first / Vision-fallback 的离线与真实 Chromium 验收。"""

import base64
import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from taskpilot.browser import (
    BrowserConfig,
    BrowserLocatorError,
    BrowserToolProvider,
    BrowserVisionError,
    is_vision_fallback_eligible,
)
from taskpilot.cli import create_initial_state
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
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
from taskpilot.policy import DefaultRiskPolicy, RiskLevel
from taskpilot.tools import ToolRegistry
from taskpilot.trace import InMemoryTraceRecorder, TraceEventType
from taskpilot.vision import (
    MultimodalVisionTargeter,
    VisionConfig,
    VisionTarget,
    VisionTargetRequest,
    validate_vision_target,
)


class FakeVisionTargeter:
    """只返回 fixture 中 canvas 按钮中心点，不读取或保存图片。"""

    def __init__(self, target: VisionTarget | None = None) -> None:
        self.target = target or VisionTarget(
            found=True,
            x=201,
            y=145,
            confidence=0.96,
            reason="Canvas Action rectangle is visible",
        )
        self.calls: list[dict[str, Any]] = []

    async def locate(
        self,
        *,
        screenshot: bytes,
        request: VisionTargetRequest,
    ) -> VisionTarget:
        self.calls.append(
            {
                "screenshot_bytes": len(screenshot),
                "request": request.model_copy(deep=True),
            }
        )
        return self.target.model_copy(deep=True)


class _StructuredVisionModel:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.messages: list[Sequence[BaseMessage]] = []
        self.schema: Any = None
        self.method: str | None = None

    def with_structured_output(
        self, schema: Any, **kwargs: Any
    ) -> "_StructuredVisionModel":
        self.schema = schema
        self.method = kwargs.get("method")
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> dict[str, Any]:
        self.messages.append(messages)
        return self.result


class _FunctionCallingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "_FunctionCallingModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self.responses.pop(0)


class _Analyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["Canvas status observed"])


class _Planner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Use the visible canvas action",
                    success_criteria=["Canvas click status is observed"],
                )
            ]
        )


class _Verifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="Deterministic canvas fixture accepted",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="Canvas fixture completed")


def _call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
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


def _finish(call_id: str) -> AIMessage:
    return _call(
        FINISH_STEP_NAME,
        {
            "summary": "Canvas interaction was checked.",
            "evidence": ["The localhost canvas status was extracted."],
            "output": {"canvas": True},
        },
        call_id,
    )


def _canvas_locator() -> dict[str, Any]:
    return {
        "strategy": "role",
        "role": "button",
        "name": "Canvas Action",
    }


def _browser_config(tmp_path: Path, name: str) -> BrowserConfig:
    root = tmp_path / name
    return BrowserConfig(
        headless=True,
        action_timeout_ms=250,
        navigation_timeout_ms=5_000,
        download_dir=root / "downloads",
        artifact_dir=root / "screenshots",
    )


def _vision_config() -> VisionConfig:
    return VisionConfig(enabled=True, min_confidence=0.80)


def test_vision_models_enforce_coordinate_and_confidence_contract() -> None:
    request = VisionTargetRequest(
        target_description="button",
        viewport_width=100,
        viewport_height=80,
    )
    with pytest.raises(ValidationError, match="必须同时存在"):
        VisionTarget(found=True, x=1, confidence=0.9, reason="partial")
    with pytest.raises(ValidationError, match="必须为 null"):
        VisionTarget(found=False, x=1, y=2, confidence=0.2, reason="absent")
    with pytest.raises(ValidationError):
        VisionTarget(found=False, confidence=1.1, reason="invalid")
    with pytest.raises(ValueError, match="超出 viewport"):
        validate_vision_target(
            VisionTarget(found=True, x=100, y=20, confidence=0.9, reason="edge"),
            request,
        )


def test_vision_fallback_eligibility_excludes_runtime_failures() -> None:
    assert is_vision_fallback_eligible(
        BrowserLocatorError("Timeout 250ms waiting for target")
    )
    assert is_vision_fallback_eligible(
        BrowserLocatorError("strict mode violation: resolved to 2 elements")
    )
    assert not is_vision_fallback_eligible(
        PlaywrightError("Target page, context or browser has been closed")
    )
    assert not is_vision_fallback_eligible(PlaywrightError("navigation failed"))
    assert not is_vision_fallback_eligible(RuntimeError("unrelated bug"))


@pytest.mark.asyncio
async def test_multimodal_targeter_uses_png_data_url_and_untrusted_prompt() -> None:
    model = _StructuredVisionModel(
        {
            "found": True,
            "x": 12,
            "y": 23,
            "confidence": 0.91,
            "reason": "visible",
        }
    )
    targeter = MultimodalVisionTargeter(
        VisionConfig(enabled=True, max_screenshot_bytes=100),
        model=cast(BaseChatModel, model),
    )
    request = VisionTargetRequest(
        target_description="Canvas Action",
        role="button",
        accessible_name="Canvas Action",
        viewport_width=100,
        viewport_height=80,
    )
    target = await targeter.locate(screenshot=b"\x89PNG\r\n", request=request)

    assert target.found is True
    assert model.schema is VisionTarget
    assert model.method == "function_calling"
    system = str(model.messages[0][0].content)
    assert "untrusted visual environment data" in system
    content = model.messages[0][1].content
    assert isinstance(content, list)
    image_url = cast(dict[str, Any], content[1])["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert base64.b64decode(image_url.split(",", 1)[1]) == b"\x89PNG\r\n"


@pytest.mark.asyncio
async def test_dom_click_never_calls_vision(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    fake = FakeVisionTargeter()
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(tmp_path, "dom"),
        vision_config=_vision_config(),
        vision_targeter=fake,
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke("browser_navigate", {"url": browser_site_url})
        result = await registry.invoke(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Apply filters",
                }
            },
        )

    assert result["interaction_mode"] == "dom"
    assert fake.calls == []
    print(
        "DOM_FIRST_SMOKE=" + json.dumps({"dom_success": True, "vision_called": False})
    )


@pytest.mark.asyncio
async def test_real_chromium_canvas_uses_vision_only_after_dom_failure(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    fake = FakeVisionTargeter()
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(tmp_path, "canvas"),
        vision_config=_vision_config(),
        vision_targeter=fake,
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/canvas.html"}
        )
        before = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#canvas-status"}},
        )
        result = await registry.invoke("browser_click", {"locator": _canvas_locator()})
        after = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#canvas-status"}},
        )

    assert before["text"] == "Canvas not clicked."
    assert after["text"] == "Canvas clicked."
    assert len(fake.calls) == 1
    assert result["interaction_mode"] == "vision"
    assert result["confidence"] == 0.96
    print(
        "VISION_FALLBACK_BROWSER_SMOKE="
        + json.dumps(
            {
                "dom_target_found": False,
                "vision_called": True,
                "interaction_mode": result["interaction_mode"],
                "canvas_clicked": after["text"] == "Canvas clicked.",
            }
        )
    )


@pytest.mark.asyncio
async def test_disabled_or_rejected_vision_never_clicks(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    disabled = FakeVisionTargeter()
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(tmp_path, "disabled"),
        vision_config=VisionConfig(enabled=False),
        vision_targeter=disabled,
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/canvas.html"}
        )
        with pytest.raises(BrowserLocatorError):
            await registry.invoke("browser_click", {"locator": _canvas_locator()})
    assert disabled.calls == []

    rejected = FakeVisionTargeter(
        VisionTarget(
            found=True,
            x=201,
            y=145,
            confidence=0.50,
            reason="uncertain",
        )
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(tmp_path, "rejected"),
        vision_config=_vision_config(),
        vision_targeter=rejected,
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/canvas.html"}
        )
        with pytest.raises(BrowserVisionError, match="confidence"):
            await registry.invoke("browser_click", {"locator": _canvas_locator()})
        status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#canvas-status"}},
        )
    assert status["text"] == "Canvas not clicked."


@pytest.mark.asyncio
async def test_vision_click_graph_requires_approval_and_records_metadata_only(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    fake = FakeVisionTargeter()
    model = _FunctionCallingModel(
        [
            _call("browser_click", {"locator": _canvas_locator()}, "vision-click"),
            _call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#canvas-status"}},
                "vision-status",
            ),
            _finish("vision-finish"),
        ]
    )
    trace = InMemoryTraceRecorder()
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(tmp_path, "approval"),
        vision_config=_vision_config(),
        vision_targeter=fake,
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/canvas.html"}
        )
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            policy=DefaultRiskPolicy(),
        )
        graph = build_graph(
            analyzer=_Analyzer(),
            planner=_Planner(),
            executor=executor,
            verifier=_Verifier(),
            checkpointer=MemorySaver(),
            trace_recorder=trace,
        )
        config = {"configurable": {"thread_id": "vision-approval"}}
        state = create_initial_state("Click the Canvas Action")
        state["task_id"] = "vision-approval"
        paused = await graph.ainvoke(state, config=config)
        before = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#canvas-status"}},
        )
        snapshot = await graph.aget_state(config)
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        final = await graph.ainvoke(
            Command(resume={"decision": "approve"}), config=config
        )
        after = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#canvas-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert paused["policy_decisions"][-1].risk_level is RiskLevel.HIGH
    assert interrupts[0].value["arguments_preview"] == {"locator": _canvas_locator()}
    assert "x" not in interrupts[0].value["arguments_preview"]
    assert before["text"] == "Canvas not clicked."
    assert fake.calls == [fake.calls[0]]
    assert after["text"] == "Canvas clicked."
    assert final["status"] is TaskStatus.COMPLETED
    vision_events = [
        event
        for event in trace.events
        if event.event_type
        in {
            TraceEventType.VISION_FALLBACK_REQUESTED,
            TraceEventType.VISION_TARGET_FOUND,
            TraceEventType.VISION_TARGET_REJECTED,
        }
    ]
    assert [event.event_type for event in vision_events] == [
        TraceEventType.VISION_FALLBACK_REQUESTED,
        TraceEventType.VISION_TARGET_FOUND,
    ]
    serialized = json.dumps([event.model_dump(mode="json") for event in vision_events])
    assert "data:image" not in serialized
    assert "iVBOR" not in serialized
    print(
        "VISION_APPROVAL_SMOKE="
        + json.dumps(
            {
                "side_effect_before_approval": False,
                "risk": "high",
                "side_effect_after_approval": True,
            }
        )
    )


@pytest.mark.asyncio
async def test_rejected_vision_approval_never_invokes_targeter(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    fake = FakeVisionTargeter()
    model = _FunctionCallingModel(
        [
            _call("browser_click", {"locator": _canvas_locator()}, "reject-click"),
            _call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#canvas-status"}},
                "reject-status",
            ),
            _finish("reject-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(
        _browser_config(tmp_path, "reject"),
        vision_config=_vision_config(),
        vision_targeter=fake,
    ) as provider:
        provider.register_tools(registry)
        await registry.invoke(
            "browser_navigate", {"url": f"{browser_site_url}/canvas.html"}
        )
        graph = build_graph(
            analyzer=_Analyzer(),
            planner=_Planner(),
            executor=TaskExecutor(
                registry=registry,
                model=cast(BaseChatModel, model),
                policy=DefaultRiskPolicy(),
            ),
            verifier=_Verifier(),
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": "vision-reject"}}
        state = create_initial_state("Do not click without approval")
        state["task_id"] = "vision-reject"
        await graph.ainvoke(state, config=config)
        final = await graph.ainvoke(
            Command(resume={"decision": "reject", "reason": "No coordinate click"}),
            config=config,
        )
        status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#canvas-status"}},
        )

    assert fake.calls == []
    assert status["text"] == "Canvas not clicked."
    assert final["status"] is TaskStatus.COMPLETED


@pytest.mark.skipif(
    __import__("os").getenv("TASKPILOT_RUN_VISION_INTEGRATION") != "1",
    reason="需要显式 opt-in 与真实 vision provider 凭证",
)
@pytest.mark.asyncio
async def test_optional_real_vision_provider_contract() -> None:
    """只验证 opt-in provider 调用；默认回归绝不访问网络或消耗付费额度。"""

    config = VisionConfig.from_env()
    targeter = MultimodalVisionTargeter(config)
    request = VisionTargetRequest(
        target_description="No target exists in this 1x1 transparent PNG",
        viewport_width=1,
        viewport_height=1,
    )
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL+WQAAAABJRU5ErkJggg=="
    )
    result = await targeter.locate(screenshot=png, request=request)
    assert isinstance(result, VisionTarget)
