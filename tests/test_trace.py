"""Phase 10 structured Trace recorder 离线测试。"""

from pathlib import Path

import pytest

from taskpilot.trace import (
    InMemoryTraceRecorder,
    NoOpTraceRecorder,
    SQLiteTraceRecorder,
    TraceEventType,
    make_trace_event,
    summarize_task_trace,
)


@pytest.mark.asyncio
async def test_in_memory_trace_preserves_event_order() -> None:
    recorder = InMemoryTraceRecorder()
    await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED))
    await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_COMPLETED))
    assert [event.event_type for event in await recorder.list_events("t")] == [
        TraceEventType.TASK_STARTED,
        TraceEventType.TASK_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_in_memory_trace_filters_tasks() -> None:
    recorder = InMemoryTraceRecorder()
    await recorder.record(make_trace_event(task_id="a", event_type=TraceEventType.TASK_STARTED))
    await recorder.record(make_trace_event(task_id="b", event_type=TraceEventType.TASK_STARTED))
    assert len(await recorder.list_events("a")) == 1


@pytest.mark.asyncio
async def test_count_by_type_uses_enum_keys() -> None:
    recorder = InMemoryTraceRecorder()
    for _ in range(2):
        await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TOOL_STARTED))
    assert (await recorder.count_by_type("t"))[TraceEventType.TOOL_STARTED] == 2


def test_make_trace_event_redacts_nested_secrets() -> None:
    event = make_trace_event(
        task_id="t",
        event_type=TraceEventType.TOOL_STARTED,
        payload={"arguments": {"authorization": "Bearer secret", "nested": {"token": "x"}}},
    )
    assert event.payload["arguments"]["authorization"] == "[REDACTED]"
    assert event.payload["arguments"]["nested"]["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_noop_trace_recorder_is_empty() -> None:
    recorder = NoOpTraceRecorder()
    await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED))
    assert await recorder.list_events("t") == []


@pytest.mark.asyncio
async def test_trace_summary_counts_real_events_and_latency() -> None:
    recorder = InMemoryTraceRecorder()
    types = [
        TraceEventType.TOOL_STARTED,
        TraceEventType.TOOL_FAILED,
        TraceEventType.STEP_REJECTED,
        TraceEventType.PLAN_REPLANNED,
        TraceEventType.APPROVAL_REQUESTED,
        TraceEventType.RECOVERY_REQUIRED,
    ]
    for event_type in types:
        await recorder.record(
            make_trace_event(task_id="t", event_type=event_type, latency_ms=2.5)
        )
    summary = await summarize_task_trace(recorder, "t")
    assert summary.event_count == 6
    assert summary.tool_call_count == 1
    assert summary.tool_failure_count == 1
    assert summary.step_rejection_count == 1
    assert summary.replan_count == 1
    assert summary.approval_request_count == 1
    assert summary.recovery_required_count == 1
    assert summary.observed_latency_ms == 15.0


@pytest.mark.asyncio
async def test_sqlite_trace_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "trace.sqlite3"
    async with SQLiteTraceRecorder(path) as recorder:
        event = make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED)
        await recorder.record(event)
        loaded = await recorder.list_events("t")
        assert loaded == [event]
        assert (await recorder.count_by_type("t"))[TraceEventType.TASK_STARTED] == 1


@pytest.mark.asyncio
async def test_sqlite_trace_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "trace.sqlite3"
    async with SQLiteTraceRecorder(path) as recorder:
        await recorder.record(make_trace_event(task_id="t", event_type=TraceEventType.TASK_STARTED))
    async with SQLiteTraceRecorder(path) as recorder:
        assert len(await recorder.list_events("t")) == 1


@pytest.mark.asyncio
async def test_sqlite_trace_requires_context_lifecycle(tmp_path: Path) -> None:
    recorder = SQLiteTraceRecorder(tmp_path / "trace.sqlite3")
    with pytest.raises(RuntimeError, match="async context"):
        await recorder.list_events("t")


def test_trace_event_is_json_serializable() -> None:
    event = make_trace_event(
        task_id="t", event_type=TraceEventType.CONTEXT_BUILT, payload={"chars": 12}
    )
    assert '"event_type":"context_built"' in event.model_dump_json()
