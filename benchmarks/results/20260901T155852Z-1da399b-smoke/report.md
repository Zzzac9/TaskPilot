# TaskPilot Benchmark Report

## Environment

- Python: 3.11.15
- Git commit: 1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
- Git dirty: True
- Model provider: api.deepseek.com
- Model: deepseek-v4-flash

## Task Set and Denominator

- 30 tasks declared: 3
- Eligible: 3
- NOT_RUN because capability unavailable: 0
- Executed: 3
- Succeeded: 0
- Failed: 3
- Invariant: S + F == Z is True

## Primary Results

- Task Success Rate: 0/3 (0.0%)
- 95% Wilson interval: [0.00%, 56.15%]
- False Completion: 0/3
- Average tool calls/task: 0.0
- Average tool calls/successful task: None
- Tool failure rate: None%
- Mean/median/P95 latency (ms): 1065.9610000050936 / 1285.877900023479 / 1338.0634000059217

## Category Results

- browser_dom: 0/1 (0.00%)
- constraint_aggregation: 0/1 (0.00%)
- mcp: 0/1 (0.00%)

## Failure Cases

- mcp_record_02: unknown — one or more independent checks failed
- browser_table_03: unknown — one or more independent checks failed
- aggregate_cross_source_01: unknown — one or more independent checks failed

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

D:\Desktop\TaskPilot\benchmarks\results\20260901T155852Z-1da399b-smoke\runs.jsonl
