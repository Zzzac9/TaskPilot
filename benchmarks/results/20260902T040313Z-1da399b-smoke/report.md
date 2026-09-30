# TaskPilot Benchmark Report

## Environment

- Python: 3.11.15
- Git commit: 1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
- Git dirty: True
- Model provider: api.deepseek.com
- Model: deepseek-v4-flash

## Task Set and Denominator

- Tasks declared in this run: 3
- Eligible: 3
- NOT_RUN because capability unavailable: 0
- Executed: 3
- Succeeded: 1
- Failed: 2
- Invariant: S + F == Z is True

## Primary Results

- Task Success Rate: 1/3 (33.33333333333333%)
- 95% Wilson interval: [6.15%, 79.23%]
- False Completion: 1/3
- Average tool calls/task: 4.333333333333333
- Average tool calls/successful task: 2.0
- Tool failure rate: 0.0%
- Mean/median/P95 latency (ms): 19342.456333319813 / 20350.64829996554 / 23508.102699997835

## Category Results

- browser_dom: 0/1 (0.00%)
- constraint_aggregation: 0/1 (0.00%)
- mcp: 1/1 (100.00%)

## Failure Cases

- browser_table_03: verification_error — one or more independent checks failed
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

D:\Desktop\TaskPilot\benchmarks\results\20260902T040313Z-1da399b-smoke\runs.jsonl
