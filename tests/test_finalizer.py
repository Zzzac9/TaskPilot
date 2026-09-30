"""最终答案生成层测试。"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from taskpilot.finalizer import FinalAnswerGenerator
from taskpilot.models import StepResult, TaskSpec, VerificationResult


def test_final_answer_generator_returns_user_facing_model_answer() -> None:
    model = FakeListChatModel(
        responses=[
            "## 搜索总结\n\n1. [TaskPilot](https://example.com)：任务管理工具。"
        ]
    )
    generator = FinalAnswerGenerator(model=model)

    answer = generator.generate(
        task_spec=TaskSpec(
            goal="总结搜索结果",
            completion_criteria=["给出完整总结"],
        ),
        step_results=[
            StepResult(
                step_id=1,
                summary="已提取结果",
                output={"title": "TaskPilot", "url": "https://example.com"},
                evidence=["浏览器提取结果"],
            )
        ],
        task_results={},
        verification=VerificationResult(completed=True, reason="结果已验证"),
    )

    assert answer.startswith("## 搜索总结")
    assert "[TaskPilot](https://example.com)" in answer
