"""Benchmark manifest、oracle、raw run 与聚合结果模型。"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Benchmark 文件拒绝未知字段，避免静默接受拼写错误。"""

    model_config = ConfigDict(extra="forbid")


class BenchmarkCategory(str, Enum):
    MCP = "mcp"
    BROWSER_DOM = "browser_dom"
    FORM_INTERACTION = "form_interaction"
    CONSTRAINT_AGGREGATION = "constraint_aggregation"
    REPLANNING = "replanning"
    HITL = "hitl"


class OracleType(str, Enum):
    """独立 oracle 支持的确定性检查来源。"""

    STATE_CONTAINS = "state_contains"
    FIXTURE_STATE = "fixture_state"
    FILE_CONTENT = "file_content"


class RunDisposition(str, Enum):
    EXECUTED = "executed"
    NOT_RUN = "not_run"


class BenchmarkTask(StrictModel):
    """manifest 中固定、完全 JSON serializable 的单个任务。"""

    id: str
    category: BenchmarkCategory
    prompt: str
    requires_browser: bool = False
    requires_mcp: bool = False
    requires_vision: bool = False
    requires_approval: bool = False
    oracle_type: OracleType
    oracle_config: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(gt=0)


class OracleCheck(StrictModel):
    name: str
    passed: bool
    expected: Any = None
    observed: Any = None
    reason: str


class OracleResult(StrictModel):
    passed: bool
    reason: str
    checks: list[OracleCheck] = Field(default_factory=list)
    observed_output: Any = None


class BenchmarkRunRecord(StrictModel):
    """每次已启动任务的不可变 raw result。"""

    run_id: str
    task_id: str
    category: BenchmarkCategory
    attempt: int = Field(ge=1)
    disposition: RunDisposition = RunDisposition.EXECUTED
    task_status: str | None
    oracle_passed: bool | None
    success: bool
    error: str | None
    failure_category: str | None = None
    wall_latency_ms: float = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    tool_failure_count: int = Field(ge=0)
    llm_action_count: int = Field(ge=0)
    replan_count: int = Field(ge=0)
    step_rejection_count: int = Field(ge=0)
    approval_request_count: int = Field(ge=0)
    recovery_required_count: int = Field(ge=0)
    plan_revision: int = Field(ge=0)
    trace_event_count: int = Field(ge=0)
    false_completion: bool
    token_usage: int | None = Field(default=None, ge=0)
    oracle: OracleResult | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_execution_semantics(self) -> "BenchmarkRunRecord":
        if self.disposition is RunDisposition.NOT_RUN:
            if self.task_status is not None or self.oracle_passed is not None:
                raise ValueError("NOT_RUN 不得伪装为已执行状态或 oracle 结果")
            if self.success or self.false_completion:
                raise ValueError("NOT_RUN 不得计为 success/false completion")
        elif self.oracle_passed is None:
            raise ValueError("已执行 run 必须记录 oracle_passed")
        expected_success = (
            self.disposition is RunDisposition.EXECUTED
            and self.task_status == "completed"
            and self.oracle_passed is True
        )
        if self.success != expected_success:
            raise ValueError("success 必须同时满足 completed 与 external oracle")
        expected_false_completion = (
            self.disposition is RunDisposition.EXECUTED
            and self.task_status == "completed"
            and self.oracle_passed is False
        )
        if self.false_completion != expected_false_completion:
            raise ValueError("false_completion 定义不一致")
        return self


class WilsonInterval(StrictModel):
    lower: float
    upper: float


class RateMetric(StrictModel):
    numerator: int
    denominator: int
    percentage: float | None
    confidence_95: WilsonInterval | None = None


class CategoryResult(StrictModel):
    category: BenchmarkCategory
    success: RateMetric


class BenchmarkSummary(StrictModel):
    run_id: str
    declared_tasks: int
    eligible_tasks: int
    not_run_tasks: int
    executed_tasks: int
    succeeded_tasks: int
    failed_tasks: int
    task_success_rate: RateMetric
    false_completion_rate: RateMetric
    average_tool_calls_per_task: float | None
    average_tool_calls_per_successful_task: float | None
    tool_failure_rate: RateMetric
    average_replans_per_task: float | None
    replan_trigger_rate: RateMetric
    replan_recovery_rate: RateMetric
    step_rejection_rate: RateMetric
    approval_request_rate: RateMetric
    mean_wall_latency_ms: float | None
    median_wall_latency_ms: float | None
    p95_wall_latency_ms: float | None
    category_results: list[CategoryResult]


class ReliabilityCaseRecord(StrictModel):
    case_id: str
    mechanism: str
    passed: bool
    duplicate_unsafe_side_effects: int = Field(ge=0)
    terminal_journal_dedup_failures: int = Field(ge=0)
    recovery_interrupts_required: int = Field(ge=0)
    recovery_cases_resolved: int = Field(ge=0)
    browser_stale_runtime_executions: int = Field(ge=0)
    unauthorized_side_effects_before_approval: int = Field(ge=0)
    command: list[str]
    exit_code: int
    output: str


class ReliabilitySummary(StrictModel):
    cases_total: int
    cases_passed: int
    duplicate_unsafe_side_effects: int
    terminal_journal_dedup_failures: int
    recovery_interrupts_required: int
    recovery_cases_resolved: int
    browser_stale_runtime_executions: int
    unauthorized_side_effects_before_approval: int
