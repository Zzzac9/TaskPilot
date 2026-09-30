"""Action Journal、故障注入与人工恢复的数据模型。"""

from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class JournalStatus(str, Enum):
    """一次外部动作的 durable journal 状态。"""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ActionJournalRecord(BaseModel):
    """不复制原始参数、仅保留 canonical hash 的动作记录。"""

    action_key: str
    task_id: str
    step_id: int
    retry_count: int
    action_index: int
    call_id: str
    tool_name: str
    arguments_hash: str
    status: JournalStatus
    replay_safe: bool = False
    attempt_count: int = 1
    # 非 replay-safe STARTED action 的下一次人工授权 attempt；消费后立即清空。
    authorized_retry_attempt: int | None = None
    result: Any | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class RecoveryChoice(str, Enum):
    """Ambiguous action 只允许显式重试或终止。"""

    RETRY = "retry"
    ABORT = "abort"


class RecoveryResponse(BaseModel):
    """严格校验 LangGraph recovery interrupt 的 resume value。"""

    model_config = ConfigDict(extra="forbid")

    choice: RecoveryChoice
    reason: str | None = None


class RecoveryIssue(BaseModel):
    """STARTED 无终态且不可自动重放时的人机审查材料。"""

    action_key: str
    task_id: str
    step_id: int
    action_index: int
    tool_name: str
    call_id: str
    reason: str
    replay_safe: bool
    arguments_preview: dict[str, Any] = Field(default_factory=dict)
    retry_authorized: bool = False
    resolution_reason: str | None = None


class ExecutionFaultPoint(str, Enum):
    """覆盖三段 crash window 的确定性测试注入点。"""

    AFTER_JOURNAL_STARTED = "after_journal_started"
    AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL = (
        "after_tool_return_before_terminal_journal"
    )
    AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN = (
        "after_terminal_journal_before_graph_return"
    )


class InjectedCrash(BaseException):
    """模拟进程死亡；继承 BaseException 避免被普通业务异常吞掉。"""


class ExecutionFaultInjector(Protocol):
    """生产默认 no-op、测试可替换的 crash hook。"""

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        """在指定持久化边界同步触发或直接返回。"""

        ...


class NoOpExecutionFaultInjector:
    """生产路径默认不注入故障。"""

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        return None
