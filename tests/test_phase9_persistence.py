"""Phase 9 SQLite checkpoint、journal 与 metadata 的离线测试。"""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from langgraph.graph import END, START, StateGraph

from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    StepResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.browser.tools import BrowserTools
from taskpilot.persistence import (
    ActionJournalRecord,
    JournalStatus,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.persistence.journal import utc_now
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
)
from taskpilot.state import TaskState


def complete_state() -> TaskState:
    decision = PolicyDecision(
        call_id="call-1",
        step_id=1,
        tool_name="local_read",
        risk_level=RiskLevel.HIGH,
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        reason="offline fixture",
        tool_source="local",
        preview={"query": "safe"},
    )
    return {
        "task_id": "round-trip-task",
        "user_input": "持久化完整状态",
        "task_spec": TaskSpec(
            goal="持久化完整状态",
            constraints={"offline": True},
            completion_criteria=["类型可恢复"],
        ),
        "plan": [PlanStep(id=1, description="测试", success_criteria=["通过"])],
        "current_step_id": 1,
        "step_outcome": None,
        "step_results": [StepResult(step_id=1, summary="已有结果")],
        "step_verification": None,
        "tool_calls": [
            ToolCallRecord(
                call_id="old-call",
                step_id=1,
                tool_name="local_read",
                status=ToolCallStatus.SUCCESS,
                result={"ok": True},
            )
        ],
        "task_results": {"answer": 42},
        "verification": None,
        "pending_action": PendingToolAction(
            call_id="call-1",
            step_id=1,
            tool_name="local_read",
            arguments={"query": "safe"},
            policy_decision=decision,
        ),
        "policy_decisions": [decision],
        "approval_records": [
            ApprovalRecord(
                call_id="call-1",
                step_id=1,
                tool_name="local_read",
                decision=ApprovalChoice.APPROVE,
            )
        ],
        "current_action_index": 1,
        "pending_executor_action": ExecutorAction(
            step_id=1,
            action_index=1,
            call_id="call-1",
            action_type=ExecutorActionType.TOOL,
            tool_name="local_read",
            arguments={"query": "safe"},
        ),
        "recovery_issue": None,
        "status": TaskStatus.WAITING_APPROVAL,
        "error": None,
    }


def checkpoint_graph(checkpointer: Any):
    builder = StateGraph(TaskState)
    builder.add_node("persist", lambda state: {})
    builder.add_edge(START, "persist")
    builder.add_edge("persist", END)
    return builder.compile(checkpointer=checkpointer)


def test_persistence_config_resolves_paths(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path / "state")

    assert config.checkpoint_db_path.is_absolute()
    assert config.action_journal_db_path.is_absolute()
    assert config.checkpoint_db_path.name == "checkpoints.sqlite3"
    assert config.action_journal_db_path.name == "actions.sqlite3"


def test_browser_replay_rules_are_conservative_and_explicit() -> None:
    tools = {tool.name: tool.metadata for tool in BrowserTools(object()).build()}

    assert tools["browser_observe"]["replay_safe"] is True
    assert tools["browser_navigate"]["replay_safe"] is True
    assert tools["browser_fill"]["replay_safe"] is True
    assert tools["browser_click"]["replay_safe"] is False
    assert tools["browser_download"]["replay_safe"] is False
    assert tools["browser_screenshot"]["replay_safe"] is False
    assert tools["browser_switch_page"]["replay_safe"] is False


def test_task_state_schema_contains_no_live_runtime_objects() -> None:
    forbidden = {
        "registry", "browser_session", "page", "mcp_session",
        "llm", "sqlite_connection", "checkpointer",
    }

    assert forbidden.isdisjoint(TaskState.__annotations__)


@pytest.mark.asyncio
async def test_sqlite_checkpoint_round_trip_restores_structured_state(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    graph_config = {"configurable": {"thread_id": "round-trip-task"}}

    async with SQLiteCheckpointProvider(config) as first:
        assert first.checkpointer is not None
        await checkpoint_graph(first.checkpointer).ainvoke(
            complete_state(), config=graph_config
        )
        history = await first.list_task_checkpoints("round-trip-task")
        assert len(history) >= 2

    async with SQLiteCheckpointProvider(config) as second:
        assert second.checkpointer is not None
        snapshot = await checkpoint_graph(second.checkpointer).aget_state(graph_config)

    restored = snapshot.values
    assert isinstance(restored["task_spec"], TaskSpec)
    assert isinstance(restored["plan"][0], PlanStep)
    assert isinstance(restored["step_results"][0], StepResult)
    assert isinstance(restored["tool_calls"][0], ToolCallRecord)
    assert isinstance(restored["policy_decisions"][0], PolicyDecision)
    assert isinstance(restored["pending_action"], PendingToolAction)
    assert isinstance(restored["approval_records"][0], ApprovalRecord)
    assert isinstance(restored["pending_executor_action"], ExecutorAction)
    assert restored["task_id"] == "round-trip-task"


@pytest.mark.asyncio
async def test_sqlite_action_journal_commits_started_retry_and_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    now = utc_now()
    original = ActionJournalRecord(
        action_key="task:1:0:1",
        task_id="task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="local_read",
        arguments_hash="abc",
        status=JournalStatus.STARTED,
        replay_safe=True,
        created_at=now,
        updated_at=now,
    )
    async with SQLiteActionJournal(path) as journal:
        await journal.start(original)
    async with SQLiteActionJournal(path) as journal:
        started = await journal.get(original.action_key)
        assert started is not None
        assert started.status is JournalStatus.STARTED
        retried = await journal.retry_started(original.action_key)
        assert retried.attempt_count == 2
        terminal = await journal.succeed(original.action_key, {"ok": True})
        assert terminal.status is JournalStatus.SUCCEEDED
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None
        assert restored.result == {"ok": True}
        assert restored.attempt_count == 2


@pytest.mark.asyncio
async def test_sqlite_action_journal_one_shot_retry_permit_is_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "one-shot-journal.sqlite3"
    now = utc_now()
    original = ActionJournalRecord(
        action_key="task:1:0:1",
        task_id="task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="unsafe_write",
        arguments_hash="abc",
        status=JournalStatus.STARTED,
        replay_safe=False,
        created_at=now,
        updated_at=now,
    )
    async with SQLiteActionJournal(path) as journal:
        await journal.start(original)
        authorized = await journal.authorize_retry(original.action_key)
        assert authorized.attempt_count == 1
        assert authorized.authorized_retry_attempt == 2
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None and restored.authorized_retry_attempt == 2
        consumed = await journal.consume_authorized_retry(original.action_key)
        assert consumed.attempt_count == 2
        assert consumed.authorized_retry_attempt is None
        with pytest.raises(ValueError, match="不存在、已消费"):
            await journal.consume_authorized_retry(original.action_key)
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None
        assert restored.attempt_count == 2
        assert restored.authorized_retry_attempt is None


@pytest.mark.asyncio
async def test_sqlite_action_journal_migrates_phase9_schema(tmp_path: Path) -> None:
    path = tmp_path / "phase9-schema.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            """
            CREATE TABLE action_journal (
                action_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                retry_count INTEGER NOT NULL,
                action_index INTEGER NOT NULL,
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                replay_safe INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await connection.commit()
    async with SQLiteActionJournal(path):
        pass
    async with aiosqlite.connect(path) as connection:
        cursor = await connection.execute("PRAGMA table_info(action_journal)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
    assert "authorized_retry_attempt" in columns
