"""结构化模型输出的有限解析重试测试。"""

from unittest.mock import MagicMock

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.llm import invoke_structured_with_retry
from taskpilot.models import TaskSpec


def test_structured_output_retries_parser_error_then_returns_valid_model() -> None:
    expected = TaskSpec(goal="搜索华为招聘", completion_criteria=["给出总结"])
    structured_model = MagicMock()
    structured_model.invoke.side_effect = [
        OutputParserException("invalid nested quotes"),
        expected,
    ]
    model = MagicMock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    result = invoke_structured_with_retry(
        model,
        TaskSpec,
        [("system", "system"), ("human", "搜索华为招聘")],
    )

    assert result == expected
    assert structured_model.invoke.call_count == 2
    retry_messages = structured_model.invoke.call_args_list[1].args[0]
    assert "反斜杠正确转义" in retry_messages[-1][1]


def test_structured_output_does_not_retry_non_parser_failure() -> None:
    structured_model = MagicMock()
    structured_model.invoke.side_effect = RuntimeError("authentication failed")
    model = MagicMock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    with pytest.raises(RuntimeError, match="authentication failed"):
        invoke_structured_with_retry(
            model,
            TaskSpec,
            [("human", "task")],
        )

    assert structured_model.invoke.call_count == 1
