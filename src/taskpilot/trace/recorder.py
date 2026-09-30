"""Trace recorder 接口、内存实现与通用聚合。"""

from typing import Any, Protocol, Sequence

from taskpilot.config import redact_sensitive_values
from taskpilot.policy import redact_preview
from taskpilot.trace.models import TraceEvent, TraceEventType, TraceSummary


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


async def summarize_task_trace(
    recorder: TraceRecorderLike, task_id: str
) -> TraceSummary:
    """只根据实际存在的事件计算任务 Trace 摘要。"""

    events = await recorder.list_events(task_id)
    counts = _count_types(events)
    return TraceSummary(
        event_count=len(events),
        tool_call_count=counts.get(TraceEventType.TOOL_STARTED, 0),
        tool_failure_count=counts.get(TraceEventType.TOOL_FAILED, 0),
        step_rejection_count=counts.get(TraceEventType.STEP_REJECTED, 0),
        replan_count=counts.get(TraceEventType.PLAN_REPLANNED, 0),
        approval_request_count=counts.get(TraceEventType.APPROVAL_REQUESTED, 0),
        recovery_required_count=counts.get(TraceEventType.RECOVERY_REQUIRED, 0),
        observed_latency_ms=sum(
            event.latency_ms for event in events if event.latency_ms is not None
        ),
    )
