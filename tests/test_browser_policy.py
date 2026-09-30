"""真实 Chromium Browser 风险 preflight 与 Graph HITL 测试。"""

import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from taskpilot.browser import BrowserConfig, BrowserLocatorError, BrowserToolProvider
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
from taskpilot.policy import DefaultRiskPolicy, PolicyOutcome, RiskLevel
from taskpilot.policy.browser import classify_browser_press
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "FakeFunctionCallingModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
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


def finish(call_id: str) -> AIMessage:
    return call(
        FINISH_STEP_NAME,
        {
            "summary": "The localhost browser path was observed.",
            "evidence": ["Rendered DOM status was extracted."],
            "output": {"browser_fixture": True},
        },
        call_id,
    )


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            completion_criteria=["Observe the requested localhost DOM state"],
        )


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Interact with the deterministic localhost fixture",
                    success_criteria=["The expected DOM state is observed"],
                )
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
                    reason="Local deterministic Browser fixture accepted",
                )
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="Browser fixture completed")


def browser_config(test_name: str) -> BrowserConfig:
    root = Path("tests/.artifacts/browser_policy").resolve() / test_name
    return BrowserConfig(
        headless=True,
        action_timeout_ms=3_000,
        navigation_timeout_ms=5_000,
        download_dir=root / "downloads",
        artifact_dir=root / "screenshots",
    )


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "Run a localhost Browser policy fixture",
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


def make_graph(
    registry: ToolRegistry,
    model: FakeFunctionCallingModel,
    *,
    max_actions: int = 6,
) -> CompiledStateGraph:
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        max_actions_per_step=max_actions,
        policy=DefaultRiskPolicy(),
    )
    return build_graph(
        analyzer=FakeAnalyzer(),
        planner=FakePlanner(),
        executor=executor,
        verifier=FakeVerifier(),
        checkpointer=MemorySaver(),
    )


def interrupt_values(
    graph: CompiledStateGraph,
    config: dict[str, Any],
) -> list[Any]:
    snapshot = graph.get_state(config)
    return [item for task in snapshot.tasks for item in task.interrupts]


def locator(role: str, name: str) -> dict[str, Any]:
    return {"strategy": "role", "role": role, "name": name}


def test_search_get_enter_does_not_require_approval() -> None:
    risk, reason = classify_browser_press(
        {
            "pressed_key": "Enter",
            "is_submit": True,
            "in_form": True,
            "form_method": "get",
            "input_type": "search",
            "accessible_name": "在此处输入你的搜索",
        }
    )

    assert risk is RiskLevel.MEDIUM
    assert "搜索型 GET" in reason


def test_post_form_enter_still_requires_approval() -> None:
    risk, reason = classify_browser_press(
        {
            "pressed_key": "Enter",
            "is_submit": True,
            "in_form": True,
            "form_method": "post",
            "input_type": "text",
            "accessible_name": "Confirm order",
        }
    )

    assert risk is RiskLevel.HIGH
    assert "可能提交" in reason


@pytest.mark.asyncio
async def test_browser_click_preflight_risk_rules_without_side_effect(
    browser_site_url: str,
) -> None:
    registry = ToolRegistry()
    policy = DefaultRiskPolicy()
    async with BrowserToolProvider(browser_config("preflight")) as provider:
        provider.register_tools(registry)
        await registry.invoke("browser_navigate", {"url": browser_site_url})
        click_tool = registry.get("browser_click")
        navigate = await policy.assess(
            call_id="risk-nav",
            step_id=1,
            tool=registry.get("browser_navigate"),
            arguments={"url": browser_site_url},
        )
        fill = await policy.assess(
            call_id="risk-fill",
            step_id=1,
            tool=registry.get("browser_fill"),
            arguments={
                "locator": {"strategy": "label", "value": "Search Query"},
                "value": "gyms",
            },
        )
        press_enter = await policy.assess(
            call_id="risk-press-enter",
            step_id=1,
            tool=registry.get("browser_press"),
            arguments={
                "locator": {"strategy": "label", "value": "Search Query"},
                "key": "Enter",
            },
        )
        press_form_enter = await policy.assess(
            call_id="risk-press-form-enter",
            step_id=1,
            tool=registry.get("browser_press"),
            arguments={
                "locator": {"strategy": "label", "value": "Profile name"},
                "key": "Enter",
            },
        )
        apply = await policy.assess(
            call_id="risk-apply",
            step_id=1,
            tool=click_tool,
            arguments={"locator": locator("button", "Apply filters")},
        )
        submit = await policy.assess(
            call_id="risk-submit",
            step_id=1,
            tool=click_tool,
            arguments={"locator": locator("button", "Submit profile")},
        )
        delete = await policy.assess(
            call_id="risk-delete",
            step_id=1,
            tool=click_tool,
            arguments={"locator": locator("button", "Delete item")},
        )
        link = await policy.assess(
            call_id="risk-link",
            step_id=1,
            tool=click_tool,
            arguments={
                "locator": {
                    **locator("link", "Open details"),
                    "exact": True,
                }
            },
        )
        with pytest.raises(BrowserLocatorError, match="strict mode violation"):
            await policy.assess(
                call_id="risk-ambiguous",
                step_id=1,
                tool=click_tool,
                arguments={"locator": locator("button", "Duplicate action")},
            )
        submit_status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )
        delete_status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delete-status"}},
        )

    assert navigate.risk_level is RiskLevel.LOW
    assert fill.risk_level is RiskLevel.MEDIUM
    assert press_enter.risk_level is RiskLevel.MEDIUM
    assert press_enter.outcome is PolicyOutcome.ALLOW
    assert press_form_enter.risk_level is RiskLevel.HIGH
    assert press_form_enter.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert apply.risk_level is RiskLevel.MEDIUM
    assert submit.risk_level is RiskLevel.HIGH
    assert submit.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert delete.risk_level is RiskLevel.HIGH
    assert link.risk_level is RiskLevel.LOW
    assert submit_status["text"] == "Profile not submitted."
    assert delete_status["text"] == "Item exists."


@pytest.mark.asyncio
async def test_missing_click_preflight_is_recoverable_policy_feedback(
    browser_site_url: str,
) -> None:
    registry = ToolRegistry()
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "recover-nav"),
            call(
                "browser_click",
                {"locator": locator("button", "Missing button")},
                "recover-missing",
            ),
            call(
                "browser_click",
                {"locator": locator("button", "Apply filters")},
                "recover-click",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#results"}},
                "recover-extract",
            ),
            finish("recover-finish"),
        ]
    )
    async with BrowserToolProvider(browser_config("recover_preflight")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        result = await graph.ainvoke(
            initial_state("recover-preflight"),
            config={"configurable": {"thread_id": "recover-preflight"}},
        )

    assert result["status"] is TaskStatus.COMPLETED
    denied = [
        decision
        for decision in result["policy_decisions"]
        if decision.call_id == "recover-missing"
    ]
    assert len(denied) == 1
    assert denied[0].outcome is PolicyOutcome.DENY
    assert "无法可靠定位目标" in denied[0].reason
    assert all(call.call_id != "recover-missing" for call in result["tool_calls"])


@pytest.mark.asyncio
async def test_low_and_medium_browser_actions_do_not_interrupt(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "normal-nav"),
            call("browser_observe", {}, "normal-observe"),
            call(
                "browser_fill",
                {
                    "locator": {"strategy": "label", "value": "Search Query"},
                    "value": "gyms",
                },
                "normal-fill",
            ),
            call(
                "browser_click",
                {"locator": locator("button", "Apply filters")},
                "normal-click",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#results"}},
                "normal-extract",
            ),
            finish("normal-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("normal")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-normal"}}
        result = await graph.ainvoke(initial_state("browser-normal"), config=config)

    assert interrupt_values(graph, config) == []
    assert result["status"] is TaskStatus.COMPLETED
    assert [item.risk_level for item in result["policy_decisions"]] == [
        RiskLevel.LOW,
        RiskLevel.LOW,
        RiskLevel.MEDIUM,
        RiskLevel.MEDIUM,
        RiskLevel.LOW,
    ]


@pytest.mark.asyncio
async def test_browser_submit_waits_for_approval_then_executes(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "submit-nav"),
            call(
                "browser_fill",
                {
                    "locator": {"strategy": "label", "value": "Profile name"},
                    "value": "Alice",
                },
                "submit-fill",
            ),
            call(
                "browser_click",
                {"locator": locator("button", "Submit profile")},
                "submit-click",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#submit-status"}},
                "submit-extract",
            ),
            finish("submit-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("submit_approve")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-submit-approve"}}
        paused = await graph.ainvoke(
            initial_state("browser-submit-approve"), config=config
        )
        before = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )
        interrupts = interrupt_values(graph, config)
        final = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        after = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert before["text"] == "Profile not submitted."
    assert interrupts[0].value["risk_level"] == "high"
    assert after["text"] == "Profile submitted."
    assert final["status"] is TaskStatus.COMPLETED
    assert [record.tool_name for record in final["tool_calls"]].count(
        "browser_click"
    ) == 1
    print(
        "BROWSER_SUBMIT_APPROVAL_SMOKE="
        + json.dumps(
            {
                "status_before": before["text"],
                "paused_status": paused["status"].value,
                "interrupt": interrupts[0].value,
                "status_after": after["text"],
                "final_status": final["status"].value,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.asyncio
async def test_browser_submit_rejection_never_changes_dom(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "reject-nav"),
            call(
                "browser_click",
                {"locator": locator("button", "Submit profile")},
                "reject-submit",
            ),
            call(
                "browser_extract_text",
                {"locator": {"strategy": "css", "value": "#submit-status"}},
                "reject-extract",
            ),
            finish("reject-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("submit_reject")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-submit-reject"}}
        paused = await graph.ainvoke(
            initial_state("browser-submit-reject"), config=config
        )
        final = await graph.ainvoke(
            Command(resume={"decision": "reject", "reason": "Do not submit"}),
            config=config,
        )
        status = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#submit-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert status["text"] == "Profile not submitted."
    assert final["status"] is TaskStatus.COMPLETED
    assert "browser_click" not in [record.tool_name for record in final["tool_calls"]]


@pytest.mark.asyncio
async def test_browser_delete_is_high_and_only_changes_dom_after_approval(
    browser_site_url: str,
) -> None:
    model = FakeFunctionCallingModel(
        [
            call("browser_navigate", {"url": browser_site_url}, "delete-nav"),
            call(
                "browser_click",
                {"locator": locator("button", "Delete item")},
                "delete-click",
            ),
            finish("delete-finish"),
        ]
    )
    registry = ToolRegistry()
    async with BrowserToolProvider(browser_config("delete")) as provider:
        provider.register_tools(registry)
        graph = make_graph(registry, model)
        config = {"configurable": {"thread_id": "browser-delete"}}
        paused = await graph.ainvoke(initial_state("browser-delete"), config=config)
        before = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delete-status"}},
        )
        interrupts = interrupt_values(graph, config)
        final = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=config,
        )
        after = await registry.invoke(
            "browser_extract_text",
            {"locator": {"strategy": "css", "value": "#delete-status"}},
        )

    assert paused["status"] is TaskStatus.WAITING_APPROVAL
    assert interrupts[0].value["risk_level"] == "high"
    assert before["text"] == "Item exists."
    assert after["text"] == "Item deleted."
    assert final["status"] is TaskStatus.COMPLETED
    print(
        "BROWSER_DELETE_APPROVAL_SMOKE="
        + json.dumps(
            {
                "before": before["text"],
                "risk": interrupts[0].value["risk_level"],
                "after": after["text"],
            },
            ensure_ascii=True,
        )
    )
