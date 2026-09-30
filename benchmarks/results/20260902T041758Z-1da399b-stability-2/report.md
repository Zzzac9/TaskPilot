# TaskPilot Benchmark Report

## Environment

- Python: 3.11.15
- Git commit: 1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
- Git dirty: True
- Model provider: api.deepseek.com
- Model: deepseek-v4-flash

## Task Set and Denominator

- Tasks declared in this run: 10
- Eligible: 10
- NOT_RUN because capability unavailable: 0
- Executed: 10
- Succeeded: 2
- Failed: 8
- Invariant: S + F == Z is True

## Primary Results

- Task Success Rate: 2/10 (20.0%)
- 95% Wilson interval: [5.67%, 50.98%]
- False Completion: 3/10
- Average tool calls/task: 8.2
- Average tool calls/successful task: 3.0
- Tool failure rate: 4.878048780487805%
- Mean/median/P95 latency (ms): 28077.822440001182 / 21657.776899985038 / 84225.6659999839

## Category Results

- browser_dom: 0/1 (0.00%)
- constraint_aggregation: 0/1 (0.00%)
- form_interaction: 0/1 (0.00%)
- hitl: 0/2 (0.00%)
- mcp: 1/1 (100.00%)
- replanning: 1/4 (25.00%)

## Failure Cases

- browser_table_03: verification_error — one or more independent checks failed
- form_fill_01: unknown — one or more independent checks failed
- aggregate_cross_source_01: unknown — one or more independent checks failed
- replan_mcp_01: unknown — one or more independent checks failed
- replan_browser_02: verification_error — one or more independent checks failed
- replan_mcp_03: verification_error — one or more independent checks failed
- hitl_submit_01: environment_error — Recursion limit of 100 reached without hitting a stop condition. You can increase the limit by setting the `recursion_limit` config key.
For troubleshooting, visit: https://python.langchain.com/docs/troubleshooting/errors/GRAPH_RECURSION_LIMIT
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

D:\Desktop\TaskPilot\benchmarks\results\20260902T041758Z-1da399b-stability-2\runs.jsonl
