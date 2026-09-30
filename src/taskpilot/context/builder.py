"""从 durable State 事实中选择当前模型调用所需的有限上下文。"""

import json
from typing import Any, Sequence

from pydantic import BaseModel, Field

from taskpilot.context.config import ContextConfig
from taskpilot.models import (
    PlanStep,
    ReplanRequest,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
)
from taskpilot.policy import ApprovalRecord, PolicyDecision, redact_preview


class ContextBudgetError(ValueError):
    """最高优先级核心上下文本身超过总字符预算。"""


class ContextSnapshot(BaseModel):
    """一次动态组装结果；只供模型输入和 Trace 使用。"""

    content: dict[str, Any]
    total_chars: int
    included_tool_calls: int = 0
    omitted_tool_calls: int = 0
    truncated_sections: list[str] = Field(default_factory=list)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _size(value: Any) -> int:
    return len(_json_text(value))


def _truncate(value: Any, limit: int) -> tuple[Any, bool]:
    """稳定截断序列化文本，并显式告知模型原内容不完整。"""

    text = _json_text(value)
    if len(text) <= limit:
        return value, False
    return {
        "truncated": True,
        "original_length": len(text),
        "preview": text[:limit],
    }, True


class ContextBuilder:
    """按固定优先级构建 Executor 与 Replanner 上下文。"""

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()

    def build_executor_context(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ContextSnapshot:
        """优先保留任务核心、反馈和 verified dependency results。"""

        core: dict[str, Any] = {
            "task_goal": task_spec.goal,
            "task_constraints": task_spec.constraints,
            "current_step": {
                "id": step.id,
                "revision": step.revision,
                "description": step.description,
                "success_criteria": step.success_criteria,
            },
        }
        if _size(core) > self.config.max_total_chars:
            raise ContextBudgetError(
                "任务目标、约束与当前步骤超过 max_total_chars，拒绝静默截断核心上下文"
            )

        content = dict(core)
        truncated: list[str] = []

        feedback: Any = None
        if verification_feedback is not None and verification_feedback.step_id == step.id:
            feedback, cut = _truncate(
                verification_feedback.model_dump(mode="json"),
                self.config.max_feedback_chars,
            )
            if cut:
                truncated.append("verification_feedback")
        self._add_high_priority(content, "verification_feedback", feedback, truncated)

        dependencies: list[Any] = []
        for result in step_results:
            if result.step_id not in step.depends_on:
                continue
            compact, cut = _truncate(
                result.model_dump(mode="json"),
                self.config.max_dependency_result_chars,
            )
            if cut:
                truncated.append(f"dependency_result:{result.step_id}")
            dependencies.append(compact)
        self._add_if_fits(content, "dependency_results", dependencies, truncated)

        current_calls = [record for record in tool_calls if record.step_id == step.id]
        selected = current_calls[-self.config.max_recent_tool_calls :]
        if self.config.max_recent_tool_calls == 0:
            selected = []
        observations: list[dict[str, Any]] = []
        for record in selected:
            value = record.result if record.error is None else record.error
            compact, cut = _truncate(value, self.config.max_single_observation_chars)
            if cut:
                truncated.append(f"tool_observation:{record.call_id}")
            observations.append(
                {
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "status": record.status.value,
                    "observation": compact,
                }
            )
        included = self._add_observations(content, observations, truncated)

        compact_results, cut = _truncate(
            task_results, self.config.max_task_results_chars
        )
        if cut:
            truncated.append("task_results")
        self._add_if_fits(content, "task_results", compact_results, truncated)

        policy = [
            item.model_dump(mode="json")
            for item in policy_decisions
            if item.step_id == step.id
        ]
        approvals = [
            item.model_dump(mode="json")
            for item in approval_records
            if item.step_id == step.id
        ]
        self._add_if_fits(content, "policy_feedback", policy, truncated)
        self._add_if_fits(content, "approval_history", approvals, truncated)
        return ContextSnapshot(
            content=content,
            total_chars=_size(content),
            included_tool_calls=included,
            omitted_tool_calls=len(current_calls) - included,
            truncated_sections=list(dict.fromkeys(truncated)),
        )

    def build_replan_context(
        self,
        *,
        task_spec: TaskSpec,
        plan: Sequence[PlanStep],
        step_results: Sequence[StepResult],
        tool_calls: Sequence[ToolCallRecord],
        request: ReplanRequest,
        plan_revision: int,
        replan_count: int,
    ) -> ContextSnapshot:
        """构造只包含真实事实与最近相关 Observation 的重规划上下文。"""

        failed = request.failed_step_id
        feedback, feedback_cut = _truncate(
            {
                "reason": request.reason,
                "missing_requirements": request.missing_requirements,
                "verification_feedback": request.verification_feedback,
            },
            self.config.max_feedback_chars,
        )
        core = {
            "task_goal": task_spec.goal,
            "task_constraints": task_spec.constraints,
            "completion_criteria": task_spec.completion_criteria,
            "replan_trigger": request.trigger.value,
            "current_plan_revision": plan_revision,
            "replan_count": replan_count,
            "historical_max_step_id": max((step.id for step in plan), default=0),
            "failed_step_id": failed,
            "failure_feedback": feedback,
        }
        if _size(core) > self.config.max_total_chars:
            raise ContextBudgetError("Replanner 核心任务事实超过 max_total_chars")
        content: dict[str, Any] = dict(core)
        truncated = ["failure_feedback"] if feedback_cut else []
        self._add_if_fits(
            content,
            "plan_status",
            [
                {
                    "id": step.id,
                    "revision": step.revision,
                    "description": step.description,
                    "status": step.status.value,
                    "depends_on": step.depends_on,
                    "success_criteria": step.success_criteria,
                }
                for step in plan
            ],
            truncated,
        )
        verified: list[Any] = []
        for result in step_results:
            compact, cut = _truncate(
                result.model_dump(mode="json"),
                self.config.max_dependency_result_chars,
            )
            if cut:
                truncated.append(f"verified_result:{result.step_id}")
            verified.append(compact)
        self._add_if_fits(content, "verified_step_results", verified, truncated)

        related = [record for record in tool_calls if record.step_id == failed]
        selected = related[-self.config.max_recent_tool_calls :]
        if self.config.max_recent_tool_calls == 0:
            selected = []
        observations = []
        for record in selected:
            compact, cut = _truncate(
                record.error if record.error is not None else record.result,
                self.config.max_single_observation_chars,
            )
            if cut:
                truncated.append(f"tool_observation:{record.call_id}")
            observations.append(
                {
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "arguments_preview": redact_preview(record.arguments),
                    "status": record.status.value,
                    "observation": compact,
                }
            )
        included = self._add_observations(content, observations, truncated)
        return ContextSnapshot(
            content=content,
            total_chars=_size(content),
            included_tool_calls=included,
            omitted_tool_calls=len(related) - included,
            truncated_sections=list(dict.fromkeys(truncated)),
        )

    def _add_if_fits(
        self,
        content: dict[str, Any],
        name: str,
        value: Any,
        truncated: list[str],
    ) -> bool:
        candidate = {**content, name: value}
        if _size(candidate) <= self.config.max_total_chars:
            content[name] = value
            return True
        truncated.append(name)
        return False

    def _add_high_priority(
        self,
        content: dict[str, Any],
        name: str,
        value: Any,
        truncated: list[str],
    ) -> None:
        """反馈放不下时缩短 preview，而不是静默丢弃。"""

        if self._add_if_fits(content, name, value, truncated):
            return
        text = _json_text(value)
        low, high = 0, len(text)
        fitted: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate = {
                "truncated": True,
                "original_length": len(text),
                "preview": text[:middle],
            }
            if _size({**content, name: candidate}) <= self.config.max_total_chars:
                fitted = candidate
                low = middle + 1
            else:
                high = middle - 1
        if fitted is None:
            raise ContextBudgetError(f"核心上下文后没有空间保留高优先级 {name}")
        content[name] = fitted

    def _add_observations(
        self,
        content: dict[str, Any],
        observations: Sequence[dict[str, Any]],
        truncated: list[str],
    ) -> int:
        included: list[dict[str, Any]] = []
        # 最新 Observation 优先；最终恢复为时间顺序，便于模型阅读。
        for observation in reversed(observations):
            candidate = list(reversed([observation, *included]))
            if _size({**content, "current_step_observations": candidate}) <= self.config.max_total_chars:
                included.insert(0, observation)
            else:
                truncated.append("current_step_observations")
        if _size({**content, "current_step_observations": included}) <= self.config.max_total_chars:
            content["current_step_observations"] = included
        else:
            truncated.append("current_step_observations")
            included = []
        return len(included)
