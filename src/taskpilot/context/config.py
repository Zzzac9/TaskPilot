"""Context Builder 的确定性字符预算配置。"""

from pydantic import BaseModel, Field


class ContextConfig(BaseModel):
    """字符预算不是 tokenizer 估算，只用于稳定限制上下文体积。"""

    max_total_chars: int = Field(default=24_000, ge=1)
    max_single_observation_chars: int = Field(default=4_000, ge=1)
    max_recent_tool_calls: int = Field(default=6, ge=0)
    max_dependency_result_chars: int = Field(default=6_000, ge=1)
    max_task_results_chars: int = Field(default=4_000, ge=1)
    max_feedback_chars: int = Field(default=3_000, ge=1)
