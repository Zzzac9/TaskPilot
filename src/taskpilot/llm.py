"""OpenAI-compatible Chat Model 的轻量创建入口。"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from taskpilot.config import Settings


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

    model_options: dict[str, str] = {
        "api_key": active_settings.llm_api_key,
        "model": active_settings.llm_model,
    }
    if active_settings.llm_base_url:
        model_options["base_url"] = active_settings.llm_base_url
    return ChatOpenAI(**model_options)
