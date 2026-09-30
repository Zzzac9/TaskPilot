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
from taskpilot.policy import DefaultRiskPolicy, PolicyOutcome, RiskLevel
from taskpilot.tools import ToolRegistry


SERVER_PATH = Path(__file__).parent / "fixtures" / "mcp_server.py"


def make_stdio_config(*, trust_tool_annotations: bool = False) -> MCPServerConfig:
    """使用当前 pytest 解释器启动同一环境中的官方 MCP Server。"""

    return MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command=sys.executable,
        args=["-u", str(SERVER_PATH)],
        trust_tool_annotations=trust_tool_annotations,
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
        assert set(discovered) == {
            "echo",
            "add",
            "destructive_dummy",
            "fail_tool",
        }
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
        assert echo_adapter.metadata["replay_safe"] is False
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
async def test_real_mcp_annotation_trust_policy() -> None:
    policy = DefaultRiskPolicy()
    async with MCPToolProvider(
        [make_stdio_config(trust_tool_annotations=True)]
    ) as trusted_provider:
        trusted = {
            tool.remote_tool_name: tool
            for tool in await trusted_provider.discover_tools()
        }
        read_decision = await policy.assess(
            call_id="trusted-read",
            step_id=1,
            tool=trusted["echo"],
            arguments={"text": "hello"},
        )
        destructive_decision = await policy.assess(
            call_id="trusted-destructive",
            step_id=1,
            tool=trusted["destructive_dummy"],
            arguments={"target": "fixture"},
        )
        assert trusted["echo"].metadata["replay_safe"] is True
        assert trusted["destructive_dummy"].metadata["replay_safe"] is False

    async with MCPToolProvider(
        [make_stdio_config(trust_tool_annotations=False)]
    ) as untrusted_provider:
        untrusted = {
            tool.remote_tool_name: tool
            for tool in await untrusted_provider.discover_tools()
        }
        untrusted_read = await policy.assess(
            call_id="untrusted-read",
            step_id=1,
            tool=untrusted["echo"],
            arguments={"text": "hello"},
        )
        assert untrusted["echo"].metadata["replay_safe"] is False

    assert read_decision.risk_level is RiskLevel.LOW
    assert read_decision.outcome is PolicyOutcome.ALLOW
    assert read_decision.annotations_trusted is True
    assert destructive_decision.risk_level is RiskLevel.HIGH
    assert destructive_decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert untrusted_read.risk_level is RiskLevel.HIGH
    assert untrusted_read.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert untrusted_read.annotations_trusted is False
    print(
        "MCP_POLICY_TRUST_SMOKE="
        + json.dumps(
            {
                "trusted_read": read_decision.risk_level.value,
                "trusted_destructive": destructive_decision.risk_level.value,
                "untrusted_read": untrusted_read.risk_level.value,
            },
            sort_keys=True,
        )
    )


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
