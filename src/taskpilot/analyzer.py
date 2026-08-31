"""将用户自然语言任务转换为结构化 TaskSpec。"""

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.llm import create_chat_model
from taskpilot.models import TaskSpec


ANALYZER_SYSTEM_PROMPT = """你是 Task Analyzer，只负责把用户任务结构化为 TaskSpec。
不要规划或执行任务，不要选择或调用工具，也不要添加用户未表达的硬约束。
constraints 应忠实保留用户明确提出的数字、位置、格式和时间等要求；未指定的要求不要虚构。
用户没有指定具体输出格式时，expected_output 应为 null。
completion_criteria 必须描述用户目标真正完成时可验证的成功条件，不能写成搜索、点击、提取等执行步骤。
输出必须符合 TaskSpec schema，包含 goal、constraints、expected_output 和 completion_criteria。"""


class TaskAnalyzerLike(Protocol):
    """真实 Analyzer 与离线 Stub 共同遵循的最小接口。"""

    def analyze(self, user_input: str) -> TaskSpec:
        """分析用户输入并返回结构化任务描述。"""

        ...


class TaskAnalyzer:
    """使用 Chat Model Structured Output 分析用户任务。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def analyze(self, user_input: str) -> TaskSpec:
        """调用模型的 Structured Output 能力生成 TaskSpec。"""

        if self._model is None:
            # 延迟创建模型，使导入模块和离线构图不要求存在 API Key。
            self._model = create_chat_model()
        structured_model = self._model.with_structured_output(TaskSpec)
        result = structured_model.invoke(
            [
                ("system", ANALYZER_SYSTEM_PROMPT),
                ("human", user_input),
            ]
        )
        if isinstance(result, TaskSpec):
            return result
        return TaskSpec.model_validate(result)
