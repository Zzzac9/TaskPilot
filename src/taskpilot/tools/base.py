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
