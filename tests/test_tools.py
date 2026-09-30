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
