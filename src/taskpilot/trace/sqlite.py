"""每条事件独立 commit 的单进程 SQLite Trace recorder。"""

import asyncio
from pathlib import Path

import aiosqlite

from taskpilot.trace.models import TraceEvent, TraceEventType


class SQLiteTraceRecorder:
    """使用独立 SQLite 文件保存 Trace，不污染 checkpoint 或 action journal。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path.expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "SQLiteTraceRecorder":
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._db_path)
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trace_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL
            )
            """
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trace_task_sequence "
            "ON trace_events(task_id, sequence)"
        )
        await self._connection.commit()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _active(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteTraceRecorder 尚未进入 async context")
        return self._connection

    async def record(self, event: TraceEvent) -> None:
        connection = self._active()
        async with self._lock:
            await connection.execute(
                "INSERT INTO trace_events(event_id, task_id, event_type, event_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.task_id,
                    event.event_type.value,
                    event.model_dump_json(),
                ),
            )
            await connection.commit()

    async def list_events(self, task_id: str) -> list[TraceEvent]:
        connection = self._active()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT event_json FROM trace_events WHERE task_id = ? "
                "ORDER BY sequence",
                (task_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [TraceEvent.model_validate_json(row[0]) for row in rows]

    async def count_by_type(self, task_id: str) -> dict[TraceEventType, int]:
        connection = self._active()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT event_type, COUNT(*) FROM trace_events "
                "WHERE task_id = ? GROUP BY event_type",
                (task_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return {TraceEventType(row[0]): int(row[1]) for row in rows}
