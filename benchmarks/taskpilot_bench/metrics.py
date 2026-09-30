"""Primary、category、replan 与 stability 指标的确定性聚合。"""

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence

from benchmarks.taskpilot_bench.models import (
    BenchmarkRunRecord,
    BenchmarkSummary,
    CategoryResult,
    RateMetric,
    RunDisposition,
    WilsonInterval,
)


def wilson_interval(
    successes: int, total: int, z: float = 1.959963984540054
) -> WilsonInterval | None:
    """计算 binomial proportion 的 95% Wilson score interval。"""

    if successes < 0 or total < 0 or successes > total:
        raise ValueError("successes/total 无效")
    if total == 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return WilsonInterval(
        lower=max(0.0, centre - margin), upper=min(1.0, centre + margin)
    )


def rate(numerator: int, denominator: int, *, confidence: bool = False) -> RateMetric:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError("rate numerator/denominator 无效")
    return RateMetric(
        numerator=numerator,
        denominator=denominator,
        percentage=(numerator / denominator * 100 if denominator else None),
        confidence_95=(wilson_interval(numerator, denominator) if confidence else None),
    )


def percentile_nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    """使用 nearest-rank，避免不同统计库插值策略导致报告漂移。"""

    if not values:
        return None
    if not 0 < percentile <= 1:
        raise ValueError("percentile 必须在 (0, 1] 内")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _mean(values: Iterable[float]) -> float | None:
    collected = list(values)
    return statistics.fmean(collected) if collected else None


def aggregate_runs(
    run_id: str,
    records: Sequence[BenchmarkRunRecord],
    *,
    declared_tasks: int,
) -> BenchmarkSummary:
    """只把 EXECUTED 放进 denominator；所有已启动失败均保留。"""

    if declared_tasks < len({record.task_id for record in records}):
        raise ValueError("declared_tasks 小于 raw record 中的 task 数")
    executed = [
        record for record in records if record.disposition is RunDisposition.EXECUTED
    ]
    not_run = [
        record for record in records if record.disposition is RunDisposition.NOT_RUN
    ]
    succeeded = [record for record in executed if record.success]
    failed = [record for record in executed if not record.success]
    if len(succeeded) + len(failed) != len(executed):  # pragma: no cover
        raise AssertionError("S + F 必须等于 executed")

    tool_calls = sum(record.tool_call_count for record in executed)
    tool_failures = sum(record.tool_failure_count for record in executed)
    replanned = sum(record.replan_count > 0 for record in executed)
    accepted_replans = [record for record in executed if record.replan_count > 0]
    approval_tasks = sum(record.approval_request_count > 0 for record in executed)
    false_completions = sum(record.false_completion for record in executed)

    grouped: dict[object, list[BenchmarkRunRecord]] = defaultdict(list)
    for record in executed:
        grouped[record.category].append(record)
    categories = [
        CategoryResult(
            category=category,
            success=rate(
                sum(item.success for item in items), len(items), confidence=True
            ),
        )
        for category, items in sorted(grouped.items(), key=lambda pair: pair[0].value)
    ]
    latencies = [record.wall_latency_ms for record in executed]
    return BenchmarkSummary(
        run_id=run_id,
        declared_tasks=declared_tasks,
        eligible_tasks=declared_tasks - len(not_run),
        not_run_tasks=len(not_run),
        executed_tasks=len(executed),
        succeeded_tasks=len(succeeded),
        failed_tasks=len(failed),
        task_success_rate=rate(len(succeeded), len(executed), confidence=True),
        false_completion_rate=rate(false_completions, len(executed)),
        average_tool_calls_per_task=_mean(
            record.tool_call_count for record in executed
        ),
        average_tool_calls_per_successful_task=_mean(
            record.tool_call_count for record in succeeded
        ),
        tool_failure_rate=rate(tool_failures, tool_calls),
        average_replans_per_task=_mean(record.replan_count for record in executed),
        replan_trigger_rate=rate(replanned, len(executed)),
        replan_recovery_rate=rate(
            sum(record.success for record in accepted_replans), len(accepted_replans)
        ),
        step_rejection_rate=rate(
            sum(record.step_rejection_count > 0 for record in executed), len(executed)
        ),
        approval_request_rate=rate(approval_tasks, len(executed)),
        mean_wall_latency_ms=_mean(latencies),
        median_wall_latency_ms=(statistics.median(latencies) if latencies else None),
        p95_wall_latency_ms=percentile_nearest_rank(latencies, 0.95),
        category_results=categories,
    )


def replan_ablation(
    full: Sequence[BenchmarkRunRecord], no_replan: Sequence[BenchmarkRunRecord]
) -> dict[str, object]:
    """按相同 task IDs 比较 Full 与 max_replans=0；不拼接不同题集。"""

    full_by_id = {item.task_id: item for item in full}
    no_replan_by_id = {item.task_id: item for item in no_replan}
    if set(full_by_id) != set(no_replan_by_id):
        raise ValueError("Full/No-Replan task IDs 必须完全一致")
    task_ids = sorted(full_by_id)
    full_successes = sum(full_by_id[item].success for item in task_ids)
    no_replan_successes = sum(no_replan_by_id[item].success for item in task_ids)
    total = len(task_ids)
    full_rate = full_successes / total if total else None
    no_replan_rate = no_replan_successes / total if total else None
    return {
        "task_ids": task_ids,
        "full_success": rate(full_successes, total).model_dump(mode="json"),
        "no_replan_success": rate(no_replan_successes, total).model_dump(mode="json"),
        "delta_percentage_points": (
            (full_rate - no_replan_rate) * 100
            if full_rate is not None and no_replan_rate is not None
            else None
        ),
    }


def stability_summary(records: Sequence[BenchmarkRunRecord]) -> dict[str, object]:
    """固定三轮 executed attempt 的逐题与 subset 稳定性统计。"""

    grouped: dict[str, list[BenchmarkRunRecord]] = defaultdict(list)
    for record in records:
        if record.disposition is RunDisposition.EXECUTED:
            grouped[record.task_id].append(record)
    if any(
        len(items) != 3 or {item.attempt for item in items} != {1, 2, 3}
        for items in grouped.values()
    ):
        raise ValueError("stability subset 每题必须恰好包含 attempt 1/2/3")
    variances: dict[str, float] = {}
    success_counts: dict[str, int] = {}
    all_three_success = 0
    instability_count = 0
    for task_id, items in sorted(grouped.items()):
        values = [int(item.success) for item in items]
        success_count = sum(values)
        success_counts[task_id] = success_count
        all_three_success += success_count == 3
        instability_count += 0 < success_count < 3
        variances[task_id] = statistics.pvariance(values)
    attempt_success_rates = {
        str(attempt): _mean(
            int(item.success)
            for items in grouped.values()
            for item in items
            if item.attempt == attempt
        )
        for attempt in (1, 2, 3)
    }
    return {
        "tasks": len(grouped),
        "per_task_success_count": success_counts,
        "all_three_success_count": all_three_success,
        "instability_count": instability_count,
        "attempt_success_rates": attempt_success_rates,
        "subset_aggregate_success_variance": statistics.pvariance(
            [value for value in attempt_success_rates.values() if value is not None]
        ),
        "success_variance_by_task": variances,
        "mean_success_variance": _mean(variances.values()),
    }
