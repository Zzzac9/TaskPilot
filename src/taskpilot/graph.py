"""TaskPilot durable action-level LangGraph 工作流。"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from typing import Any, Literal, Sequence, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.browser import BrowserLocatorError, is_vision_fallback_eligible
from taskpilot.config import redact_sensitive_values
from taskpilot.context import ContextBuilder
from taskpilot.executor import ExecutorRunResult, TaskExecutor, TaskExecutorLike
from taskpilot.finalizer import FinalAnswerGeneratorLike, VerifiedResultsRenderer
from taskpilot.models import (
    ExecutorActionType,
    PlanStep,
    PlanStepStatus,
    ReplanRecord,
    ReplanRequest,
    ReplanTrigger,
    StepExecutionOutcome,
    StepResult,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.persistence.journal import ActionJournalLike, InMemoryActionJournal
from taskpilot.persistence.models import (
    ActionJournalRecord,
    ExecutionFaultInjector,
    ExecutionFaultPoint,
    JournalStatus,
    NoOpExecutionFaultInjector,
    RecoveryChoice,
    RecoveryIssue,
    RecoveryResponse,
)
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.replanner import (
    TaskReplanner,
    TaskReplannerLike,
    validate_replan_proposal,
)
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    ApprovalResponse,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
    redact_preview,
)
from taskpilot.state import TaskState, TaskStateUpdate
from taskpilot.tools.registry import ToolRegistry
from taskpilot.trace import (
    NoOpTraceRecorder,
    TraceEventType,
    TraceRecorderLike,
    make_trace_event,
)
from taskpilot.verifier import (
    TaskVerifier,
    TaskVerifierLike,
    incomplete_outcome_rejection,
    validate_step_verification,
    validate_task_verification,
)


def initialize_task(state: TaskState) -> TaskStateUpdate:
    """将新创建的任务推进到运行状态。"""

    # 只允许 created → running，避免意外覆盖完成、失败等其他状态。
    if state["status"] == TaskStatus.CREATED:
        return {"status": TaskStatus.RUNNING}
    return {}


def analyze_task(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
) -> TaskStateUpdate:
    """调用 Analyzer，并将结构化结果写回任务状态。"""

    try:
        task_spec = analyzer.analyze(state["user_input"])
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Analyzer 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }
    return {"task_spec": task_spec, "error": None}


def plan_task(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
) -> TaskStateUpdate:
    """调用 Planner，并选出第一条依赖已满足的待执行步骤。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Task Planner 无法运行: task_spec 缺失",
        }

    try:
        task_plan = planner.plan(task_spec)
        initial_steps = [
            step.model_copy(update={"revision": 0}) for step in task_plan.steps
        ]
        current_step_id = find_next_executable_step(initial_steps)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "plan": [],
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Planner 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if current_step_id is None:
        return {
            "plan": initial_steps,
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": "Task Planner 返回的计划没有可执行步骤",
        }
    return {
        "plan": initial_steps,
        "current_step_id": current_step_id,
        "plan_revision": 0,
        "replan_count": 0,
        "replan_history": [],
        "pending_replan": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_step(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """只执行 current_step_id 对应步骤，并保存 Executor 声明。"""

    current_step_id = state["current_step_id"]
    if current_step_id is None:
        return _failed_update(state["error"] or "Executor 无法运行: current_step_id 缺失")
    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update(state["error"] or "Executor 无法运行: task_spec 缺失")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            "Executor 无法定位唯一当前步骤: "
            f"current_step_id={current_step_id}"
        )

    current_step = state["plan"][step_index]
    if current_step.status not in {
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    }:
        return _failed_update(
            f"Executor 不能执行状态为 {current_step.status.value} 的步骤 "
            f"{current_step.id}"
        )

    running_step = current_step.model_copy(
        update={"status": PlanStepStatus.RUNNING}
    )
    updated_plan = list(state["plan"])
    updated_plan[step_index] = running_step
    prior_step_tool_calls = [
        record
        for record in state["tool_calls"]
        if record.step_id == current_step_id
    ]

    try:
        execution = await executor.execute(
            task_spec=task_spec,
            step=running_step,
            task_results=state["task_results"],
            tool_calls=prior_step_tool_calls,
            step_results=state["step_results"],
            verification_feedback=state.get("step_verification"),
            policy_decisions=state.get("policy_decisions", []),
            approval_records=state.get("approval_records", []),
        )
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        error = f"Task Executor 调用失败: {type(exc).__name__}: {error_detail}"
        return {
            "plan": updated_plan,
            "step_outcome": StepExecutionOutcome(
                step_id=current_step_id,
                claimed_complete=False,
                action_count=0,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }

    return _execution_state_update(
        state=state,
        updated_plan=updated_plan,
        execution=execution,
    )


def prepare_approval(state: TaskState) -> TaskStateUpdate:
    """在 interrupt 前提交 WAITING_APPROVAL，且不执行目标 Tool。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("prepare_approval 需要 pending_action")
    if pending.step_id != state["current_step_id"]:
        return _failed_update("pending_action.step_id 与当前步骤不一致")
    return {"status": TaskStatus.WAITING_APPROVAL, "error": None}


def human_approval(state: TaskState) -> TaskStateUpdate:
    """通过 LangGraph interrupt 获取只含 approve/reject 的 resume value。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("human_approval 需要 pending_action")
    decision = pending.policy_decision
    payload = {
        "type": "tool_approval",
        "call_id": pending.call_id,
        "step_id": pending.step_id,
        "tool_name": pending.tool_name,
        "risk_level": decision.risk_level.value,
        "reason": decision.reason,
        "arguments_preview": decision.preview,
        "tool_source": decision.tool_source,
    }
    # interrupt 之前没有外部副作用；resume 时该 node 会从头重放到这里。
    resume_value = interrupt(payload)
    try:
        response = ApprovalResponse.model_validate(resume_value)
    except ValidationError as exc:
        return {
            "status": TaskStatus.FAILED,
            "error": f"Human approval resume value 无效，Tool 未执行: {exc}",
        }
    record = ApprovalRecord(
        call_id=pending.call_id,
        step_id=pending.step_id,
        tool_name=pending.tool_name,
        decision=response.decision,
        reason=response.reason,
    )
    return {
        "approval_records": state.get("approval_records", []) + [record],
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_approved_action(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """不再询问模型，执行 PendingToolAction 中冻结的 exact action 一次。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("execute_approved_action 需要 pending_action")
    result = await executor.execute_approved_action(
        pending_action=pending,
        approval_records=state.get("approval_records", []),
    )
    if result.fatal_error or result.tool_call is None:
        return {
            "status": TaskStatus.FAILED,
            "error": result.error or "获批 Tool action 未返回执行记录",
        }
    return {
        "tool_calls": state["tool_calls"] + [result.tool_call],
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def handle_rejection(state: TaskState) -> TaskStateUpdate:
    """清除被拒动作，让 Executor 从审批历史中看到拒绝并选择替代方案。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("handle_rejection 需要 pending_action")
    matches = [
        record
        for record in state.get("approval_records", [])
        if record.call_id == pending.call_id
        and record.step_id == pending.step_id
        and record.tool_name == pending.tool_name
    ]
    if len(matches) != 1 or matches[0].decision is not ApprovalChoice.REJECT:
        return _failed_update("被拒动作缺少唯一 REJECT approval record")
    return {
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_step(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    max_replans: int = 0,
) -> TaskStateUpdate:
    """验证当前 Step 声明，并记录接受或拒绝结论。"""

    current_step_id = state["current_step_id"]
    task_spec = state["task_spec"]
    outcome = state["step_outcome"]
    if current_step_id is None or task_spec is None or outcome is None:
        return _failed_update("Step Verifier 无法运行: 当前步骤上下文不完整")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"Step Verifier 无法定位唯一当前步骤: {current_step_id}"
        )
    step = state["plan"][step_index]
    if outcome.step_id != step.id:
        return _failed_update(
            "StepExecutionOutcome.step_id 与当前步骤不一致: "
            f"expected={step.id}, actual={outcome.step_id}"
        )

    current_calls = [
        record for record in state["tool_calls"] if record.step_id == step.id
    ]
    dependency_results = [
        result
        for result in state["step_results"]
        if result.step_id in step.depends_on
    ]
    try:
        if outcome.claimed_complete:
            result = verifier.verify_step(
                task_spec=task_spec,
                step=step,
                outcome=outcome,
                tool_calls=current_calls,
                dependency_results=dependency_results,
            )
        else:
            # 尚未 claim complete 时确定性拒绝，不浪费一次模型调用。
            result = incomplete_outcome_rejection(step, outcome)
        validate_step_verification(step, result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        failed_step = step.model_copy(update={"status": PlanStepStatus.FAILED})
        return {
            "plan": _replace_step(state["plan"], step_index, failed_step),
            "status": TaskStatus.FAILED,
            "error": (
                f"Step Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.verified:
        return {
            "step_verification": result,
            "status": TaskStatus.RUNNING,
            "error": None,
        }

    retry_count = step.retry_count + 1
    reached_limit = retry_count >= max_step_attempts
    rejected_step = step.model_copy(
        update={
            "retry_count": retry_count,
            "status": (
                PlanStepStatus.FAILED
                if reached_limit
                else PlanStepStatus.RUNNING
            ),
        }
    )
    # 动作上限是 Executor 的硬安全边界。若把它交给 Replanner 创建一个
    # “新”步骤，就能重新获得一整份额度，既绕过边界也容易重复页面操作。
    action_limit_reached = bool(
        not outcome.claimed_complete
        and outcome.error
        and outcome.error.startswith("Executor action limit reached:")
    )
    can_replan = (
        reached_limit
        and not action_limit_reached
        and state.get("replan_count", 0) < max_replans
    )
    update: TaskStateUpdate = {
        "plan": _replace_step(state["plan"], step_index, rejected_step),
        "step_verification": result,
        "status": (
            TaskStatus.RUNNING if can_replan or not reached_limit else TaskStatus.FAILED
        ),
        "error": None,
    }
    if reached_limit:
        reason = (
            f"Step {step.id} 连续被 Verifier 拒绝，已达到 "
            f"max_step_attempts={max_step_attempts}: "
            f"{result.feedback or '未提供反馈'}"
        )
        if can_replan:
            update["pending_replan"] = ReplanRequest(
                trigger=ReplanTrigger.STEP_ATTEMPTS_EXHAUSTED,
                failed_step_id=step.id,
                reason=reason,
                verification_feedback=result.feedback,
            )
            update["error"] = None
        else:
            if action_limit_reached:
                update["error"] = f"{reason}; hard action limit, replan disabled"
            else:
                update["error"] = (
                    reason
                    if max_replans == 0
                    else f"{reason}; replan budget exhausted"
                )
    return update


def advance_step(state: TaskState) -> TaskStateUpdate:
    """接受已验证 Claim，沉淀 StepResult 并选择下一可执行步骤。"""

    current_step_id = state["current_step_id"]
    outcome = state["step_outcome"]
    verification = state["step_verification"]
    if (
        current_step_id is None
        or outcome is None
        or verification is None
        or not verification.verified
    ):
        return _failed_update("advance_step 需要已通过验证的当前步骤")
    if outcome.step_id != current_step_id or verification.step_id != current_step_id:
        return _failed_update("advance_step 的步骤 ID 不一致")
    if not outcome.summary:
        return _failed_update("已验证的 StepExecutionOutcome 缺少 summary")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"advance_step 无法定位唯一当前步骤: {current_step_id}"
        )
    completed_step = state["plan"][step_index].model_copy(
        update={"status": PlanStepStatus.COMPLETED}
    )
    updated_plan = _replace_step(state["plan"], step_index, completed_step)
    result = StepResult(
        step_id=current_step_id,
        summary=outcome.summary,
        output=outcome.output,
        evidence=outcome.evidence,
    )
    updated_results = _upsert_step_result(
        state["step_results"],
        result,
        updated_plan,
    )

    try:
        next_step_id = find_next_executable_step(updated_plan)
    except ValueError as exc:
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": str(exc),
        }

    active_revision = state.get("plan_revision", 0)
    if next_step_id is None and not current_revision_finished(
        updated_plan, active_revision
    ):
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": "Plan 状态异常: 没有 pending 步骤，但并非所有步骤已完成",
        }

    return {
        "plan": updated_plan,
        "step_results": updated_results,
        "current_step_id": next_step_id,
        # 新步骤不能继承上一条步骤的 Claim 或反馈。
        "step_outcome": None,
        "step_verification": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_task(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_replans: int = 0,
) -> TaskStateUpdate:
    """所有步骤完成后，独立验证 Task completion criteria。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update("Task Verifier 无法运行: task_spec 缺失")
    try:
        result = verifier.verify_task(
            task_spec=task_spec,
            step_results=state["step_results"],
            task_results=state["task_results"],
        )
        validate_task_verification(result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.completed:
        return {
            "verification": result,
            "current_step_id": None,
            "status": TaskStatus.COMPLETED,
            "error": None,
        }
    next_hint = f"; next_action={result.next_action}" if result.next_action else ""
    reason = f"Task Verifier 拒绝任务完成: {result.reason}{next_hint}"
    if state.get("replan_count", 0) < max_replans:
        return {
            "verification": result,
            "current_step_id": None,
            "pending_replan": ReplanRequest(
                trigger=ReplanTrigger.TASK_VERIFICATION_FAILED,
                reason=result.reason,
                missing_requirements=result.missing_requirements,
            ),
            "status": TaskStatus.RUNNING,
            "error": None,
        }
    return {
        "verification": result,
        "current_step_id": None,
        "status": TaskStatus.FAILED,
        "error": (
            reason
            if max_replans == 0
            else f"{reason}; replan budget exhausted"
        ),
    }


def current_revision_finished(plan: Sequence[PlanStep], revision: int) -> bool:
    """当前 revision 没有 pending/running 即可进入 Task Verifier。"""

    active = [step for step in plan if step.revision == revision]
    return bool(active) and not any(
        step.status in {PlanStepStatus.PENDING, PlanStepStatus.RUNNING}
        for step in active
    )


async def _trace(
    recorder: TraceRecorderLike,
    state: TaskState,
    event_type: TraceEventType,
    *,
    step_id: int | None = None,
    call_id: str | None = None,
    status: str | None = None,
    latency_ms: float | None = None,
    payload: dict[str, Any] | None = None,
    plan_revision_override: int | None = None,
) -> None:
    """把 state 中的稳定 identity 附到独立 Trace 事件。"""

    await recorder.record(
        make_trace_event(
            task_id=state["task_id"],
            event_type=event_type,
            step_id=step_id,
            call_id=call_id,
            plan_revision=(
                plan_revision_override
                if plan_revision_override is not None
                else state.get("plan_revision", 0)
            ),
            status=status,
            latency_ms=latency_ms,
            payload=payload,
        )
    )


async def initialize_task_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = initialize_task(state)
    await _trace(
        trace_recorder,
        state,
        TraceEventType.TASK_STARTED,
        status=TaskStatus.RUNNING.value,
        payload={"user_input_length": len(state["user_input"])},
    )
    return update


async def analyze_task_traced(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = analyze_task(state, analyzer=analyzer)
    latency = (perf_counter() - started) * 1000
    failed = update.get("status") is TaskStatus.FAILED
    await _trace(
        trace_recorder,
        state,
        TraceEventType.TASK_ANALYZED,
        status="failed" if failed else "success",
        latency_ms=latency,
        payload={"error": update.get("error")} if failed else {},
    )
    if failed:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


async def plan_task_traced(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = plan_task(state, planner=planner)
    latency = (perf_counter() - started) * 1000
    failed = update.get("status") is TaskStatus.FAILED
    await _trace(
        trace_recorder,
        state,
        TraceEventType.PLAN_CREATED,
        status="failed" if failed else "success",
        latency_ms=latency,
        payload={
            "step_ids": [step.id for step in update.get("plan", [])],
            **({"error": update.get("error")} if failed else {}),
        },
    )
    if failed:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


async def begin_step_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = begin_step(state)
    if update.get("status") is not TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.STEP_STARTED,
            step_id=state.get("current_step_id"),
            status=PlanStepStatus.RUNNING.value,
        )
    return update


async def replan_task(
    state: TaskState,
    *,
    replanner: TaskReplannerLike,
    context_builder: ContextBuilder,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """验证并接受一版只覆盖剩余工作的全新 revision。"""

    request = state.get("pending_replan")
    task_spec = state.get("task_spec")
    if request is None or task_spec is None:
        return _failed_update("replan_task 缺少 pending_replan 或 TaskSpec")
    if state.get("replan_count", 0) >= max_replans:
        error = "replan budget exhausted"
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": error, "trigger": request.trigger.value},
        )
        return _failed_update(error)

    started = perf_counter()
    try:
        snapshot = context_builder.build_replan_context(
            task_spec=task_spec,
            plan=state["plan"],
            step_results=state["step_results"],
            tool_calls=state["tool_calls"],
            request=request,
            plan_revision=state.get("plan_revision", 0),
            replan_count=state.get("replan_count", 0),
        )
        proposal = await asyncio.to_thread(replanner.replan, snapshot)
        validate_replan_proposal(proposal, state["plan"])
        next_revision = state.get("plan_revision", 0) + 1
        new_steps = [
            step.model_copy(
                update={
                    "revision": next_revision,
                    "status": PlanStepStatus.PENDING,
                    "retry_count": 0,
                }
            )
            for step in proposal.steps
        ]
        remaining_ids = [
            step.id
            for step in state["plan"]
            if step.status in {PlanStepStatus.PENDING, PlanStepStatus.RUNNING}
        ]
        replaced_ids = list(
            dict.fromkeys(
                ([request.failed_step_id] if request.failed_step_id is not None else [])
                + remaining_ids
            )
        )
        historical = [
            step.model_copy(update={"status": PlanStepStatus.SKIPPED})
            if step.id in remaining_ids
            else step
            for step in state["plan"]
        ]
        updated_plan = historical + new_steps
        next_step_id = find_next_executable_step(updated_plan)
        if next_step_id is None:
            raise ValueError("已接受的 ReplanProposal 没有可执行新步骤")
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        error = f"Task Replanner 调用或校验失败: {type(exc).__name__}: {detail}"
        await _trace(
            trace_recorder,
            state,
            TraceEventType.PLAN_REPLANNED,
            status="failed",
            latency_ms=(perf_counter() - started) * 1000,
            payload={"trigger": request.trigger.value, "error": error},
        )
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": error},
        )
        return _failed_update(error)

    record = ReplanRecord(
        revision=next_revision,
        trigger=request.trigger,
        reason=proposal.reason,
        replaced_step_ids=replaced_ids,
        new_step_ids=[step.id for step in new_steps],
    )
    await _trace(
        trace_recorder,
        state,
        TraceEventType.PLAN_REPLANNED,
        plan_revision_override=next_revision,
        status="success",
        latency_ms=(perf_counter() - started) * 1000,
        payload={
            "trigger": request.trigger.value,
            "failed_step_id": request.failed_step_id,
            "reason": proposal.reason,
            "replaced_step_ids": replaced_ids,
            "new_revision": next_revision,
            "new_step_ids": record.new_step_ids,
            "context_chars": snapshot.total_chars,
        },
    )
    return {
        "plan": updated_plan,
        "plan_revision": next_revision,
        "replan_count": state.get("replan_count", 0) + 1,
        "replan_history": state.get("replan_history", []) + [record],
        "current_step_id": next_step_id,
        "current_action_index": 0,
        "step_outcome": None,
        "step_verification": None,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "pending_replan": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def find_next_executable_step(plan: Sequence[PlanStep]) -> int | None:
    """按计划顺序选择第一条依赖均已完成的 pending 步骤。"""

    completed_ids = {
        step.id for step in plan if step.status == PlanStepStatus.COMPLETED
    }
    pending_steps = [
        step for step in plan if step.status == PlanStepStatus.PENDING
    ]
    for step in pending_steps:
        if all(dependency_id in completed_ids for dependency_id in step.depends_on):
            return step.id
    if pending_steps:
        blocked = {
            step.id: [
                dependency_id
                for dependency_id in step.depends_on
                if dependency_id not in completed_ids
            ]
            for step in pending_steps
        }
        raise ValueError(
            "Plan 依赖阻塞: 存在 pending 步骤但没有可执行步骤; "
            f"unmet_dependencies={blocked}"
        )
    return None


def _execution_state_update(
    *,
    state: TaskState,
    updated_plan: list[PlanStep],
    execution: ExecutorRunResult,
) -> TaskStateUpdate:
    """把 Executor 返回值合并到 LangGraph 状态。"""

    update: TaskStateUpdate = {
        "plan": updated_plan,
        "tool_calls": state["tool_calls"] + execution.tool_calls,
        "policy_decisions": (
            state.get("policy_decisions", []) + execution.policy_decisions
        ),
        "step_outcome": execution.outcome,
        "pending_action": execution.pending_action,
        "status": (
            TaskStatus.FAILED if execution.fatal_error else TaskStatus.RUNNING
        ),
        "error": (
            execution.outcome.error
            if execution.fatal_error and execution.outcome is not None
            else None
        ),
    }
    if execution.fatal_error and execution.outcome is None:
        update["error"] = "Executor fatal failure 未提供 StepExecutionOutcome"
    if not execution.fatal_error and execution.outcome is None:
        if execution.pending_action is None:
            update["status"] = TaskStatus.FAILED
            update["error"] = "Executor 未返回 outcome 或 pending_action"
    return update


def _find_unique_step_index(plan: Sequence[PlanStep], step_id: int) -> int | None:
    matches = [index for index, step in enumerate(plan) if step.id == step_id]
    return matches[0] if len(matches) == 1 else None


def _replace_step(
    plan: Sequence[PlanStep],
    index: int,
    step: PlanStep,
) -> list[PlanStep]:
    updated = list(plan)
    updated[index] = step
    return updated


def _upsert_step_result(
    step_results: Sequence[StepResult],
    result: StepResult,
    plan: Sequence[PlanStep],
) -> list[StepResult]:
    """按 step_id 替换正式结果，并保持计划顺序且不产生重复。"""

    by_step_id = {
        item.step_id: item for item in step_results if item.step_id != result.step_id
    }
    by_step_id[result.step_id] = result
    plan_order = {step.id: index for index, step in enumerate(plan)}
    return sorted(
        by_step_id.values(),
        key=lambda item: plan_order.get(item.step_id, len(plan)),
    )


def _failed_update(error: str) -> TaskStateUpdate:
    return {"status": TaskStatus.FAILED, "error": error}


def _route_after_plan(state: TaskState) -> Literal["execute_step", "end"]:
    return "end" if state["status"] == TaskStatus.FAILED else "execute_step"


def _route_after_execute(
    state: TaskState,
) -> Literal["prepare_approval", "verify_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    return "verify_step"


def _route_after_human_approval(
    state: TaskState,
) -> Literal["execute_approved_action", "handle_rejection", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    records = state.get("approval_records", [])
    if not records:
        return "end"
    return (
        "execute_approved_action"
        if records[-1].decision is ApprovalChoice.APPROVE
        else "handle_rejection"
    )


def _route_after_step_verification(
    state: TaskState,
) -> Literal["advance_step", "execute_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    verification = state["step_verification"]
    if verification is not None and verification.verified:
        return "advance_step"
    return "execute_step"


def _route_after_advance(
    state: TaskState,
) -> Literal["execute_step", "verify_task", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state["current_step_id"] is None:
        return "verify_task"
    return "execute_step"


def _build_legacy_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """构建带 Policy/HITL、两层验证和有限重试的状态图。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = (
        executor
        if executor is not None
        else TaskExecutor(registry=ToolRegistry())
    )
    active_verifier = verifier if verifier is not None else TaskVerifier()

    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node(
        "analyze_task",
        partial(analyze_task, analyzer=active_analyzer),
    )
    builder.add_node("plan_task", partial(plan_task, planner=active_planner))
    builder.add_node(
        "execute_step",
        partial(execute_step, executor=active_executor),
    )
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("human_approval", human_approval)
    builder.add_node(
        "execute_approved_action",
        partial(execute_approved_action, executor=active_executor),
    )
    builder.add_node("handle_rejection", handle_rejection)
    builder.add_node(
        "verify_step",
        partial(
            verify_step,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
        ),
    )
    builder.add_node("advance_step", advance_step)
    builder.add_node(
        "verify_task",
        partial(verify_task, verifier=active_verifier),
    )

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task",
        _route_after_plan,
        {"execute_step": "execute_step", "end": END},
    )
    builder.add_conditional_edges(
        "execute_step",
        _route_after_execute,
        {
            "prepare_approval": "prepare_approval",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_approved_action",
            "handle_rejection": "handle_rejection",
            "end": END,
        },
    )
    builder.add_edge("execute_approved_action", "execute_step")
    builder.add_edge("handle_rejection", "execute_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_step_verification,
        {
            "advance_step": "advance_step",
            "execute_step": "execute_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "advance_step",
        _route_after_advance,
        {
            "execute_step": "execute_step",
            "verify_task": "verify_task",
            "end": END,
        },
    )
    builder.add_edge("verify_task", END)
    return builder.compile(checkpointer=checkpointer)


def begin_step(state: TaskState) -> TaskStateUpdate:
    """进入新 Step，并为本次 verification attempt 初始化 action counter。"""

    step_id = state.get("current_step_id")
    if step_id is None:
        return _failed_update("begin_step 需要 current_step_id")
    index = _find_unique_step_index(state["plan"], step_id)
    if index is None:
        return _failed_update(f"begin_step 无法定位唯一当前步骤: {step_id}")
    step = state["plan"][index]
    running = step.model_copy(update={"status": PlanStepStatus.RUNNING})
    return {
        "plan": _replace_step(state["plan"], index, running),
        "current_action_index": 0,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "step_outcome": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def decide_action(
    state: TaskState,
    *,
    executor: TaskExecutor,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """最多调用模型一次，并把 frozen ExecutorAction 写回 state。"""

    step_id = state.get("current_step_id")
    task_spec = state.get("task_spec")
    if step_id is None or task_spec is None:
        return _failed_update("decide_action 缺少当前 Step 或 TaskSpec")
    index = _find_unique_step_index(state["plan"], step_id)
    if index is None:
        return _failed_update(f"decide_action 无法定位唯一当前步骤: {step_id}")
    current_index = state.get("current_action_index", 0)
    if current_index >= executor.max_actions_per_step:
        return {
            "step_outcome": StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=current_index,
                error=f"Executor action limit reached: {executor.max_actions_per_step}",
            ),
            "pending_executor_action": None,
            "status": TaskStatus.RUNNING,
            "error": None,
        }
    action_index = current_index + 1
    started = perf_counter()
    result = await executor.decide_action(
        task_spec=task_spec,
        step=state["plan"][index],
        action_index=action_index,
        task_results=state["task_results"],
        tool_calls=state["tool_calls"],
        step_results=state["step_results"],
        verification_feedback=state.get("step_verification"),
        policy_decisions=state.get("policy_decisions", []),
        approval_records=state.get("approval_records", []),
    )
    latency = (perf_counter() - started) * 1000
    if result.context_snapshot is not None:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.CONTEXT_BUILT,
            step_id=step_id,
            status="success",
            payload={
                "total_chars": result.context_snapshot.total_chars,
                "included_tool_calls": result.context_snapshot.included_tool_calls,
                "omitted_tool_calls": result.context_snapshot.omitted_tool_calls,
                "truncated_sections": result.context_snapshot.truncated_sections,
            },
        )
    await _trace(
        trace_recorder,
        state,
        TraceEventType.LLM_ACTION_DECIDED,
        step_id=step_id,
        call_id=result.action.call_id if result.action is not None else None,
        status="failed" if result.fatal_error else "success",
        latency_ms=latency,
        payload={
            "model_invoked": result.context_snapshot is not None,
            **(
                {
                    "action_type": result.action.action_type.value,
                    "tool_name": result.action.tool_name,
                }
                if result.action is not None
                else {"error": result.error}
            ),
            **(
                {"usage_metadata": result.usage_metadata}
                if result.usage_metadata is not None
                else {}
            ),
        },
    )
    if result.fatal_error:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            step_id=step_id,
            status=TaskStatus.FAILED.value,
            payload={"error": result.error},
        )
    if result.fatal_error or result.action is None:
        error = result.error or "Executor decision 未返回 action"
        return {
            "current_action_index": action_index,
            "step_outcome": StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_index,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }
    action = result.action
    if action.step_id != step_id or action.action_index != action_index:
        return _failed_update("ExecutorAction identity 与 Graph 当前边界不一致")
    return {
        "current_action_index": action_index,
        "pending_executor_action": action,
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def build_step_outcome(state: TaskState) -> TaskStateUpdate:
    """把 FINISH_STEP frozen arguments 转换为未验证的完成声明。"""

    action = state.get("pending_executor_action")
    if action is None or action.action_type is not ExecutorActionType.FINISH_STEP:
        return _failed_update("build_step_outcome 需要 FINISH_STEP action")
    arguments = action.arguments
    return {
        "step_outcome": StepExecutionOutcome(
            step_id=action.step_id,
            claimed_complete=True,
            summary=arguments["summary"],
            evidence=arguments["evidence"],
            output=arguments["output"],
            action_count=action.action_index,
        ),
        "pending_executor_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def assess_policy(
    state: TaskState,
    *,
    executor: TaskExecutor,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """评估 frozen action；审批之前绝不执行目标 Tool。"""

    action = state.get("pending_executor_action")
    if action is None:
        return _failed_update("assess_policy 需要 pending_executor_action")
    try:
        decision = await executor.assess_action(action)
    except BrowserLocatorError as exc:
        if not is_vision_fallback_eligible(exc):
            detail = redact_sensitive_values(str(exc))
            return _failed_update(
                "Tool Risk Policy 评估失败，动作未执行: "
                f"{type(exc).__name__}: {detail}"
            )
        detail = redact_sensitive_values(str(exc))
        preview = redact_preview(action.arguments)
        decision = PolicyDecision(
            call_id=action.call_id,
            step_id=action.step_id,
            tool_name=action.tool_name or "",
            risk_level=RiskLevel.HIGH,
            outcome=PolicyOutcome.DENY,
            reason=(
                "Browser locator preflight 无法可靠定位目标；本次动作未执行，"
                f"请改用新 locator、键盘操作或直接导航 URL: {detail}"
            ),
            tool_source="browser",
            preview=preview if isinstance(preview, dict) else {"value": preview},
        )
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        return _failed_update(
            f"Tool Risk Policy 评估失败，动作未执行: {type(exc).__name__}: {detail}"
        )
    if decision is None:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.POLICY_DECIDED,
            step_id=action.step_id,
            call_id=action.call_id,
            status="not_configured",
        )
        return {"status": TaskStatus.RUNNING, "error": None}
    await _trace(
        trace_recorder,
        state,
        TraceEventType.POLICY_DECIDED,
        step_id=action.step_id,
        call_id=action.call_id,
        status=decision.outcome.value,
        payload={"reason": decision.reason, "risk_level": decision.risk_level.value},
    )
    update: TaskStateUpdate = {
        "policy_decisions": state.get("policy_decisions", []) + [decision],
        "status": TaskStatus.RUNNING,
        "error": None,
    }
    if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
        update["pending_action"] = PendingToolAction(
            call_id=action.call_id,
            step_id=action.step_id,
            tool_name=action.tool_name or "",
            arguments=dict(action.arguments),
            policy_decision=decision,
        )
    return update


def policy_feedback(state: TaskState) -> TaskStateUpdate:
    """保留 DENY decision，清除动作后让模型选择替代方案。"""

    return {
        "pending_executor_action": None,
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def rejection_feedback(state: TaskState) -> TaskStateUpdate:
    """校验 REJECT record 后清除两种 pending action。"""

    update = handle_rejection(state)
    if update.get("status") is not TaskStatus.FAILED:
        update["pending_executor_action"] = None
    return update


async def prepare_approval_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = prepare_approval(state)
    pending = state.get("pending_action")
    if pending is not None and update.get("status") is not TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.APPROVAL_REQUESTED,
            step_id=pending.step_id,
            call_id=pending.call_id,
            status=TaskStatus.WAITING_APPROVAL.value,
            payload={"tool_name": pending.tool_name},
        )
    return update


async def human_approval_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = human_approval(state)
    pending = state.get("pending_action")
    records = update.get("approval_records", [])
    if pending is not None and records:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.APPROVAL_RESOLVED,
            step_id=pending.step_id,
            call_id=pending.call_id,
            status=records[-1].decision.value,
            payload={"reason": records[-1].reason},
        )
    return update


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    """按 Phase 9 约定计算稳定 JSON SHA-256。"""

    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def action_key_for(state: TaskState) -> str:
    """生成包含 task/step/retry/action index 的稳定 journal key。"""

    action = state.get("pending_executor_action")
    if action is None:
        raise ValueError("action_key_for 需要 pending_executor_action")
    index = _find_unique_step_index(state["plan"], action.step_id)
    if index is None:
        raise ValueError("action_key_for 无法定位唯一 PlanStep")
    retry_count = state["plan"][index].retry_count
    return f"{state['task_id']}:{action.step_id}:{retry_count}:{action.action_index}"


def _journal_identity_error(
    record: ActionJournalRecord,
    *,
    state: TaskState,
    arguments_hash: str,
    replay_safe: bool,
) -> str | None:
    action = state["pending_executor_action"]
    assert action is not None
    index = _find_unique_step_index(state["plan"], action.step_id)
    assert index is not None
    expected = (
        state["task_id"], action.step_id, state["plan"][index].retry_count,
        action.action_index, action.call_id, action.tool_name, arguments_hash,
        replay_safe,
    )
    actual = (
        record.task_id, record.step_id, record.retry_count, record.action_index,
        record.call_id, record.tool_name, record.arguments_hash, record.replay_safe,
    )
    return None if actual == expected else "Action Journal identity/hash 不一致，拒绝恢复"


def _materialize_tool_call(
    state: TaskState,
    record: ActionJournalRecord,
) -> TaskStateUpdate:
    action = state["pending_executor_action"]
    assert action is not None and action.tool_name is not None
    tool_call = ToolCallRecord(
        call_id=action.call_id,
        step_id=action.step_id,
        tool_name=action.tool_name,
        arguments=action.arguments,
        status=(
            ToolCallStatus.SUCCESS
            if record.status is JournalStatus.SUCCEEDED
            else ToolCallStatus.FAILED
        ),
        result=record.result,
        error=record.error,
    )
    existing = [
        item for item in state["tool_calls"]
        if item.step_id == tool_call.step_id and item.call_id == tool_call.call_id
    ]
    if existing:
        if len(existing) != 1 or existing[0].model_dump(mode="json") != tool_call.model_dump(mode="json"):
            return _failed_update("ToolCallRecord identity 内容冲突，拒绝静默覆盖")
        updated_calls = state["tool_calls"]
    else:
        updated_calls = state["tool_calls"] + [tool_call]
    return {
        "tool_calls": updated_calls,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_tool_action(
    state: TaskState,
    *,
    executor: TaskExecutor,
    journal: ActionJournalLike,
    fault_injector: ExecutionFaultInjector,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    """按 STARTED → one invoke → terminal → node return 的顺序执行一次。"""

    action = state.get("pending_executor_action")
    if action is None or action.action_type is not ExecutorActionType.TOOL:
        return _failed_update("execute_tool_action 需要 frozen Tool action")
    try:
        executor.validate_frozen_action(action)
        replay_safe = executor.is_replay_safe(action)
        action_key = action_key_for(state)
        arguments_hash = canonical_arguments_hash(action.arguments)
    except Exception as exc:
        return _failed_update(f"Frozen Tool action 校验失败: {exc}")

    record = await journal.get(action_key)
    if record is not None:
        identity_error = _journal_identity_error(
            record,
            state=state,
            arguments_hash=arguments_hash,
            replay_safe=replay_safe,
        )
        if identity_error is not None:
            return _failed_update(identity_error)
        if record.status in {JournalStatus.SUCCEEDED, JournalStatus.FAILED}:
            await _trace(
                trace_recorder,
                state,
                TraceEventType.TOOL_MATERIALIZED_FROM_JOURNAL,
                step_id=action.step_id,
                call_id=action.call_id,
                status=record.status.value,
                payload={"tool_name": action.tool_name},
            )
            return _materialize_tool_call(state, record)
        authorized_attempt = record.attempt_count + 1
        has_one_shot_permit = (
            record.authorized_retry_attempt == authorized_attempt
        )
        if not replay_safe and not has_one_shot_permit:
            preview = redact_preview(action.arguments)
            await _trace(
                trace_recorder,
                state,
                TraceEventType.RECOVERY_REQUIRED,
                step_id=action.step_id,
                call_id=action.call_id,
                status=TaskStatus.INTERRUPTED.value,
                payload={"tool_name": action.tool_name, "reason": "ambiguous_started"},
            )
            return {
                "recovery_issue": RecoveryIssue(
                    action_key=action_key,
                    task_id=state["task_id"],
                    step_id=action.step_id,
                    action_index=action.action_index,
                    tool_name=action.tool_name or "",
                    call_id=action.call_id,
                    reason=("Journal 仅有 STARTED；Tool 可能尚未执行、执行中，"
                            "或远端已产生副作用但终态未提交"),
                    replay_safe=False,
                    arguments_preview=cast(dict[str, Any], preview),
                ),
                "status": TaskStatus.INTERRUPTED,
                "error": None,
            }
        record = (
            await journal.retry_started(action_key)
            if replay_safe
            else await journal.consume_authorized_retry(action_key)
        )
    else:
        index = _find_unique_step_index(state["plan"], action.step_id)
        assert index is not None
        now = datetime.now(UTC)
        record = ActionJournalRecord(
            action_key=action_key,
            task_id=state["task_id"],
            step_id=action.step_id,
            retry_count=state["plan"][index].retry_count,
            action_index=action.action_index,
            call_id=action.call_id,
            tool_name=action.tool_name or "",
            arguments_hash=arguments_hash,
            status=JournalStatus.STARTED,
            replay_safe=replay_safe,
            created_at=now,
            updated_at=now,
        )
        await journal.start(record)
        fault_injector.hit(ExecutionFaultPoint.AFTER_JOURNAL_STARTED, action_key)

    await _trace(
        trace_recorder,
        state,
        TraceEventType.TOOL_STARTED,
        step_id=action.step_id,
        call_id=action.call_id,
        status=ToolCallStatus.RUNNING.value,
        payload={
            "tool_name": action.tool_name,
            "arguments_preview": redact_preview(action.arguments),
            "replay_safe": replay_safe,
        },
    )
    invocation_started = perf_counter()
    try:
        result = await executor.invoke_frozen_action(action)
        await _trace_vision_runtime_result(
            trace_recorder,
            state,
            step_id=action.step_id,
            call_id=action.call_id,
            result=result,
        )
        fault_injector.hit(
            ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL,
            action_key,
        )
    except Exception as exc:
        await _trace_vision_runtime_error(
            trace_recorder,
            state,
            step_id=action.step_id,
            call_id=action.call_id,
            error=exc,
        )
        detail = redact_sensitive_values(str(exc))
        record = await journal.fail(action_key, f"{type(exc).__name__}: {detail}")
    else:
        record = await journal.succeed(action_key, result)
    terminal_type = (
        TraceEventType.TOOL_SUCCEEDED
        if record.status is JournalStatus.SUCCEEDED
        else TraceEventType.TOOL_FAILED
    )
    await _trace(
        trace_recorder,
        state,
        terminal_type,
        step_id=action.step_id,
        call_id=action.call_id,
        status=record.status.value,
        latency_ms=(perf_counter() - invocation_started) * 1000,
        payload={"tool_name": action.tool_name, "error": record.error},
    )
    fault_injector.hit(
        ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN,
        action_key,
    )
    return _materialize_tool_call(state, record)


def _vision_trace_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    """只提取 Vision 审计元数据，明确排除 screenshot bytes/base64 本体。"""

    return {
        "confidence": metadata.get("confidence"),
        "reason": metadata.get("reason"),
        "fallback_reason": metadata.get("fallback_reason"),
        "viewport": {
            "width": metadata.get("viewport_width"),
            "height": metadata.get("viewport_height"),
        },
        "screenshot_bytes": metadata.get("screenshot_bytes"),
    }


async def _trace_vision_runtime_result(
    recorder: TraceRecorderLike,
    state: TaskState,
    *,
    step_id: int,
    call_id: str,
    result: Any,
) -> None:
    """成功的 DOM click 不留 Vision 事件；视觉成功记录 requested + found。"""

    if not isinstance(result, dict) or result.get("interaction_mode") != "vision":
        return
    metadata = {
        "confidence": result.get("confidence"),
        "reason": result.get("vision_reason"),
        "fallback_reason": result.get("fallback_reason"),
        "viewport_width": result.get("viewport_width"),
        "viewport_height": result.get("viewport_height"),
        "screenshot_bytes": result.get("screenshot_bytes"),
    }
    payload = _vision_trace_payload(metadata)
    await _trace(
        recorder,
        state,
        TraceEventType.VISION_FALLBACK_REQUESTED,
        step_id=step_id,
        call_id=call_id,
        status="requested",
        payload=payload,
    )
    await _trace(
        recorder,
        state,
        TraceEventType.VISION_TARGET_FOUND,
        step_id=step_id,
        call_id=call_id,
        status="found",
        payload=payload,
    )


async def _trace_vision_runtime_error(
    recorder: TraceRecorderLike,
    state: TaskState,
    *,
    step_id: int,
    call_id: str,
    error: BaseException,
) -> None:
    """只识别 BrowserVisionError 风格的安全 metadata，不序列化异常对象。"""

    metadata = getattr(error, "vision_metadata", None)
    if not isinstance(metadata, dict):
        return
    payload = _vision_trace_payload(metadata)
    await _trace(
        recorder,
        state,
        TraceEventType.VISION_FALLBACK_REQUESTED,
        step_id=step_id,
        call_id=call_id,
        status="requested",
        payload=payload,
    )
    event_type = (
        TraceEventType.VISION_TARGET_FOUND
        if metadata.get("status") == "found"
        else TraceEventType.VISION_TARGET_REJECTED
    )
    await _trace(
        recorder,
        state,
        event_type,
        step_id=step_id,
        call_id=call_id,
        status=str(metadata.get("status") or "rejected"),
        payload=payload,
    )


def recovery_review(state: TaskState) -> TaskStateUpdate:
    """对不可安全重放的 ambiguous STARTED action 发起真实 interrupt。"""

    issue = state.get("recovery_issue")
    if issue is None:
        return _failed_update("recovery_review 需要 RecoveryIssue")
    payload = {
        "type": "ambiguous_tool_recovery",
        "action_key": issue.action_key,
        "tool_name": issue.tool_name,
        "reason": issue.reason,
        "arguments_preview": issue.arguments_preview,
        "choices": [RecoveryChoice.RETRY.value, RecoveryChoice.ABORT.value],
    }
    resume_value = interrupt(payload)
    try:
        response = RecoveryResponse.model_validate(resume_value)
    except ValidationError as exc:
        return _failed_update(f"Recovery resume value 无效，Tool 未执行: {exc}")
    if response.choice is RecoveryChoice.ABORT:
        updated = issue.model_copy(update={"resolution_reason": response.reason})
        return {
            "recovery_issue": updated,
            "status": TaskStatus.FAILED,
            "error": response.reason or f"Human aborted ambiguous action {issue.action_key}",
        }
    updated = issue.model_copy(
        update={"retry_authorized": False, "resolution_reason": response.reason}
    )
    return {
        "recovery_issue": updated,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def recovery_review_traced(
    state: TaskState, *, trace_recorder: TraceRecorderLike
) -> TaskStateUpdate:
    update = recovery_review(state)
    issue = state.get("recovery_issue")
    if issue is not None:
        status = update.get("status")
        await _trace(
            trace_recorder,
            state,
            TraceEventType.RECOVERY_RESOLVED,
            step_id=issue.step_id,
            call_id=issue.call_id,
            status=status.value if isinstance(status, TaskStatus) else str(status),
            payload={"resolution_reason": update.get("error")},
        )
    return update


async def authorize_recovery_retry(
    state: TaskState,
    *,
    journal: ActionJournalLike,
) -> TaskStateUpdate:
    """把 Human RETRY 转换为 Journal 中下一 attempt 的 one-shot permit。"""

    issue = state.get("recovery_issue")
    action = state.get("pending_executor_action")
    if issue is None or action is None:
        return _failed_update("authorize_recovery_retry 缺少 RecoveryIssue 或 frozen action")
    try:
        action_key = action_key_for(state)
    except Exception as exc:
        return _failed_update(f"Recovery action identity 无效: {exc}")
    if (
        issue.action_key != action_key
        or issue.task_id != state["task_id"]
        or issue.step_id != action.step_id
        or issue.action_index != action.action_index
        or issue.call_id != action.call_id
        or issue.tool_name != action.tool_name
    ):
        return _failed_update("RecoveryIssue 与 frozen action identity 不一致")
    try:
        record = await journal.authorize_retry(action_key)
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        return _failed_update(
            f"Recovery retry permit durable 写入失败: {type(exc).__name__}: {detail}"
        )
    if record.authorized_retry_attempt != record.attempt_count + 1:
        return _failed_update("Recovery retry permit attempt identity 不一致")
    return {
        "recovery_issue": issue.model_copy(update={"retry_authorized": False}),
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def reset_step_attempt(state: TaskState) -> TaskStateUpdate:
    """Verifier reject 后开始新 attempt，不继承上一轮 action counter。"""

    return {
        "current_action_index": 0,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "step_outcome": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def verify_step_traced(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = verify_step(
        state,
        verifier=verifier,
        max_step_attempts=max_step_attempts,
        max_replans=max_replans,
    )
    result = update.get("step_verification")
    verified = result is not None and result.verified
    await _trace(
        trace_recorder,
        state,
        TraceEventType.STEP_VERIFIED if verified else TraceEventType.STEP_REJECTED,
        step_id=state.get("current_step_id"),
        status="verified" if verified else "rejected",
        latency_ms=(perf_counter() - started) * 1000,
        payload={
            "feedback": result.feedback if result is not None else update.get("error"),
            "replan_triggered": update.get("pending_replan") is not None,
            **(
                {"replan_trigger": update["pending_replan"].trigger.value}
                if update.get("pending_replan") is not None
                else {}
            ),
        },
    )
    if update.get("status") is TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


async def verify_task_traced(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
    finalizer: FinalAnswerGeneratorLike,
) -> TaskStateUpdate:
    started = perf_counter()
    update = verify_task(state, verifier=verifier, max_replans=max_replans)
    result = update.get("verification")
    completed = result is not None and result.completed
    await _trace(
        trace_recorder,
        state,
        TraceEventType.TASK_VERIFIED
        if completed
        else TraceEventType.TASK_VERIFICATION_REJECTED,
        status="completed" if completed else "rejected",
        latency_ms=(perf_counter() - started) * 1000,
        payload={
            "reason": result.reason if result is not None else update.get("error"),
            "missing_requirements": (
                result.missing_requirements if result is not None else []
            ),
            "replan_triggered": update.get("pending_replan") is not None,
        },
    )
    if completed and result is not None:
        task_spec = state.get("task_spec")
        if task_spec is None:
            return _failed_update("Final Answer 生成失败: task_spec 缺失")
        try:
            final_answer = finalizer.generate(
                task_spec=task_spec,
                step_results=state["step_results"],
                task_results=state["task_results"],
                verification=result,
            )
            if not final_answer.strip():
                raise ValueError("Final Answer 不能为空")
            update["final_answer"] = final_answer.strip()
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            update["status"] = TaskStatus.FAILED
            update["error"] = (
                "Final Answer 生成失败: "
                f"{type(exc).__name__}: {detail}"
            )
            await _trace(
                trace_recorder,
                state,
                TraceEventType.TASK_FAILED,
                status=TaskStatus.FAILED.value,
                payload={"error": update["error"]},
            )
            return update
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_COMPLETED,
            status=TaskStatus.COMPLETED.value,
        )
    elif update.get("status") is TaskStatus.FAILED:
        await _trace(
            trace_recorder,
            state,
            TraceEventType.TASK_FAILED,
            status=TaskStatus.FAILED.value,
            payload={"error": update.get("error")},
        )
    return update


def advance_step_action_level(state: TaskState) -> TaskStateUpdate:
    """复用 Step 推进逻辑，并为下一 Step 重置 action-level 字段。"""

    update = advance_step(state)
    update["current_action_index"] = 0
    update["pending_executor_action"] = None
    update["pending_action"] = None
    update["recovery_issue"] = None
    return update


def _route_after_decision(
    state: TaskState,
) -> Literal["assess_policy", "build_step_outcome", "verify_step", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    action = state.get("pending_executor_action")
    if action is None:
        return "verify_step"
    return (
        "build_step_outcome"
        if action.action_type is ExecutorActionType.FINISH_STEP
        else "assess_policy"
    )


def _route_after_policy(
    state: TaskState,
) -> Literal["policy_feedback", "prepare_approval", "execute_tool_action", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    decisions = state.get("policy_decisions", [])
    action = state.get("pending_executor_action")
    if decisions and action is not None and decisions[-1].call_id == action.call_id:
        if decisions[-1].outcome is PolicyOutcome.DENY:
            return "policy_feedback"
    return "execute_tool_action"


def _route_after_tool_execution(
    state: TaskState,
) -> Literal["decide_action", "recovery_review", "end"]:
    if state["status"] is TaskStatus.INTERRUPTED:
        return "recovery_review"
    if state["status"] is TaskStatus.FAILED:
        return "end"
    return "decide_action"


def _route_after_recovery(
    state: TaskState,
) -> Literal["authorize_recovery_retry", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "authorize_recovery_retry"


def _route_after_recovery_authorization(
    state: TaskState,
) -> Literal["execute_tool_action", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "execute_tool_action"


def _route_after_action_verification(
    state: TaskState,
) -> Literal["advance_step", "reset_step_attempt", "replan_task", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    if state.get("pending_replan") is not None:
        return "replan_task"
    result = state.get("step_verification")
    return "advance_step" if result is not None and result.verified else "reset_step_attempt"


def _route_after_action_advance(
    state: TaskState,
) -> Literal["decide_action", "verify_task", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    return "verify_task" if state["current_step_id"] is None else "decide_action"


def _route_after_task_verification(
    state: TaskState,
) -> Literal["replan_task", "end"]:
    return "replan_task" if state.get("pending_replan") is not None else "end"


def _route_after_replan(state: TaskState) -> Literal["begin_step", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "begin_step"


def _build_action_level_graph(
    *,
    analyzer: TaskAnalyzerLike,
    planner: TaskPlannerLike,
    executor: TaskExecutor,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    checkpointer: BaseCheckpointSaver[Any] | None,
    journal: ActionJournalLike,
    fault_injector: ExecutionFaultInjector,
    replanner: TaskReplannerLike,
    context_builder: ContextBuilder,
    max_replans: int,
    trace_recorder: TraceRecorderLike,
    finalizer: FinalAnswerGeneratorLike,
) -> CompiledStateGraph:
    builder = StateGraph(TaskState)
    builder.add_node(
        "initialize_task",
        partial(initialize_task_traced, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "analyze_task",
        partial(
            analyze_task_traced,
            analyzer=analyzer,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node(
        "plan_task",
        partial(plan_task_traced, planner=planner, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "begin_step", partial(begin_step_traced, trace_recorder=trace_recorder)
    )
    builder.add_node(
        "decide_action",
        partial(decide_action, executor=executor, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "assess_policy",
        partial(assess_policy, executor=executor, trace_recorder=trace_recorder),
    )
    builder.add_node("policy_feedback", policy_feedback)
    builder.add_node(
        "prepare_approval",
        partial(prepare_approval_traced, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "human_approval",
        partial(human_approval_traced, trace_recorder=trace_recorder),
    )
    builder.add_node("rejection_feedback", rejection_feedback)
    builder.add_node(
        "execute_tool_action",
        partial(
            execute_tool_action,
            executor=executor,
            journal=journal,
            fault_injector=fault_injector,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node(
        "recovery_review",
        partial(recovery_review_traced, trace_recorder=trace_recorder),
    )
    builder.add_node(
        "authorize_recovery_retry",
        partial(authorize_recovery_retry, journal=journal),
    )
    builder.add_node("build_step_outcome", build_step_outcome)
    builder.add_node(
        "verify_step",
        partial(
            verify_step_traced,
            verifier=verifier,
            max_step_attempts=max_step_attempts,
            max_replans=max_replans,
            trace_recorder=trace_recorder,
        ),
    )
    builder.add_node("reset_step_attempt", reset_step_attempt)
    builder.add_node("advance_step", advance_step_action_level)
    builder.add_node(
        "verify_task",
        partial(
            verify_task_traced,
            verifier=verifier,
            max_replans=max_replans,
            trace_recorder=trace_recorder,
            finalizer=finalizer,
        ),
    )
    builder.add_node(
        "replan_task",
        partial(
            replan_task,
            replanner=replanner,
            context_builder=context_builder,
            max_replans=max_replans,
            trace_recorder=trace_recorder,
        ),
    )

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task", _route_after_plan, {"execute_step": "begin_step", "end": END}
    )
    builder.add_edge("begin_step", "decide_action")
    builder.add_conditional_edges(
        "decide_action",
        _route_after_decision,
        {
            "assess_policy": "assess_policy",
            "build_step_outcome": "build_step_outcome",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "assess_policy",
        _route_after_policy,
        {
            "policy_feedback": "policy_feedback",
            "prepare_approval": "prepare_approval",
            "execute_tool_action": "execute_tool_action",
            "end": END,
        },
    )
    builder.add_edge("policy_feedback", "decide_action")
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_tool_action",
            "handle_rejection": "rejection_feedback",
            "end": END,
        },
    )
    builder.add_edge("rejection_feedback", "decide_action")
    builder.add_conditional_edges(
        "execute_tool_action",
        _route_after_tool_execution,
        {"decide_action": "decide_action", "recovery_review": "recovery_review", "end": END},
    )
    builder.add_conditional_edges(
        "recovery_review",
        _route_after_recovery,
        {"authorize_recovery_retry": "authorize_recovery_retry", "end": END},
    )
    builder.add_conditional_edges(
        "authorize_recovery_retry",
        _route_after_recovery_authorization,
        {"execute_tool_action": "execute_tool_action", "end": END},
    )
    builder.add_edge("build_step_outcome", "verify_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_action_verification,
        {
            "advance_step": "advance_step",
            "reset_step_attempt": "reset_step_attempt",
            "replan_task": "replan_task",
            "end": END,
        },
    )
    builder.add_edge("reset_step_attempt", "decide_action")
    builder.add_conditional_edges(
        "advance_step",
        _route_after_action_advance,
        {"decide_action": "decide_action", "verify_task": "verify_task", "end": END},
    )
    builder.add_conditional_edges(
        "verify_task",
        _route_after_task_verification,
        {"replan_task": "replan_task", "end": END},
    )
    builder.add_conditional_edges(
        "replan_task",
        _route_after_replan,
        {"begin_step": "begin_step", "end": END},
    )
    return builder.compile(checkpointer=checkpointer)


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    action_journal: ActionJournalLike | None = None,
    fault_injector: ExecutionFaultInjector | None = None,
    replanner: TaskReplannerLike | None = None,
    max_replans: int = 2,
    context_builder: ContextBuilder | None = None,
    trace_recorder: TraceRecorderLike | None = None,
    finalizer: FinalAnswerGeneratorLike | None = None,
) -> CompiledStateGraph:
    """构建 Graph；真实 TaskExecutor 使用 action-level durable execution。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    if max_replans < 0:
        raise ValueError("max_replans 必须大于等于 0")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = executor if executor is not None else TaskExecutor(ToolRegistry())
    active_verifier = verifier if verifier is not None else TaskVerifier()
    if isinstance(active_executor, TaskExecutor):
        return _build_action_level_graph(
            analyzer=active_analyzer,
            planner=active_planner,
            executor=active_executor,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
            checkpointer=checkpointer,
            journal=action_journal or InMemoryActionJournal(),
            fault_injector=fault_injector or NoOpExecutionFaultInjector(),
            replanner=replanner or TaskReplanner(),
            context_builder=context_builder or ContextBuilder(),
            max_replans=max_replans,
            trace_recorder=trace_recorder or NoOpTraceRecorder(),
            finalizer=finalizer or VerifiedResultsRenderer(),
        )
    # Phase 1–8 的离线 Fake 只实现 execute；保留兼容图，不用于生产 CLI。
    return _build_legacy_graph(
        analyzer=active_analyzer,
        planner=active_planner,
        executor=active_executor,
        verifier=active_verifier,
        max_step_attempts=max_step_attempts,
        checkpointer=checkpointer,
    )
