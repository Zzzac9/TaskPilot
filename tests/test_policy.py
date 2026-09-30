"""Phase 8 deterministic Risk Policy 的离线单元测试。"""

from dataclasses import dataclass, field
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import ValidationError

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.models import PlanStep, TaskSpec
from taskpilot.policy import (
    ApprovalResponse,
    DefaultRiskPolicy,
    PolicyOutcome,
    RiskLevel,
    redact_preview,
)
from taskpilot.tools import ToolRegistry


@dataclass
class MetadataTool:
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    description: str = "Policy fixture tool"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object"}
    )

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("risk", "expected_level", "expected_outcome"),
    [
        ("low", RiskLevel.LOW, PolicyOutcome.ALLOW),
        ("medium", RiskLevel.MEDIUM, PolicyOutcome.ALLOW),
        ("high", RiskLevel.HIGH, PolicyOutcome.REQUIRE_APPROVAL),
    ],
)
async def test_explicit_local_risk_mapping(
    risk: str,
    expected_level: RiskLevel,
    expected_outcome: PolicyOutcome,
) -> None:
    decision = await DefaultRiskPolicy().assess(
        call_id=f"local-{risk}",
        step_id=1,
        tool=MetadataTool(
            name="local_fixture",
            metadata={"source": "local", "risk": risk},
        ),
        arguments={"value": "safe"},
    )

    assert decision.risk_level is expected_level
    assert decision.outcome is expected_outcome
    assert decision.tool_source == "local"


@pytest.mark.asyncio
async def test_unknown_local_tool_defaults_to_high() -> None:
    decision = await DefaultRiskPolicy().assess(
        call_id="unknown-1",
        step_id=1,
        tool=MetadataTool(name="unknown", metadata={}),
        arguments={},
    )

    assert decision.risk_level is RiskLevel.HIGH
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL


@pytest.mark.asyncio
async def test_mcp_annotation_trust_boundary() -> None:
    annotations = {
        "read_only_hint": True,
        "destructive_hint": False,
        "open_world_hint": False,
    }
    trusted = MetadataTool(
        name="trusted__read",
        metadata={
            "source": "mcp",
            "trust_tool_annotations": True,
            "annotations": annotations,
        },
    )
    untrusted = MetadataTool(
        name="untrusted__read",
        metadata={
            "source": "mcp",
            "trust_tool_annotations": False,
            "annotations": annotations,
        },
    )

    trusted_decision = await DefaultRiskPolicy().assess(
        call_id="mcp-trusted",
        step_id=1,
        tool=trusted,
        arguments={},
    )
    untrusted_decision = await DefaultRiskPolicy().assess(
        call_id="mcp-untrusted",
        step_id=1,
        tool=untrusted,
        arguments={},
    )

    assert trusted_decision.risk_level is RiskLevel.LOW
    assert trusted_decision.annotations_trusted is True
    assert untrusted_decision.risk_level is RiskLevel.HIGH
    assert untrusted_decision.annotations_trusted is False


@pytest.mark.asyncio
async def test_trusted_destructive_mcp_annotation_is_high() -> None:
    tool = MetadataTool(
        name="trusted__delete",
        metadata={
            "source": "mcp",
            "trust_tool_annotations": True,
            "annotations": {
                "read_only_hint": False,
                "destructive_hint": True,
                "open_world_hint": False,
            },
        },
    )

    decision = await DefaultRiskPolicy().assess(
        call_id="mcp-delete",
        step_id=1,
        tool=tool,
        arguments={"target": "fixture"},
    )

    assert decision.risk_level is RiskLevel.HIGH
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_sensitive_preview_is_recursive_and_does_not_mutate_arguments() -> None:
    arguments = {
        "recipient": "alice",
        "api_key": "super-secret",
        "payload": {"password": "private", "message": "hello"},
        "items": [{"authorization": "Bearer hidden", "value": 3}],
    }

    preview = redact_preview(arguments)

    assert preview == {
        "recipient": "alice",
        "api_key": "[REDACTED]",
        "payload": {"password": "[REDACTED]", "message": "hello"},
        "items": [{"authorization": "[REDACTED]", "value": 3}],
    }
    assert arguments["api_key"] == "super-secret"
    assert arguments["payload"]["password"] == "private"


def test_approval_response_forbids_argument_editing() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ApprovalResponse.model_validate(
            {
                "decision": "approve",
                "arguments": {"recipient": "mallory"},
            }
        )


class DenySequenceModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = responses
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "DenySequenceModel":
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def _model_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
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


@pytest.mark.asyncio
async def test_explicit_policy_deny_is_feedback_and_never_invokes_tool() -> None:
    tool = MetadataTool(
        name="blocked_local",
        metadata={
            "source": "local",
            "risk": "high",
            "policy_outcome": "deny",
            "policy_reason": "Host block fixture",
        },
    )
    model = DenySequenceModel(
        [
            _model_call("blocked_local", {}, "deny-1"),
            _model_call(
                FINISH_STEP_NAME,
                {
                    "summary": "The blocked action was avoided.",
                    "evidence": ["Policy denial was observed."],
                    "output": {"blocked": True},
                },
                "finish-2",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    executor = TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )

    result = await executor.execute(
        task_spec=TaskSpec(goal="Avoid a blocked action"),
        step=PlanStep(id=1, description="Use an allowed path"),
        task_results={},
        tool_calls=[],
    )

    assert result.outcome is not None
    assert result.outcome.claimed_complete is True
    assert result.tool_calls == []
    assert result.policy_decisions[0].outcome is PolicyOutcome.DENY
    denial_message = model.invocations[1][-1]
    assert isinstance(denial_message, ToolMessage)
    assert denial_message.tool_call_id == "deny-1"
    assert denial_message.status == "error"
