"""真实 TaskExecutor/Graph 到真实 Chromium Browser Tools 的离线测试。"""

import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    ToolCallStatus,
    VerificationResult,
)
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    """按预设动作驱动真实 Executor，并记录完整 ToolMessage 链。"""

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


def _finish(
    output: dict[str, Any],
    call_id: str,
    *,
    summary: str = "The local browser step produced verified DOM evidence.",
) -> AIMessage:
    return _call(
        FINISH_STEP_NAME,
        {
            "summary": summary,
            "evidence": ["Text was extracted from the rendered local DOM."],
            "output": output,
        },
        call_id,
    )


def _config(test_name: str) -> BrowserConfig:
    root = Path("tests/.artifacts/browser_executor").resolve() / test_name
    return BrowserConfig(
        headless=True,
        action_timeout_ms=3_000,
        navigation_timeout_ms=5_000,
        download_dir=root / "downloads",
        artifact_dir=root / "screenshots",
    )


def _spec() -> TaskSpec:
    return TaskSpec(
        goal="Use the local page to find two gym candidates",
        constraints={"source": "localhost"},
        completion_criteria=["Return Harbor Gym and Peak Fitness"],
    )


def _step() -> PlanStep:
    return PlanStep(
        id=1,
        description="Open the local page and extract two candidate names",
        success_criteria=["Two rendered candidate names are extracted"],
    )


@pytest.mark.asyncio
async def test_executor_drives_real_browser_and_preserves_tool_message_pairing(
    browser_site_url: str,
) -> None:
    actions = [
        _call(
            "browser_navigate",
            {"url": f"{browser_site_url}/index.html"},
            "browser-1",
        ),
        _call("browser_observe", {}, "browser-2"),
        _call(
            "browser_fill",
            {
                "locator": {"strategy": "label", "value": "Search Query"},
                "value": "gyms",
            },
            "browser-3",
        ),
        _call(
            "browser_click",
            {
                "locator": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Apply filters",
                }
            },
            "browser-4",
        ),
        _call(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#results"}},
            "browser-5",
        ),
        _finish(
            {"candidates": ["Harbor Gym", "Peak Fitness"]},
            "browser-finish",
        ),
    ]
    model = FakeFunctionCallingModel(actions)
    registry = ToolRegistry()

    async with BrowserToolProvider(_config("executor_e2e")) as provider:
        provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=6,
        )
        result = await executor.execute(
            task_spec=_spec(),
            step=_step(),
            task_results={},
            tool_calls=[],
        )
        rendered = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#results"}},
        )

    assert result.fatal_error is False
    assert result.outcome.claimed_complete is True
    assert result.outcome.action_count == 6
    assert result.outcome.output == {
        "candidates": ["Harbor Gym", "Peak Fitness"]
    }
    assert "gyms" in rendered["text"]
    assert "Harbor Gym" in rendered["text"]
    assert [record.tool_name for record in result.tool_calls] == [
        "browser_navigate",
        "browser_observe",
        "browser_fill",
        "browser_click",
        "browser_extract_text",
    ]
    assert all(
        record.status is ToolCallStatus.SUCCESS for record in result.tool_calls
    )
    for index, expected_id in enumerate(
        ["browser-1", "browser-2", "browser-3", "browser-4", "browser-5"],
        start=1,
    ):
        observation = model.invocations[index][-1]
        assert isinstance(observation, ToolMessage)
        assert observation.tool_call_id == expected_id
    print(
        "EXECUTOR_BROWSER_SMOKE="
        + json.dumps(
            {
                "tool_calls": [
                    record.model_dump(mode="json") for record in result.tool_calls
                ],
                "outcome": result.outcome.model_dump(mode="json"),
                "rendered_text": rendered["text"],
            },
            ensure_ascii=False,
        )
    )


@pytest.mark.asyncio
async def test_dom_locator_failure_can_observe_and_recover(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            _call(
                "browser_navigate",
                {"url": f"{browser_site_url}/index.html"},
                "recover-nav",
            ),
            _call(
                "browser_click",
                {
                    "locator": {
                        "strategy": "role",
                        "role": "button",
                        "name": "Does Not Exist",
                    }
                },
                "recover-wrong",
            ),
            _call("browser_observe", {}, "recover-observe"),
            _call(
                "browser_click",
                {
                    "locator": {
                        "strategy": "role",
                        "role": "button",
                        "name": "Apply filters",
                    }
                },
                "recover-correct",
            ),
            _finish({"recovered": True}, "recover-finish"),
        ]
    )
    registry = ToolRegistry()

    async with BrowserToolProvider(_config("recovery")) as provider:
        provider.register_tools(registry)
        result = await TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=5,
        ).execute(
            task_spec=_spec(),
            step=_step(),
            task_results={},
            tool_calls=[],
        )

    assert result.fatal_error is False
    assert result.outcome.claimed_complete is True
    assert [record.status for record in result.tool_calls] == [
        ToolCallStatus.SUCCESS,
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCESS,
        ToolCallStatus.SUCCESS,
    ]
    assert "Timeout 3000ms exceeded" in (result.tool_calls[1].error or "")
    error_observation = model.invocations[2][-1]
    assert isinstance(error_observation, ToolMessage)
    assert error_observation.tool_call_id == "recover-wrong"
    assert error_observation.status == "error"
    print(
        "DOM_RECOVERY_SMOKE="
        + json.dumps(
            {
                "tool_calls": [
                    record.model_dump(mode="json") for record in result.tool_calls
                ],
                "fatal_error": result.fatal_error,
                "outcome": result.outcome.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
    )


class _FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            constraints={"source": "localhost"},
            completion_criteria=["Return two candidate names"],
        )


class _FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[_step()])


class _FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="The real browser extraction returned two names.",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(
            completed=True,
            reason="The verified StepResult contains both candidate names.",
        )


def _initial_state() -> TaskState:
    return {
        "task_id": "phase-7-browser-graph",
        "user_input": "打开本地页面并提取两个候选名称",
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
        "status": TaskStatus.CREATED,
        "error": None,
    }


@pytest.mark.asyncio
async def test_full_graph_completes_with_real_browser_executor(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            _call(
                "browser_navigate",
                {"url": f"{browser_site_url}/details.html"},
                "graph-nav",
            ),
            _call("browser_observe", {}, "graph-observe"),
            _call(
                "browser_extract_text",
                {},
                "graph-extract",
            ),
            _finish(
                {"candidates": ["Harbor Gym", "Peak Fitness"]},
                "graph-finish",
            ),
        ]
    )
    registry = ToolRegistry()

    async with BrowserToolProvider(_config("full_graph")) as provider:
        provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=4,
        )
        result = await build_graph(
            analyzer=_FakeAnalyzer(),
            planner=_FakePlanner(),
            executor=executor,
            verifier=_FakeVerifier(),
        ).ainvoke(_initial_state())

    assert result["status"] is TaskStatus.COMPLETED
    assert result["error"] is None
    assert result["plan"][0].status.value == "completed"
    assert [record.tool_name for record in result["tool_calls"]] == [
        "browser_navigate",
        "browser_observe",
        "browser_extract_text",
    ]
    assert result["step_results"][0].output == {
        "candidates": ["Harbor Gym", "Peak Fitness"]
    }
    assert result["verification"].completed is True
    print(
        "FULL_GRAPH_BROWSER_SMOKE="
        + json.dumps(
            {
                "plan": [step.model_dump(mode="json") for step in result["plan"]],
                "tool_calls": [
                    record.model_dump(mode="json")
                    for record in result["tool_calls"]
                ],
                "step_results": [
                    item.model_dump(mode="json")
                    for item in result["step_results"]
                ],
                "status": result["status"].value,
                "error": result["error"],
            },
            ensure_ascii=False,
        )
    )
