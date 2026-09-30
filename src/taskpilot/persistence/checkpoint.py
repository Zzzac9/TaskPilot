"""Host 管理的 AsyncSqliteSaver 生命周期。"""

from contextlib import AbstractAsyncContextManager
import sys
from types import TracebackType
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from taskpilot.persistence.config import PersistenceConfig


class SQLiteCheckpointProvider:
    """打开、setup 并关闭 durable LangGraph SQLite checkpointer。"""

    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self.checkpointer: AsyncSqliteSaver | None = None
        self._context: AbstractAsyncContextManager[AsyncSqliteSaver] | None = None

    async def __aenter__(self) -> "SQLiteCheckpointProvider":
        path = self.config.checkpoint_db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        context = AsyncSqliteSaver.from_conn_string(str(path))
        self._context = context
        try:
            self.checkpointer = await context.__aenter__()
            # 当前兼容版本没有 msgpack allowlist API；明确禁用 pickle fallback。
            self.checkpointer.serde = JsonPlusSerializer(pickle_fallback=False)
            await self.checkpointer.setup()
        except BaseException:
            # Windows 上 aiosqlite worker 未关闭会持续锁定数据库文件。
            await context.__aexit__(*sys.exc_info())
            self._context = None
            self.checkpointer = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        context = self._context
        self._context = None
        self.checkpointer = None
        if context is None:
            return None
        return await context.__aexit__(exc_type, exc, traceback)

    async def list_task_checkpoints(self, task_id: str) -> list[Any]:
        """用 saver 公开 alist API 返回指定 thread 的 checkpoint history。"""

        checkpointer = self.checkpointer
        if checkpointer is None:
            raise RuntimeError("SQLiteCheckpointProvider 必须先进入 async context")
        config = {"configurable": {"thread_id": task_id}}
        return [item async for item in checkpointer.alist(config)]
