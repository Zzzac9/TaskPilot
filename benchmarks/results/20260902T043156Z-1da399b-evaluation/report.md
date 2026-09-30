# TaskPilot Phase 12 Final Evaluation

## Source Runs

```json
{
  "primary": "20260902T040441Z-1da399b-primary",
  "no_replan": "20260902T041621Z-1da399b-no-replan",
  "stability_2": "20260902T041758Z-1da399b-stability-2",
  "stability_3": "20260902T042251Z-1da399b-stability-3",
  "reliability": "20260902T042822Z-1da399b-reliability"
}
```

## Primary

- declared / eligible / executed: 30 / 30 / 30
- succeeded / failed: 8 / 22
- Task Success Rate: 8/30 (26.67%)
- Wilson 95% CI: [14.18%, 44.45%]
- False Completion: 11/30
- Average Tool calls/task: 5.0
- Tool failure rate: 3.33%
- Replan recovery: 1/2

## Failure Analysis

- human_policy_reject: 1
- planning_error: 2
- verification_error: 13
- wrong_tool_selection: 6

## Replanning Ablation

```json
{
  "task_ids": [
    "replan_browser_02",
    "replan_cross_04",
    "replan_mcp_01",
    "replan_mcp_03"
  ],
  "full_success": {
    "numerator": 1,
    "denominator": 4,
    "percentage": 25.0,
    "confidence_95": null
  },
  "no_replan_success": {
    "numerator": 0,
    "denominator": 4,
    "percentage": 0.0,
    "confidence_95": null
  },
  "delta_percentage_points": 25.0
}
```

## Stability

```json
{
  "tasks": 10,
  "per_task_success_count": {
    "aggregate_cross_source_01": 0,
    "browser_table_03": 0,
    "form_fill_01": 0,
    "hitl_delete_reject_02": 0,
    "hitl_submit_01": 2,
    "mcp_record_02": 3,
    "replan_browser_02": 0,
    "replan_cross_04": 2,
    "replan_mcp_01": 0,
    "replan_mcp_03": 0
  },
  "all_three_success_count": 1,
  "instability_count": 2,
  "attempt_success_rates": {
    "1": 0.3,
    "2": 0.2,
    "3": 0.2
  },
  "subset_aggregate_success_variance": 0.0022222222222222214,
  "success_variance_by_task": {
    "aggregate_cross_source_01": 0,
    "browser_table_03": 0,
    "form_fill_01": 0,
    "hitl_delete_reject_02": 0,
    "hitl_submit_01": 0.2222222222222222,
    "mcp_record_02": 0,
    "replan_browser_02": 0,
    "replan_cross_04": 0.2222222222222222,
    "replan_mcp_01": 0,
    "replan_mcp_03": 0
  },
  "mean_success_variance": 0.04444444444444444
}
```

## Runtime Reliability

```json
{
  "cases_total": 9,
  "cases_passed": 9,
  "duplicate_unsafe_side_effects": 0,
  "terminal_journal_dedup_failures": 0,
  "recovery_interrupts_required": 2,
  "recovery_cases_resolved": 2,
  "browser_stale_runtime_executions": 0,
  "unauthorized_side_effects_before_approval": 0
}
```

## Boundaries

- Real external Vision model ablation: NOT RUN.
- Token reduction: not measured and not claimed.
- Localhost fixtures only; no public-web production benchmark.
- Runtime reliability cases do not prove universal exactly-once.
