"""OpenAI-compatible Chat Model 的轻量创建入口。"""

from typing import Any, Sequence
from urllib.parse import urlsplit

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError
from langchain_openai import ChatOpenAI

from taskpilot.config import Settings


STRUCTURED_OUTPUT_METHOD = "function_calling"
STRUCTURED_OUTPUT_MAX_ATTEMPTS = 3
STRUCTURED_OUTPUT_RETRY_PROMPT = """上一轮结构化输出无法解析。请重新完整生成一次。
严格使用要求的 schema，不要输出 schema 之外的文字。
JSON 字符串内容中的 ASCII 双引号必须使用反斜杠正确转义；更推荐改用中文引号“……”。"""


def bind_structured_output(model: BaseChatModel, schema: type[Any]) -> Any:
    """显式使用兼容 OpenAI/DeepSeek 的 Tool Calling 结构化输出。"""

    return model.with_structured_output(
        schema,
        method=STRUCTURED_OUTPUT_METHOD,
    )


def invoke_structured_with_retry(
    model: BaseChatModel,
    schema: type[Any],
    messages: Sequence[Any],
    *,
    max_attempts: int = STRUCTURED_OUTPUT_MAX_ATTEMPTS,
) -> Any:
    """只对模型结构化解析/校验错误做有限重试。"""

    if max_attempts < 1:
        raise ValueError("max_attempts 必须至少为 1")
    structured_model = bind_structured_output(model, schema)
    active_messages = list(messages)
    for attempt in range(1, max_attempts + 1):
        try:
            raw_result = structured_model.invoke(active_messages)
            return (
                raw_result
                if isinstance(raw_result, schema)
                else schema.model_validate(raw_result)
            )
        except (OutputParserException, ValidationError):
            if attempt >= max_attempts:
                raise
            active_messages = [
                *messages,
                (
                    "human",
                    f"{STRUCTURED_OUTPUT_RETRY_PROMPT}\n当前是第 {attempt + 1} 次尝试。",
                ),
            ]
    raise AssertionError("unreachable")


def create_chat_model(settings: Settings | None = None) -> BaseChatModel:
    """根据环境配置创建项目使用的 Chat Model。"""

    active_settings = settings or Settings.from_env()
    missing_variables: list[str] = []
    if not active_settings.llm_api_key:
        missing_variables.append("LLM_API_KEY")
    if not active_settings.llm_model:
        missing_variables.append("LLM_MODEL")
    if missing_variables:
        missing = ", ".join(missing_variables)
        raise ValueError(f"缺少必要的 LLM 环境变量: {missing}")

    model_options: dict[str, Any] = {
        "api_key": active_settings.llm_api_key,
        "model": active_settings.llm_model,
        # Phase 12 真实 Benchmark 固定为 provider 支持的最低随机性配置。
        "temperature": 0,
    }
    if active_settings.llm_base_url:
        model_options["base_url"] = active_settings.llm_base_url
        hostname = urlsplit(active_settings.llm_base_url).hostname or ""
        if hostname == "deepseek.com" or hostname.endswith(".deepseek.com"):
            # DeepSeek 默认开启 Thinking Mode；该模式会拒绝结构化输出使用的
            # 指定 tool_choice，也要求在多轮工具调用中回传 reasoning_content。
            model_options["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**model_options)
