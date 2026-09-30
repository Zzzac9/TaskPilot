"""MCP Server transport 的轻量、安全配置模型。"""

import json
from enum import Enum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class MCPTransport(str, Enum):
    """Phase 6 支持的非 deprecated MCP transports。"""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPServerConfig(BaseModel):
    """单个 MCP Server 的连接配置。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    server_id: str
    transport: MCPTransport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    # SecretStr 确保 repr 和 model_dump(mode="json") 不泄露显式传入的值。
    env: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    url: str | None = None
    # 只表示 Host 是否采信该 Server 的 Tool annotation 风险提示。
    trust_tool_annotations: bool = False

    @field_validator("server_id")
    @classmethod
    def validate_server_id(cls, value: str) -> str:
        """server_id 去除首尾空白后必须非空。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("server_id 不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerConfig":
        """确保每种 transport 只使用自己必需的连接字段。"""

        if self.transport is MCPTransport.STDIO:
            if not self.command or not self.command.strip():
                raise ValueError("STDIO MCP Server 必须提供 command")
            if self.url is not None:
                raise ValueError("STDIO MCP Server 不能提供 url")
        else:
            if not self.url or not self.url.startswith(("http://", "https://")):
                raise ValueError(
                    "Streamable HTTP MCP Server 必须提供 http(s) url"
                )
            if self.command is not None or self.args or self.env:
                raise ValueError(
                    "Streamable HTTP MCP Server 不能提供 command、args 或 env"
                )
        return self

    def resolved_env(self) -> dict[str, str]:
        """只解开用户显式配置的环境变量，不复制整个 os.environ。"""

        return {
            name: secret.get_secret_value() for name, secret in self.env.items()
        }


class MCPConfigFile(BaseModel):
    """CLI MCP JSON 文件的顶层结构。"""

    servers: list[MCPServerConfig]


def load_mcp_server_configs(path: Path) -> list[MCPServerConfig]:
    """从 JSON 文件读取 MCP Server 配置。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    config_file = MCPConfigFile.model_validate(raw)
    return config_file.servers
