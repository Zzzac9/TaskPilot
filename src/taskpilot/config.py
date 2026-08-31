"""TaskPilot 的轻量配置模型。"""

import os

from pydantic import BaseModel


class Settings(BaseModel):
    """为本地运行预留的基础配置。"""

    environment: str = "development"
    log_level: str = "INFO"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取当前阶段所需配置。"""

        return cls(
            environment=os.getenv("TASKPILOT_ENVIRONMENT", "development"),
            log_level=os.getenv("TASKPILOT_LOG_LEVEL", "INFO"),
            llm_api_key=os.getenv("LLM_API_KEY"),
            llm_base_url=os.getenv("LLM_BASE_URL"),
            llm_model=os.getenv("LLM_MODEL"),
        )


def redact_sensitive_values(message: str) -> str:
    """从错误信息中移除当前环境已知的敏感配置值。"""

    api_key = os.getenv("LLM_API_KEY")
    if api_key:
        return message.replace(api_key, "[REDACTED]")
    return message
