"""根据已发生事实调整剩余语义路径的 Dynamic Replanner。"""

import json
from typing import Protocol, Sequence

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.context import ContextSnapshot
from taskpilot.llm import create_chat_model, invoke_structured_with_retry
from taskpilot.requirements import build_requirement_contract, validate_step_references
from taskpilot.models import (
    PlanStep,
    PlanStepStatus,
    ReplanDecision,
    ReplanProposal,
    TaskSpec,
)


REPLANNER_SYSTEM_PROMPT = """你是 Task Replanner。你必须选择 REPLAN、REQUEST_HUMAN 或 ABORT；这只是建议，Runtime 会独立校验并控制状态路由。
REPLAN 只规划从当前真实状态出发接下来还需要完成的语义子目标（WHAT），不执行任务，也不宣布整个任务已经完成。
用户 Task Goal 与 Constraints 不允许修改；verified StepResult 是已完成事实，必须复用且不得让对应 completed Step 重新执行。failed/skipped 的旧路径可以替代，但不得把它们当作已完成事实。
failed Step 的 recent tool observations 也是当前环境的真实瞬时状态：若其中显示浏览器已打开或激活了目标页面，新计划必须从该页面继续，不得规划重新导航并覆盖已经到达的页面。
选择 REPLAN 时，只输出剩余工作的新 PlanStep，不复制历史步骤。每个新 Step 必须有非空、仅针对该步骤的 success_criteria，并保持 pending、retry_count=0。
只有输入包含明确的 non-retryable blocking_failure（登录、验证码或权限阻塞）时才可选择 REQUEST_HUMAN；此时 steps 必须为空，并明确填写 required_human_action 与可通过重新观察验证的 resume_condition。不得把人工协助写成 approve/approval，也不得声称已获高风险授权。
无法安全重规划且人工协助不合理时选择 ABORT；steps 必须为空，并给出明确原因。
失败类型 failure_category 可区分动作上限、全局预算、证据缺失等；重规划不能通过创建新 Step 擅自恢复已经耗尽的全局预算。
保留用户 required 条件；对 TaskSpec.optional_criteria 或计划自己附加的细节可以降级，但不得把用户明确的硬要求删除，也不得以部分结果宣布 COMPLETED。
新计划尽可能复用最近的页面证据，不应在相同来源上重复相同工具动作。
每个新 Step 必须设置 requirement_ids，并只引用 requirement_contract 中存在的 R/O 编号；用户必需条件 R 不得因重规划或降级而丢失，可选条件 O 可舍弃。
所有新 Step ID 必须严格大于输入中的 historical_max_step_id。depends_on 只能引用已 completed 的历史 Step ID，或本 proposal 中排在当前 Step 之前的新 Step ID；禁止依赖 failed、skipped、running 或 pending 的历史 Step。
不得选择或调用 Tool，不得输出 search(...)、browser_open(...)、click(...)、filesystem.write(...) 等调用、具体 Tool arguments、CSS selector 或浏览器定位器。
不得添加用户没有给出的硬约束，不得虚构环境事实、工具结果或尚未获得的数据。输出必须符合 ReplanProposal schema。"""


class TaskReplannerLike(Protocol):
    """真实 Replanner 与离线 Fake 共用的最小接口。"""

    def replan(self, context: ContextSnapshot) -> ReplanProposal:
        """根据已压缩但事实完整的上下文提出剩余语义步骤。"""

        ...


class TaskReplanner:
    """使用现有 OpenAI-compatible Structured Output 的 Replanner。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def replan(self, context: ContextSnapshot) -> ReplanProposal:
        if self._model is None:
            self._model = create_chat_model()
        return invoke_structured_with_retry(
            self._model,
            ReplanProposal,
            [
                ("system", REPLANNER_SYSTEM_PROMPT),
                (
                    "human",
                    "Current verified task facts:\n"
                    + json.dumps(context.content, ensure_ascii=False, sort_keys=True),
                ),
            ],
        )


def validate_replan_proposal(
    proposal: ReplanProposal,
    historical_plan: Sequence[PlanStep],
    task_spec: TaskSpec | None = None,
) -> None:
    """拒绝会破坏历史身份、依赖或执行初态的 Proposal。"""

    if proposal.decision is not ReplanDecision.REPLAN:
        if proposal.steps:
            raise ValueError(f"{proposal.decision.value} 不得包含 PlanStep")
        if proposal.decision is ReplanDecision.REQUEST_HUMAN:
            if not (proposal.required_human_action or "").strip():
                raise ValueError("REQUEST_HUMAN 缺少 required_human_action")
            if not (proposal.resume_condition or "").strip():
                raise ValueError("REQUEST_HUMAN 缺少 resume_condition")
        return
    if not proposal.steps:
        raise ValueError("REPLAN 的 ReplanProposal.steps 不能为空")
    if (
        proposal.required_human_action is not None
        or proposal.resume_condition is not None
    ):
        raise ValueError("REPLAN 不得包含 Human Assistance 字段")
    historical_max = max((step.id for step in historical_plan), default=0)
    ids = [step.id for step in proposal.steps]
    if len(ids) != len(set(ids)):
        raise ValueError("ReplanProposal 的 PlanStep.id 必须唯一")
    if any(step_id <= historical_max for step_id in ids):
        raise ValueError(
            f"Replan Step ID 必须大于 historical_max_step_id={historical_max}"
        )

    contract = build_requirement_contract(task_spec) if task_spec is not None else None
    historical_verified_refs = {
        ref for step in historical_plan
        if step.status is PlanStepStatus.COMPLETED
        for ref in step.requirement_ids
    }
    new_refs: set[str] = set()
    historical_by_id = {step.id: step for step in historical_plan}
    completed_historical = {
        step.id for step in historical_plan if step.status is PlanStepStatus.COMPLETED
    }
    earlier_new: set[int] = set()
    for step in proposal.steps:
        if step.status is not PlanStepStatus.PENDING:
            raise ValueError(f"Replan Step {step.id} 初始 status 必须为 pending")
        if step.retry_count != 0:
            raise ValueError(f"Replan Step {step.id} 初始 retry_count 必须为 0")
        if not step.success_criteria:
            raise ValueError(f"Replan Step {step.id} 缺少 success_criteria")
        if contract is not None and contract.all_ids:
            validate_step_references(step.requirement_ids, contract)
            new_refs.update(step.requirement_ids)
        if step.id in step.depends_on:
            raise ValueError(f"Replan Step {step.id} 不能依赖自身")
        for dependency_id in step.depends_on:
            if dependency_id in earlier_new or dependency_id in completed_historical:
                continue
            historical = historical_by_id.get(dependency_id)
            if historical is not None:
                raise ValueError(
                    f"Replan Step {step.id} 不得依赖状态为 "
                    f"{historical.status.value} 的历史 Step {dependency_id}"
                )
            if dependency_id in ids:
                raise ValueError(
                    f"Replan Step {step.id} 的新依赖 {dependency_id} 必须更早出现"
                )
            raise ValueError(
                f"Replan Step {step.id} 引用了不存在的依赖 {dependency_id}"
            )
        earlier_new.add(step.id)

    if contract is not None:
        uncovered = contract.required_ids - historical_verified_refs - new_refs
        if uncovered:
            raise ValueError(
                f"Replan 丢失用户必需条件: {sorted(uncovered)}"
            )

    # 上述“只能依赖更早新步骤”已经排除环；显式 DFS 保持规则可审计。
    new_by_id = {step.id: step for step in proposal.steps}
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(step_id: int) -> None:
        if step_id in visiting:
            raise ValueError("ReplanProposal 不允许循环依赖")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency_id in new_by_id[step_id].depends_on:
            if dependency_id in new_by_id:
                visit(dependency_id)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in ids:
        visit(step_id)
