"""最终答案生成层测试。"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from taskpilot.finalizer import FinalAnswerGenerator, render_failure_answer
from taskpilot.models import (
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    TaskSpec,
    VerificationResult,
)


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


def test_failure_answer_prefers_verified_results_and_marks_partial_output(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "failure-secret")
    answer = render_failure_answer(
        task_spec=TaskSpec(goal="Collect two facts"),
        plan=[PlanStep(id=1, description="Collect facts", success_criteria=["two"])],
        current_step_id=1,
        step_results=[
            StepResult(step_id=1, summary="One fact verified", output={"fact": "A"})
        ],
        step_outcome=StepExecutionOutcome(
            step_id=2,
            claimed_complete=True,
            summary="Second fact claimed",
            output={"token": "failure-secret", "fact": "B"},
            evidence=["unverified evidence"],
        ),
        error="Verifier rejected second fact using failure-secret",
        verification=None,
    )

    assert "任务未完成" in answer
    assert "已完成并通过验证的结果" in answer
    assert "One fact verified" in answer
    assert "未验证的部分结果（仅供参考）" in answer
    assert "Second fact claimed" in answer
    assert "failure-secret" not in answer
    assert "[REDACTED]" in answer
