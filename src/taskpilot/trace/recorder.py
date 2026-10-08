"""Trace recorder 接口、内存实现与通用聚合。"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, Sequence

from taskpilot.config import redact_sensitive_values
from taskpilot.policy import redact_preview
from taskpilot.trace.models import (
    LatencyStats,
    TraceEvent,
    TraceEventType,
    TraceSummary,
)


ProgressHandler = Callable[[TraceEvent], Awaitable[None] | None]


class TraceRecorderLike(Protocol):
    """Graph 只依赖的最小异步 Trace 接口。"""

    async def record(self, event: TraceEvent) -> None: ...

    async def list_events(self, task_id: str) -> list[TraceEvent]: ...

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]: ...


def make_trace_event(
    *,
    task_id: str,
    event_type: TraceEventType,
    step_id: int | None = None,
    call_id: str | None = None,
    plan_revision: int = 0,
    status: str | None = None,
    latency_ms: float | None = None,
    payload: dict[str, Any] | None = None,
) -> TraceEvent:
    """创建事件并递归脱敏 payload 中可能出现的凭证字段。"""

    safe_payload = redact_preview(payload or {})

    def redact_known_values(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact_known_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_known_values(item) for item in value]
        if isinstance(value, str):
            return redact_sensitive_values(value)
        return value

    safe_payload = redact_known_values(safe_payload)
    if not isinstance(safe_payload, dict):  # pragma: no cover - 输入类型防御
        safe_payload = {"value": safe_payload}
    return TraceEvent(
        task_id=task_id,
        event_type=event_type,
        step_id=step_id,
        call_id=call_id,
        plan_revision=plan_revision,
        status=status,
        latency_ms=latency_ms,
        payload=safe_payload,
    )


class NoOpTraceRecorder:
    """默认不产生 I/O，保持库调用方无需配置 Trace。"""

    async def record(self, event: TraceEvent) -> None:
        return None

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        return []

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        return {}


class ProgressTraceRecorder:
    """先持久化 Trace，再把同一脱敏事件转发给实时进度消费者。"""

    def __init__(
        self,
        delegate: TraceRecorderLike,
        handler: ProgressHandler | None = None,
    ) -> None:
        self.delegate = delegate
        self.handler = handler

    async def record(self, event: TraceEvent) -> None:
        await self.delegate.record(event)
        handler = self.handler
        if handler is None:
            return
        try:
            result = handler(event.model_copy(deep=True))
            if inspect.isawaitable(result):
                await result
        except Exception:
            # 展示层故障不能改变 Graph、Checkpoint 或工具执行结果。
            return

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        return await self.delegate.list_events(task_id)

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        return await self.delegate.count_by_type(task_id)


class InMemoryTraceRecorder:
    """离线测试使用的轻量 recorder。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def record(self, event: TraceEvent) -> None:
        self.events.append(event.model_copy(deep=True))

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        return [event.model_copy(deep=True) for event in self.events if event.task_id == task_id]

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        return _count_types(await self.list_events(task_id))


def _count_types(events: Sequence[TraceEvent]) -> dict[TraceEventType, int]:
    counts: dict[TraceEventType, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return counts


def _latency_stats(samples: dict[str, list[float]]) -> dict[str, LatencyStats]:
    return {
        name: LatencyStats(
            call_count=len(values),
            total_ms=sum(values),
            average_ms=sum(values) / len(values),
            max_ms=max(values),
        )
        for name, values in sorted(samples.items())
        if values
    }


def _append_latency(
    samples: dict[str, list[float]], name: str | None, latency_ms: float | None
) -> None:
    if name and latency_ms is not None:
        samples.setdefault(name, []).append(latency_ms)


async def summarize_task_trace(
    recorder: TraceRecorderLike, task_id: str
) -> TraceSummary:
    """只根据实际存在的事件计算任务 Trace 摘要。"""

    events = await recorder.list_events(task_id)
    counts = _count_types(events)
    node_samples: dict[str, list[float]] = {}
    llm_samples: dict[str, list[float]] = {}
    browser_samples: dict[str, list[float]] = {}
    llm_event_names = {
        TraceEventType.TASK_ANALYZED: "analyzer",
        TraceEventType.PLAN_CREATED: "planner",
        TraceEventType.PLAN_REPLANNED: "replanner",
        TraceEventType.LLM_ACTION_DECIDED: "executor",
        TraceEventType.STEP_VERIFIED: "step_verifier",
        TraceEventType.STEP_REJECTED: "step_verifier",
        TraceEventType.TASK_VERIFIED: "task_verifier",
        TraceEventType.TASK_VERIFICATION_REJECTED: "task_verifier",
        TraceEventType.FINAL_ANSWER_GENERATED: "finalizer",
    }
    for event in events:
        if event.event_type is TraceEventType.NODE_COMPLETED:
            _append_latency(
                node_samples,
                str(event.payload.get("node_name") or "") or None,
                event.latency_ms,
            )
        llm_name = llm_event_names.get(event.event_type)
        if event.payload.get("model_invoked", True):
            _append_latency(llm_samples, llm_name, event.latency_ms)
        tool_name = str(event.payload.get("tool_name") or "")
        if (
            event.event_type
            in {TraceEventType.TOOL_SUCCEEDED, TraceEventType.TOOL_FAILED}
            and tool_name.startswith("browser_")
        ):
            _append_latency(browser_samples, tool_name, event.latency_ms)
    wall_latency_ms = None
    if events:
        wall_latency_ms = max(
            0.0,
            (events[-1].timestamp - events[0].timestamp).total_seconds() * 1000,
        )
    return TraceSummary(
        event_count=len(events),
        tool_call_count=counts.get(TraceEventType.TOOL_STARTED, 0),
        tool_failure_count=counts.get(TraceEventType.TOOL_FAILED, 0),
        executor_correction_count=counts.get(TraceEventType.EXECUTOR_CORRECTED, 0),
        step_rejection_count=counts.get(TraceEventType.STEP_REJECTED, 0),
        replan_count=counts.get(TraceEventType.PLAN_REPLANNED, 0),
        approval_request_count=counts.get(TraceEventType.APPROVAL_REQUESTED, 0),
        recovery_required_count=counts.get(TraceEventType.RECOVERY_REQUIRED, 0),
        observed_latency_ms=sum(
            event.latency_ms
            for event in events
            if event.latency_ms is not None
            and event.event_type is not TraceEventType.NODE_COMPLETED
        ),
        wall_latency_ms=wall_latency_ms,
        graph_nodes=_latency_stats(node_samples),
        llm_calls=_latency_stats(llm_samples),
        browser_tools=_latency_stats(browser_samples),
    )
