"""结构化模型输出的有限解析重试测试。"""

from typing import Any, Sequence
from unittest.mock import MagicMock

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.output_parsers.openai_tools import PydanticToolsParser

from taskpilot.llm import (
    STRUCTURED_OUTPUT_ERROR_MAX_CHARS,
    StructuredOutputRetryExhausted,
    invoke_structured_with_retry,
)
from taskpilot.models import TaskSpec


INVALID_TASK_SPEC_JSON = (
    '{"goal":"搜索华为招聘","completion_criteria":["给出总结"],'
    '"constraints":{},"expected_output":总结前三个结果}'
)


def parse_task_spec_tool_arguments(arguments: str) -> TaskSpec:
    """走 LangChain 的真实 tools parser，而不是手工构造解析异常。"""

    parser = PydanticToolsParser(tools=[TaskSpec])
    return parser.invoke(
        AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call-invalid-json",
                        "type": "function",
                        "function": {
                            "name": "TaskSpec",
                            "arguments": arguments,
                        },
                    }
                ]
            },
        )
    )[0]


class InvalidJsonThenValidStructuredModel:
    def __init__(self, expected: TaskSpec) -> None:
        self.expected = expected
        self.messages: list[Sequence[Any]] = []

    def invoke(self, messages: Sequence[Any]) -> TaskSpec:
        self.messages.append(messages)
        if len(self.messages) == 1:
            return parse_task_spec_tool_arguments(INVALID_TASK_SPEC_JSON)
        return self.expected


def test_structured_output_retries_real_invalid_json_with_specific_feedback() -> None:
    expected = TaskSpec(goal="搜索华为招聘", completion_criteria=["给出总结"])
    structured_model = InvalidJsonThenValidStructuredModel(expected)
    model = MagicMock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    result = invoke_structured_with_retry(
        model,
        TaskSpec,
        [("system", "system"), ("human", "搜索华为招聘")],
    )

    assert result == expected
    assert len(structured_model.messages) == 2
    retry_messages = structured_model.messages[1]
    assert "反斜杠正确转义" in retry_messages[-1][1]
    assert "Expecting value" in retry_messages[-1][1]
    assert "expected_output" in retry_messages[-1][1]
    assert "只修复 JSON/schema 格式" in retry_messages[-1][1]


def test_structured_output_redacts_and_limits_parser_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-sensitive-parser-secret"
    monkeypatch.setenv("LLM_API_KEY", secret)
    oversized_error = f"bad arguments {secret} " + "x" * 5_000
    structured_model = MagicMock()
    structured_model.invoke.side_effect = [
        OutputParserException(oversized_error),
        TaskSpec(goal="done"),
    ]
    model = MagicMock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    invoke_structured_with_retry(model, TaskSpec, [("human", "task")])

    retry_messages = structured_model.invoke.call_args_list[1].args[0]
    retry_prompt = retry_messages[-1][1]
    assert secret not in retry_prompt
    assert "[REDACTED]" in retry_prompt
    assert "[truncated" in retry_prompt
    assert len(retry_prompt) < STRUCTURED_OUTPUT_ERROR_MAX_CHARS + 500


def test_structured_output_exhaustion_is_explicit_and_bounded() -> None:
    structured_model = MagicMock()
    structured_model.invoke.side_effect = [
        OutputParserException(f"invalid JSON attempt {attempt}")
        for attempt in range(1, 4)
    ]
    model = MagicMock(spec=BaseChatModel)
    model.with_structured_output.return_value = structured_model

    with pytest.raises(StructuredOutputRetryExhausted) as captured:
        invoke_structured_with_retry(
            model,
            TaskSpec,
            [("human", "task")],
        )

    assert structured_model.invoke.call_count == 3
    assert captured.value.attempts == 3
    assert captured.value.last_error.startswith("invalid JSON attempt 3")
    assert "TaskSpec structured output parsing failed after 3 attempts" in str(
        captured.value
    )
    assert isinstance(captured.value.__cause__, OutputParserException)


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
