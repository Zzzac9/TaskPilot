# TaskPilot Benchmark Report

## Environment

- Python: 3.11.15
- Git commit: 1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
- Git dirty: True
- Model provider: api.deepseek.com
- Model: deepseek-v4-flash

## Task Set and Denominator

- Tasks declared in this run: 4
- Eligible: 4
- NOT_RUN because capability unavailable: 0
- Executed: 4
- Succeeded: 0
- Failed: 4
- Invariant: S + F == Z is True

## Primary Results

- Task Success Rate: 0/4 (0.0%)
- 95% Wilson interval: [0.00%, 48.99%]
- False Completion: 3/4
- Average tool calls/task: 4.0
- Average tool calls/successful task: None
- Tool failure rate: 31.25%
- Mean/median/P95 latency (ms): 21324.55449999543 / 22689.908649976132 / 29121.25940003898

## Category Results

- replanning: 0/4 (0.00%)

## Failure Cases

- replan_mcp_01: unknown — one or more independent checks failed
- replan_browser_02: verification_error — one or more independent checks failed
- replan_mcp_03: verification_error — one or more independent checks failed
- replan_cross_04: verification_error — one or more independent checks failed

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

D:\Desktop\TaskPilot\benchmarks\results\20260902T041621Z-1da399b-no-replan\runs.jsonl
