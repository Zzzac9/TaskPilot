"""本地 ToolRegistry 与 JSON Schema 参数校验。"""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from taskpilot.tools.base import Tool, tool_to_function_schema


class ToolRegistryError(ValueError):
    """ToolRegistry 可预期配置或调用错误的基类。"""


class DuplicateToolError(ToolRegistryError):
    """注册重复工具名称时抛出。"""


class UnknownToolError(ToolRegistryError):
    """请求未注册工具时抛出。"""


class ToolArgumentsError(ToolRegistryError):
    """工具参数不符合 input_schema 时抛出。"""


class ToolRegistry:
    """保存可用工具并负责调用前的确定性参数校验。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册名称唯一且 input_schema 合法的工具。"""

        if tool.name == "finish_step":
            raise DuplicateToolError("finish_step 是 Executor 内部控制动作，不能注册")
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具名称已注册: {tool.name}")
        try:
            Draft202012Validator.check_schema(tool.input_schema)
        except SchemaError as exc:
            raise ToolRegistryError(
                f"工具 {tool.name} 的 input_schema 无效: {exc.message}"
            ) from exc
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """按名称返回工具；未知名称会明确失败。"""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"未注册的工具: {name}") from exc

    def list_tools(self) -> list[Tool]:
        """按注册顺序返回工具列表副本。"""

        return list(self._tools.values())

    def list_function_schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI-compatible Function schemas。"""

        return [tool_to_function_schema(tool) for tool in self._tools.values()]

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        """校验参数后异步调用工具，不吞掉工具自身异常。"""

        tool = self.get(name)
        self.validate_arguments(name, arguments)
        return await tool.invoke(arguments)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        """只做确定性 schema 校验，不执行 Tool。"""

        tool = self.get(name)
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise ToolArgumentsError(
                f"工具 {name} 参数不符合 input_schema{location}: {exc.message}"
            ) from exc
