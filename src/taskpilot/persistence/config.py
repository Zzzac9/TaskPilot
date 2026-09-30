"""Phase 9 本地持久化路径配置。"""

from pathlib import Path

from pydantic import BaseModel, field_validator


class PersistenceConfig(BaseModel):
    """Checkpoint、action journal 与 trace 使用彼此独立的 SQLite 文件。"""

    checkpoint_db_path: Path = Path(".taskpilot/checkpoints.sqlite3")
    action_journal_db_path: Path = Path(".taskpilot/actions.sqlite3")
    trace_db_path: Path = Path(".taskpilot/traces.sqlite3")

    @field_validator(
        "checkpoint_db_path", "action_journal_db_path", "trace_db_path", mode="after"
    )
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        """把调用方给出的相对路径固定为绝对路径。"""

        return value.expanduser().resolve()

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> "PersistenceConfig":
        """用 CLI 的 state directory 构造默认的两个数据库路径。"""

        resolved = state_dir.expanduser().resolve()
        return cls(
            checkpoint_db_path=resolved / "checkpoints.sqlite3",
            action_journal_db_path=resolved / "actions.sqlite3",
            trace_db_path=resolved / "traces.sqlite3",
        )
