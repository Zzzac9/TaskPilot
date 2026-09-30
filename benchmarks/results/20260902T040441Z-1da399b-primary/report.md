# TaskPilot Benchmark Report

## Environment

- Python: 3.11.15
- Git commit: 1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
- Git dirty: True
- Model provider: api.deepseek.com
- Model: deepseek-v4-flash

## Task Set and Denominator

- Tasks declared in this run: 30
- Eligible: 30
- NOT_RUN because capability unavailable: 0
- Executed: 30
- Succeeded: 8
- Failed: 22
- Invariant: S + F == Z is True

## Primary Results

- Task Success Rate: 8/30 (26.666666666666668%)
- 95% Wilson interval: [14.18%, 44.45%]
- False Completion: 11/30
- Average tool calls/task: 5.0
- Average tool calls/successful task: 6.75
- Tool failure rate: 3.3333333333333335%
- Mean/median/P95 latency (ms): 22420.04686333239 / 20540.662300016265 / 55592.28109999094

## Category Results

- browser_dom: 1/8 (12.50%)
- constraint_aggregation: 0/4 (0.00%)
- form_interaction: 2/4 (50.00%)
- hitl: 3/4 (75.00%)
- mcp: 1/6 (16.67%)
- replanning: 1/4 (25.00%)

## Failure Cases

- mcp_candidates_01: verification_error — one or more independent checks failed
- mcp_calculate_03: verification_error — one or more independent checks failed
- mcp_stock_04: unknown — one or more independent checks failed
- mcp_rank_05: unknown — one or more independent checks failed
- mcp_total_06: unknown — one or more independent checks failed
- browser_list_01: verification_error — one or more independent checks failed
- browser_filter_02: verification_error — one or more independent checks failed
- browser_table_03: unknown — one or more independent checks failed
- browser_pagination_04: verification_error — one or more independent checks failed
- browser_delayed_05: unknown — one or more independent checks failed
- browser_iframe_07: verification_error — one or more independent checks failed
- browser_rank_08: verification_error — one or more independent checks failed
- form_fill_01: verification_error — one or more independent checks failed
- form_select_02: unknown — one or more independent checks failed
- aggregate_cross_source_01: unknown — one or more independent checks failed
- aggregate_inventory_02: unknown — one or more independent checks failed
- aggregate_budget_03: unknown — one or more independent checks failed
- aggregate_multi_page_04: verification_error — one or more independent checks failed
- replan_mcp_01: unknown — one or more independent checks failed
- replan_browser_02: verification_error — one or more independent checks failed
- replan_mcp_03: verification_error — one or more independent checks failed
- hitl_delete_reject_02: unknown — all independent checks passed

## Replanning Evaluation

NOT RUN

## Vision Evaluation

Real external vision-model ablation = NOT RUN unless a separate vision run artifact is referenced.
Deterministic FakeVisionTargeter reliability cases are never reported as model success improvement.

## Runtime Reliability and HITL Safety

```json
NOT RUN
```

## Context Measurement

Only character-context reduction may be reported. No tokenizer-based reduction is claimed.

## Stability Subset

NOT RUN

## Limitations

- Localhost fixtures only; no production public-web benchmark.
- External oracle is distinct from TaskPilot Verifier.
- Trace is audit data and is not assumed exactly-once.
- Token usage remains null when provider usage metadata is unavailable.

## Raw Results Location

D:\Desktop\TaskPilot\benchmarks\results\20260902T040441Z-1da399b-primary\runs.jsonl
