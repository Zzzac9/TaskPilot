"""独立于 LangGraph schema 的 SQLite external-action journal。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

import aiosqlite

from taskpilot.persistence.models import ActionJournalRecord, JournalStatus


class ActionJournalLike(Protocol):
    """SQLite journal 与离线 memory double 共用的最小异步接口。"""

    async def get(self, action_key: str) -> ActionJournalRecord | None: ...

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord: ...

    async def retry_started(self, action_key: str) -> ActionJournalRecord: ...

    async def authorize_retry(self, action_key: str) -> ActionJournalRecord: ...

    async def consume_authorized_retry(
        self, action_key: str
    ) -> ActionJournalRecord: ...

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord: ...

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class SQLiteActionJournal:
    """每次状态写入都独立 commit，缩小但不掩盖 crash ambiguity window。"""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "SQLiteActionJournal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        self._connection = connection
        await self.setup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()

    async def setup(self) -> None:
        connection = self._require_connection()
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS action_journal (
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
                authorized_retry_attempt INTEGER,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor = await connection.execute("PRAGMA table_info(action_journal)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "authorized_retry_attempt" not in columns:
            # 兼容已有 Phase 9 actions.sqlite3，迁移只新增 nullable 字段。
            await connection.execute(
                "ALTER TABLE action_journal "
                "ADD COLUMN authorized_retry_attempt INTEGER"
            )
        await connection.commit()

    async def get(self, action_key: str) -> ActionJournalRecord | None:
        connection = self._require_connection()
        cursor = await connection.execute(
            "SELECT * FROM action_journal WHERE action_key = ?", (action_key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._from_row(row) if row is not None else None

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO action_journal (
                action_key, task_id, step_id, retry_count, action_index,
                call_id, tool_name, arguments_hash, status, replay_safe,
                attempt_count, result_json, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                record.action_key,
                record.task_id,
                record.step_id,
                record.retry_count,
                record.action_index,
                record.call_id,
                record.tool_name,
                record.arguments_hash,
                record.status.value,
                int(record.replay_safe),
                record.attempt_count,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await connection.commit()
        return record

    async def retry_started(self, action_key: str) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """UPDATE action_journal
               SET attempt_count = attempt_count + 1, updated_at = ?
               WHERE action_key = ? AND status = ? AND replay_safe = 1""",
            (utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        return self._require_record(await self.get(action_key), action_key)

    async def authorize_retry(self, action_key: str) -> ActionJournalRecord:
        """幂等签发仅适用于下一次 unsafe retry 的 durable permit。"""

        connection = self._require_connection()
        record = self._require_record(await self.get(action_key), action_key)
        if record.status is not JournalStatus.STARTED or record.replay_safe:
            raise ValueError("Recovery retry permit 只适用于 unsafe STARTED action")
        expected = record.attempt_count + 1
        if record.authorized_retry_attempt == expected:
            return record
        if record.authorized_retry_attempt is not None:
            raise ValueError("Action Journal 已存在不一致的 recovery retry permit")
        cursor = await connection.execute(
            """UPDATE action_journal
               SET authorized_retry_attempt = ?, updated_at = ?
               WHERE action_key = ? AND status = ? AND replay_safe = 0
                 AND authorized_retry_attempt IS NULL""",
            (expected, utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        updated_rows = cursor.rowcount
        await cursor.close()
        if updated_rows != 1:
            raise ValueError("Recovery retry permit durable 写入失败")
        return self._require_record(await self.get(action_key), action_key)

    async def consume_authorized_retry(self, action_key: str) -> ActionJournalRecord:
        """在 external invoke 前原子消费 permit 并增加 attempt_count。"""

        connection = self._require_connection()
        cursor = await connection.execute(
            """UPDATE action_journal
               SET attempt_count = attempt_count + 1,
                   authorized_retry_attempt = NULL,
                   updated_at = ?
               WHERE action_key = ? AND status = ? AND replay_safe = 0
                 AND authorized_retry_attempt = attempt_count + 1""",
            (utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        updated_rows = cursor.rowcount
        await cursor.close()
        if updated_rows != 1:
            raise ValueError("Recovery retry permit 不存在、已消费或 identity 不一致")
        return self._require_record(await self.get(action_key), action_key)

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord:
        return await self._terminal(
            action_key,
            JournalStatus.SUCCEEDED,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            error=None,
        )

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord:
        return await self._terminal(
            action_key,
            JournalStatus.FAILED,
            result_json=None,
            error=error,
        )

    async def _terminal(
        self,
        action_key: str,
        status: JournalStatus,
        *,
        result_json: str | None,
        error: str | None,
    ) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """UPDATE action_journal
               SET status = ?, result_json = ?, error = ?,
                   authorized_retry_attempt = NULL, updated_at = ?
               WHERE action_key = ? AND status = ?""",
            (
                status.value,
                result_json,
                error,
                utc_now().isoformat(),
                action_key,
                JournalStatus.STARTED.value,
            ),
        )
        await connection.commit()
        return self._require_record(await self.get(action_key), action_key)

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteActionJournal 必须先进入 async context")
        return self._connection

    @staticmethod
    def _require_record(
        record: ActionJournalRecord | None, action_key: str
    ) -> ActionJournalRecord:
        if record is None:
            raise RuntimeError(f"Action Journal record 不存在: {action_key}")
        return record

    @staticmethod
    def _from_row(row: aiosqlite.Row) -> ActionJournalRecord:
        return ActionJournalRecord(
            action_key=row["action_key"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            retry_count=row["retry_count"],
            action_index=row["action_index"],
            call_id=row["call_id"],
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            status=JournalStatus(row["status"]),
            replay_safe=bool(row["replay_safe"]),
            attempt_count=row["attempt_count"],
            authorized_retry_attempt=row["authorized_retry_attempt"],
            result=(
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            ),
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class InMemoryActionJournal:
    """无 SQLite Host 时的离线兼容实现；生产 CLI 不使用。"""

    def __init__(self) -> None:
        self.records: dict[str, ActionJournalRecord] = {}

    async def get(self, action_key: str) -> ActionJournalRecord | None:
        return self.records.get(action_key)

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord:
        if record.action_key in self.records:
            raise ValueError(f"Action Journal key 已存在: {record.action_key}")
        self.records[record.action_key] = record
        return record

    async def retry_started(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"attempt_count": self.records[action_key].attempt_count + 1,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

    async def authorize_retry(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key]
        if record.status is not JournalStatus.STARTED or record.replay_safe:
            raise ValueError("Recovery retry permit 只适用于 unsafe STARTED action")
        expected = record.attempt_count + 1
        if record.authorized_retry_attempt not in {None, expected}:
            raise ValueError("Action Journal 已存在不一致的 recovery retry permit")
        updated = record.model_copy(
            update={
                "authorized_retry_attempt": expected,
                "updated_at": utc_now(),
            }
        )
        self.records[action_key] = updated
        return updated

    async def consume_authorized_retry(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key]
        expected = record.attempt_count + 1
        if (
            record.status is not JournalStatus.STARTED
            or record.replay_safe
            or record.authorized_retry_attempt != expected
        ):
            raise ValueError("Recovery retry permit 不存在、已消费或 identity 不一致")
        updated = record.model_copy(
            update={
                "attempt_count": expected,
                "authorized_retry_attempt": None,
                "updated_at": utc_now(),
            }
        )
        self.records[action_key] = updated
        return updated

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"status": JournalStatus.SUCCEEDED, "result": result,
                    "error": None, "authorized_retry_attempt": None,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"status": JournalStatus.FAILED, "result": None,
                    "error": error, "authorized_retry_attempt": None,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record
