# Phase 12 Review — Benchmark + Evaluation + Resume Freeze

> Status: **FINAL ACCEPTANCE COMPLETE**  
> Sections 1–40 preserve the earlier credential-blocked and recursion-blocked history. Section
> 41 onward records the authorized production repair, fresh smoke, official Primary, ablation,
> stability, final reliability run and superseding acceptance evidence.

## 1. Phase Goal

Phase 12 的唯一目标是建立可复现评价层，使用真实 LLM、真实 TaskPilot 生产组件、
真实 localhost Browser 和 MCP STDIO subprocess 完成 30 题 E2E Benchmark，并用独立
external oracle 判断结果；同时独立执行 Runtime Reliability/Safety fault injection，最终
只从 raw results 冻结 README、简历数字和面试讲法。

当前实际完成：

- 专用 clean benchmark venv 与 dependency gate；
- 固定 30 题 manifest；
- localhost fixture site 与真实 MCP STDIO server；
- Benchmark models、loader、runner、external oracle、metrics、reporting；
- 9-case Runtime Reliability/Safety suite；
- 19 项 Phase 12 harness 测试；
- clean-env Phase 1–12 全量回归。

当前没有完成：

- Real LLM 3-task smoke；
- Primary 30-task run；
- 10-task stability 的额外两轮；
- Full vs No-Replan 真实 ablation；
- 真实外部 Vision model ablation；
- 最终 Task Success Rate、latency 或 False Completion 数字；
- `docs/resume/taskpilot_resume.md`；
- `docs/interview/taskpilot_interview.md`；
- Final README freeze。

阻塞原因不是测试失败，而是当前进程没有 `LLM_API_KEY` 和 `LLM_MODEL`。Runner 在启动
任何 Primary task 前 fail closed，符合 Stop Condition。

## 2. Clean Benchmark Environment

实际创建：

```text
D:\Desktop\TaskPilot\.venv-benchmark
```

创建与安装命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m venv .venv-benchmark
.\.venv-benchmark\Scripts\python.exe -m pip install --upgrade pip
.\.venv-benchmark\Scripts\python.exe -m pip install -e .
.\.venv-benchmark\Scripts\python.exe -m playwright install chromium
```

环境使用 Python 3.11.15。没有清理或修改共享 `D:\Anaconda\envs\ai` 中其他项目包。
`.venv-benchmark/` 已加入 `.gitignore`，raw benchmark results 没有被忽略。

## 3. pip check

实际命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m pip check
```

真实输出：

```text
No broken requirements found.
```

最终 reliability raw run 同时保存：

- `pip-check.txt`
- `pip-freeze.txt`
- `environment.json`

## 4. Dependency Versions

来自最终 raw environment snapshot：

| Component | Version |
|---|---:|
| Python | 3.11.15 |
| TaskPilot | 0.1.0 editable |
| Playwright | 1.62.0 |
| Chromium | 151.0.7922.34 |
| LangGraph | 0.2.76 |
| LangChain Core | 0.3.86 |
| MCP | 2.1.1 |
| FastAPI | 0.141.1 |
| Starlette | 1.6.0 |
| Pydantic | 2.13.5 |

Git commit recorded as `1da399ba2ac0bde44f271557a0c5dfeac0c5dceb` and
`git_dirty=true`; the environment snapshot does not pretend the worktree is clean.

## 5. Benchmark Architecture

```text
Fixed manifest
→ capability gate
→ localhost fixture + real MCP STDIO subprocess
→ TaskPilotService production component path
→ scripted Human response only when Graph interrupts
→ durable TaskState + Action Journal + Trace
→ independent external Oracle
→ immutable BenchmarkRunRecord JSONL
→ deterministic aggregate metrics/report
```

Benchmark code lives under `benchmarks/taskpilot_bench/`, not `src/taskpilot/`. Production HITL
was not changed to auto-approve; scripted decisions are Host-side benchmark input only.

## 6. 30-task Manifest

固定文件：`benchmarks/manifest.json`。

IDs：

```text
mcp_candidates_01, mcp_record_02, mcp_calculate_03, mcp_stock_04,
mcp_rank_05, mcp_total_06,
browser_list_01, browser_filter_02, browser_table_03, browser_pagination_04,
browser_delayed_05, browser_popup_06, browser_iframe_07, browser_rank_08,
form_fill_01, form_select_02, form_submit_03, form_no_submit_04,
aggregate_cross_source_01, aggregate_inventory_02, aggregate_budget_03,
aggregate_multi_page_04,
replan_mcp_01, replan_browser_02, replan_mcp_03, replan_cross_04,
hitl_submit_01, hitl_delete_reject_02, hitl_mcp_write_03, hitl_mcp_reject_04
```

Loader 强制：总数必须是 30、ID 唯一、分类分布固定、Primary denominator 不得混入
`requires_vision=true`。运行过程中不会动态修改题目。

## 7. Categories

| Category | Declared |
|---|---:|
| MCP | 6 |
| Browser DOM | 8 |
| Form Interaction | 4 |
| Constraint Aggregation | 4 |
| Replanning | 4 |
| HITL | 4 |
| Total | 30 |

聚合器会分别输出 category success/total，不允许 overall rate 隐藏困难分类。

## 8. External Oracle

`benchmarks/taskpilot_bench/oracle.py` 实现独立 oracle：

- `state_contains`：检查 verified StepResult/task output 与固定 ground truth；
- `fixture_state`：通过 localhost `/fixture-state` 读取真实 submit/delete/vision side effect；
- `file_content`：在显式 allowed root 内读取真实文件。

Oracle 不读取或信任 TaskPilot 的 `VerificationResult.completed`。测试明确构造内部
Verifier=true、外部 ground truth 错误的 state，并得到 oracle fail。

## 9. Success Definition

代码强制：

```text
success = task_status == "completed" AND oracle_passed is True
```

False Completion：

```text
task_status == "completed" AND oracle_passed is False
```

Pydantic validator 拒绝不符合这两个定义的 raw record。Task runtime/model/tool/timeout
失败一旦启动就保持 `EXECUTED` 且计入 denominator；只有运行前缺 capability 才能 NOT_RUN。

## 10. Raw Result Schema

`BenchmarkRunRecord` 包含：run/task/category/attempt、disposition、task status、oracle、
success、error/failure category、wall latency、Tool/LLM/Replan/Step rejection/Approval/
Recovery/Trace counts、plan revision、false completion、timestamp，以及 nullable token usage。

Provider 没有真实 usage 时写 `null`，不会写 0 冒充。`runs.jsonl` 使用 exclusive create，
旧 run 不会被覆盖；JSONL round-trip 已测试。

## 11. Real LLM Smoke

预定固定三题：

- MCP: `mcp_record_02`
- Browser: `browser_table_03`
- Multi-step MCP + Browser: `aggregate_cross_source_01`

实际命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner smoke
```

真实输出：

```text
BENCHMARK_STOP=Real LLM smoke gate blocked; missing environment variables: LLM_API_KEY, LLM_MODEL
EXIT_CODE=2
```

没有输出或记录 key；没有启动任何 task。**Real LLM smoke = NOT RUN / BLOCKED**。

## 12. Primary Run

**NOT RUN.** 由于 Real LLM smoke 未通过 pre-run capability gate，30 题没有启动，当前
不存在 Primary Task Success Rate。pytest 数量和 reliability case 结果均未被冒充为 Agent
Task Success Rate。

Denominator 当前只能写：

```text
30 tasks declared
0 actually started
0 succeeded
0 failed after start
Primary run = NOT RUN
```

不能据此计算或展示 0%/100%。

## 13. Category Metrics

**NOT RUN.** Aggregation code 和测试已完成，但没有真实 Primary raw runs，因此不生成
category success 数字。

## 14. False Completion

**NOT RUN for Primary.** 定义、validator、aggregation 和离线测试已完成；没有真实 LLM
run，不能生成 False Completion Rate。

## 15. Dynamic Replanning Ablation

固定 dead-end subset 已声明 4 题，runner 支持 Full `max_replans=2` 与 No-Replan
`max_replans=0`，并强制两组 task IDs 完全相同。由于 Real LLM gate blocked，两组均
**NOT RUN**，没有写任何“提升 X%”数字。

## 16. Vision Ablation

**Real external vision-model ablation = NOT RUN.** 当前没有 Vision credential/provider。
FakeVisionTargeter 只用于 Runtime Reliability，不用于证明真实 Vision success improvement。

## 17. Runtime Reliability Suite

最终独立 raw run：

```text
benchmarks/results/20260901T154610Z-1da399b-reliability/
```

9 个实际 pytest subprocess case：

1. terminal journal committed 后 graph checkpoint crash；
2. unsafe side effect 后 crash + Human abort；
3. one-shot Human retry 后再次 crash；
4. durable non-browser approval；
5. same-Service Browser live runtime；
6. Service recreation Browser fail closed；
7. Vision approval boundary with real Chromium；
8. context core overflow no silent truncation；
9. Replan fresh identity/history preservation。

每个 case 保存命令、exit code 和 pytest 原始 stdout。该 suite 明确不计入 Primary Task
Success Rate。

## 18. Crash Recovery Metrics

来自 `reliability-summary.json`：

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

允许的严格表述是：“在本 run 的 9 个预定义 fault-injection/runtime mechanism cases 中，
9 个 case 通过其外部断言，未观察到重复 unsafe side effect。”不能推广为 universal
exactly-once。

## 19. HITL Safety Metrics

该 reliability run 中受控 approval-boundary cases 的：

```text
Unauthorized side effects before approval = 0
Browser stale-runtime executions = 0
```

覆盖 Browser submit、Vision click、durable high-risk action 和 recreated-Service fail-closed
边界。Primary 4 个 HITL LLM tasks 尚未运行。

## 20. Context Character Measurement

Context core overflow / no silent truncation mechanism case 已通过。Primary context-stress run
尚未执行，因此没有生成 character-context reduction 数字，更没有声称 token reduction。

## 21. Latency Metrics

**Primary mean/median/P95 = NOT RUN.** Nearest-rank P95、mean、median 的 deterministic
代码与测试已完成，但没有真实 Primary latency raw data。

## 22. Stability Subset

固定 stability tags 已放在 10 个 manifest task 上，aggregation 要求每题至少 3 个 executed
attempt。由于 Primary run 未执行，额外两轮也 **NOT RUN**，没有 cherry-pick。

## 23. Failure Analysis

当前没有 Primary task failure 可分类，因为 task 尚未启动。唯一阻塞分类：

```text
pre_run_capability_error:
  missing LLM_API_KEY
  missing LLM_MODEL
```

这不是 skipped-after-failure，也不进入 Primary denominator。

## 24. Raw Result Paths

最终 authoritative reliability run：

```text
benchmarks/results/20260901T154610Z-1da399b-reliability/
├── config.json
├── environment.json
├── pip-check.txt
├── pip-freeze.txt
├── reliability-runs.json
├── reliability-summary.json
├── report.md
└── tasks.json
```

早期 harness 自检 run 目录也未覆盖或删除：

```text
benchmarks/results/20260901T153503Z-1da399b-reliability/
benchmarks/results/20260901T153717Z-1da399b-reliability/
benchmarks/results/20260901T153952Z-1da399b-reliability/
```

正式 Primary artifact 不存在，因为 pre-run credential gate 先于 run directory 创建。

## 25. Verified Claims Table

| Claim | Source metric | run_id | Raw file | Calculation |
|---|---|---|---|---|
| Clean dedicated environment | pip check exit 0 | 20260901T154610Z-1da399b-reliability | `pip-check.txt` | exact command output |
| 9/9 predefined runtime cases passed | per-case `passed` / total | same | `reliability-runs.json` | sum(passed) / 9 |
| 0 duplicate unsafe side effects observed | duplicate counter | same | same | sum per-case external assertions |
| 0 stale Browser executions observed | stale runtime counter | same | same | sum fail-closed case assertions |
| 0 unauthorized preapproval effects observed | HITL counter | same | same | sum approval-boundary case assertions |

没有 Task Success Rate、Replan uplift、real Vision uplift、Primary P95 或 token reduction claim。

## 26. Final Resume Version

**NOT GENERATED.** `docs/resume/taskpilot_resume.md` 只有正式 Primary、ablation 和完整
Verified Claims Table 完成后才能生成。当前生成会违反数字追溯规则。

## 27. Final Interview Version

**NOT GENERATED.** `docs/interview/taskpilot_interview.md` 与 Final Resume 同属正式
Benchmark 后的 freeze artifact。

## 28. README Freeze

Root README **未做 Final Freeze**。只更新了 `benchmarks/README.md`，说明 harness、命令、
指标定义和当前 blocked gate。没有在 Root README 声称 Primary 已完成。

## 29. Full pytest

专用 clean benchmark venv 实际命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m pytest -q
```

真实输出：

```text
........................................................................ [ 28%]
..........................................................s............. [ 56%]
........................................................................ [ 85%]
.....................................                                    [100%]
============================== warnings summary ===============================
.venv-benchmark\Lib\site-packages\langgraph\checkpoint\base\__init__.py:18
  LangChainPendingDeprecationWarning: The default value of allowed_objects will change in a future version.

252 passed, 1 skipped, 1 warning in 37.21s
```

结果：passed=252，skipped=1，failed=0，errors=0。唯一 skip 仍是显式 opt-in 的真实
external Vision provider integration。

新增 Phase 12 定向测试：

```text
19 passed, 1 warning in 3.73s
```

覆盖 success/false completion/denominator/NOT_RUN/runtime failure/Wilson/category/replan
recovery/latency/JSONL/secret/environment/external oracle/stale Browser/duplicate side effect/
ablation，以及真实 benchmark MCP STDIO 和 localhost fixture oracle。

## 30. Static Checks

实际命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m ruff check benchmarks tests/test_phase12_benchmark.py src/taskpilot/llm.py
D:\Anaconda\envs\ai\python.exe -m compileall -q src benchmarks tests
git diff --check
```

真实结果：Ruff `All checks passed!`；compileall exit 0、无输出；`git diff --check`
只有 Windows LF→CRLF notices，没有 whitespace error。

## 31. Final Known Limitations

- Primary Real LLM Benchmark 尚未运行；Phase 12 不能最终验收。
- single active runner assumption；
- API no auth/authz；
- no distributed worker；
- no cross-process live Browser restoration；
- Vision only click fallback；
- no full prompt-injection defense；
- localhost fixtures only，无 production public-web benchmark；
- MCP Primary 只计划真实 STDIO；HTTP transport 不在本 Benchmark 主覆盖；
- Trace 非 distributed 且不保证 exactly-once；
- Context 是 character budget，不是 exact tokenizer budget；
- reliability 9-case result 不能证明 universal exactly-once；
- external Vision model 未配置，Vision ablation NOT RUN。

## 32. git status / commit

Commit：

```text
1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
```

正式 environment metadata 已记录 `git_dirty=true`。完整最终 `git status --short` 见本文
末尾 “Git Status Snapshot”；Phase 3–11 的既有用户修改未删除、未覆盖。

## 33. Final Project Verdict

Phase 12 评价层代码、clean environment 和 Runtime Reliability/Safety evidence 已完成且
通过离线验收；真实 LLM Primary Benchmark 未满足 credential gate，因此：

```text
Phase 12 final acceptance = BLOCKED
TaskPilot feature development = no new feature phase started
Resume freeze = NOT GENERATED
Interview freeze = NOT GENERATED
Primary benchmark claims = NONE
```

### Final Acceptance Questions

- Q1 主 Task Success Rate 是否来自 pytest？**No；当前根本没有生成主成功率。**
- Q2 Primary 是否使用真实 LLM？**尚未运行；缺 credential，因此不能回答 Yes。**
- Q3 Success 是否只看 COMPLETED？**No，代码强制 external oracle。**
- Q4 是否有 independent external oracle？**Yes，已实现并离线/localhost 测试；Primary 尚未运行。**
- Q5 Verifier 是否等于 Benchmark Oracle？**No。**
- Q6 失败 task 是否从 denominator 删除？**No；已启动失败强制 EXECUTED。**
- Q7 Fake Vision 是否证明真实 Vision 提升？**No。**
- Q8 Replanning 提升是否来自真实 ablation？**没有写提升数字；ablation NOT RUN。**
- Q9 crash duplicate-side-effect 数字是否来自真实 fault injection？**Yes，来自上述 raw run。**
- Q10 shared ai 环境是否作为最终 benchmark 环境？**No。**
- Q11 final benchmark venv pip check 是否 clean？**Yes。**
- Q12 所有当前数字是否可追到 raw result？**Yes；最终简历尚未生成。**
- Q13 是否修改失败结果换漂亮数字？**No。**
- Q14 是否新增 Phase 13？**No。**
- Q15 Phase 12 完成后是否还有功能开发 Phase？**No；但 Phase 12 本身仍被 smoke gate 阻塞。**

## Final Directory Tree

```text
TaskPilot/
├── benchmarks/
│   ├── __init__.py
│   ├── README.md
│   ├── manifest.json
│   ├── fixtures/
│   │   ├── fixture_server.py
│   │   ├── mcp_server.py
│   │   └── site/
│   │       ├── canvas.html
│   │       ├── delayed.html
│   │       ├── details.html
│   │       ├── form.html
│   │       ├── frame.html
│   │       ├── index.html
│   │       ├── listings-page-2.html
│   │       ├── listings.html
│   │       ├── source-a.html
│   │       ├── source-b.html
│   │       └── table.html
│   ├── taskpilot_bench/
│   │   ├── __init__.py
│   │   ├── environment.py
│   │   ├── loader.py
│   │   ├── metrics.py
│   │   ├── models.py
│   │   ├── oracle.py
│   │   ├── reliability.py
│   │   ├── reporting.py
│   │   └── runner.py
│   └── results/
│       └── <immutable run directories>/
├── docs/
│   └── review/
│       ├── phase_03.md ... phase_11.md
│       └── phase_12.md
├── src/taskpilot/
│   └── <Phase 1–11 production runtime>
└── tests/
    ├── test_phase12_benchmark.py
    └── <Phase 1–11 tests>
```

## Files Changed in the Blocked Phase 12 Work

新建：

- `benchmarks/__init__.py`
- `benchmarks/manifest.json`
- `benchmarks/taskpilot_bench/{__init__,models,loader,runner,oracle,metrics,reporting,reliability,environment}.py`
- `benchmarks/fixtures/mcp_server.py`
- `benchmarks/fixtures/fixture_server.py`
- `benchmarks/fixtures/site/*.html`
- `benchmarks/results/<run_id>/...`
- `tests/test_phase12_benchmark.py`
- `docs/review/phase_12.md`

修改：

- `benchmarks/README.md`：从 pending 更新为真实 harness/gate 使用说明；
- `.gitignore`：忽略专用 venv，但保留 raw results 可追踪；
- `src/taskpilot/llm.py`：真实模型统一显式 `temperature=0`。

没有创建 `docs/resume/` 或 `docs/interview/`，因为 Stop Condition 禁止提前冻结。

## Git Status Snapshot

实际命令：

```powershell
git status --short
```

实际输出：

```text
 M .env.example
 M .gitignore
 M README.md
 M benchmarks/README.md
 M docs/review/phase_03.md
 M pyproject.toml
 M src/taskpilot/cli.py
 M src/taskpilot/graph.py
 M src/taskpilot/llm.py
 M src/taskpilot/models.py
 M src/taskpilot/planner.py
 M src/taskpilot/state.py
 M tests/test_analyzer.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
 M tests/test_planner.py
?? benchmarks/__init__.py
?? benchmarks/fixtures/
?? benchmarks/manifest.json
?? benchmarks/results/
?? benchmarks/taskpilot_bench/
?? docs/review/phase_04.md
?? docs/review/phase_05.md
?? docs/review/phase_06.md
?? docs/review/phase_07.md
?? docs/review/phase_08.md
?? docs/review/phase_09.md
?? docs/review/phase_10.md
?? docs/review/phase_11.md
?? docs/review/phase_12.md
?? src/taskpilot/api/
?? src/taskpilot/browser/
?? src/taskpilot/context/
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/persistence/
?? src/taskpilot/policy/
?? src/taskpilot/replanner.py
?? src/taskpilot/service.py
?? src/taskpilot/tools/
?? src/taskpilot/trace/
?? src/taskpilot/verifier.py
?? src/taskpilot/vision/
?? tests/conftest.py
?? tests/fixtures/
?? tests/test_browser.py
?? tests/test_browser_executor.py
?? tests/test_browser_policy.py
?? tests/test_context.py
?? tests/test_executor.py
?? tests/test_hitl.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase10_graph.py
?? tests/test_phase10_trace_boundaries.py
?? tests/test_phase11_api.py
?? tests/test_phase11_vision.py
?? tests/test_phase12_benchmark.py
?? tests/test_phase5_graph.py
?? tests/test_phase9_mcp_resume.py
?? tests/test_phase9_persistence.py
?? tests/test_phase9_recovery.py
?? tests/test_policy.py
?? tests/test_replanner.py
?? tests/test_tools.py
?? tests/test_trace.py
?? tests/test_verifier.py
```

Git 额外输出两条无法读取用户级 `C:\Users\zc115\.config\git\ignore` 的 permission warning；
它不改变仓库 status 内容，未通过修改用户级 Git 配置绕过。

## 34. Credential Resolution and Compatibility Work

`.env` 中的真实 DeepSeek 配置已经由用户提供。Benchmark loader 只读取以下 allowlist：

```text
LLM_API_KEY
LLM_MODEL
LLM_BASE_URL (optional)
```

没有读取、打印或写入 API key。运行时唯一输出的配置元数据为：

```text
model=deepseek-v4-flash
base_host=api.deepseek.com
temperature=0
```

第一次进入真实模型链后，clean environment 中的 `langchain-openai` 默认使用
`json_schema` structured output，DeepSeek 返回：

```text
This response_format type is unavailable now
```

因此结构化 Analyzer / Planner / Replanner / Verifier 显式改用
`method="function_calling"`。随后 DeepSeek 报告：

```text
Thinking mode does not support this tool_choice
```

DeepSeek endpoint 默认启用 Thinking Mode，而当前 action-level Executor 并未实现
DeepSeek 专属的 `reasoning_content` 多轮回放。`create_chat_model()` 现在仅在
`LLM_BASE_URL` hostname 为 `deepseek.com` 或其子域时加入：

```python
extra_body={"thinking": {"type": "disabled"}}
```

非 DeepSeek OpenAI-compatible endpoint 不会收到该 provider-specific 参数。该修复使
结构化输出、Tool Calling 与固定 `temperature=0` 配置保持一致，没有升级或新增依赖。

## 35. Real LLM Smoke Attempts

所有 run directory 均为 exclusive create；旧 raw artifacts 没有覆盖或回写。

| Run ID | Outcome | Evidence |
|---|---|---|
| `20260901T155852Z-1da399b-smoke` | 3/3 在 Analyzer 失败 | DeepSeek 不接受默认 `json_schema` response format；每题 raw record 与 5 个 trace events 已保存 |
| `20260902T032822Z-1da399b-smoke` | 3/3 在 Analyzer 失败 | DeepSeek 默认 Thinking Mode 拒绝 structured-output `tool_choice`；每题 raw record 与 5 个 trace events 已保存 |
| `20260902T033133Z-1da399b-smoke` | MCP 题成功；两道复杂题触发 LangGraph recursion limit | 真实生产 MCP / Browser / Planner / Executor / Verifier 已启动；raw、SQLite state、Trace、summary、report 已保存 |

最终一次实际命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner smoke
```

实际控制台输出：

```text
model=deepseek-v4-flash
base_host=api.deepseek.com
temperature=0
[1/3] mcp_record_02
  status=completed oracle=True success=True
[2/3] browser_table_03
  status=failed oracle=False success=False
[3/3] aggregate_cross_source_01
  status=failed oracle=False success=False
run_dir=D:\Desktop\TaskPilot\benchmarks\results\20260902T033133Z-1da399b-smoke
```

最终 smoke raw summary：

```text
declared=3
eligible=3
not_run=0
executed=3
succeeded=1
failed=2
S + F == Z: 1 + 2 == 3
False Completion=0/3
```

成功的 `mcp_record_02` 使用真实生产组件完成 2 个 MCP tool calls，任务状态
`completed`，独立 oracle 通过，共记录 22 个 trace events。它不是 Fake 路径。

## 36. Smoke Gate Failure: Production Runtime Bug

`browser_table_03` 和 `aggregate_cross_source_01` 的直接异常均为：

```text
Recursion limit of 25 reached without hitting a stop condition.
```

只读检查对应隔离 Trace SQLite 后得到：

| Task | Trace events actually persisted | LLM actions | Tool started | Tool succeeded | Step verification events |
|---|---:|---:|---:|---:|---:|
| `browser_table_03` | 35 | 7 | 4 | 4 | 2 |
| `aggregate_cross_source_01` | 36 | 7 | 5 | 5 | 1 |

这说明 Graph 正在真实执行 action-level workflow，并非没有启动或没有 Trace。
`TaskPilotService._graph_config()` 当前只设置 `thread_id`，没有设置适合生产 action-level
Graph 的 `recursion_limit`；LangGraph 因而使用默认 25，在复杂任务正常推进期间终止。
现有 Phase 9/10 复杂 Graph 测试则显式使用 `recursion_limit=100`。

该缺陷位于生产 Runtime 配置，不是 benchmark harness、oracle、prompt 或 ground truth。
Phase 12 指令明确要求“如果 smoke 暴露生产 Runtime 真 bug：停止并报告”，因此本轮：

```text
Smoke pipeline gate = FAILED
Official Primary 30-task run = NOT RUN
No-Replan ablation = NOT RUN
Stability attempts 2/3 = NOT RUN
Final Resume freeze = NOT GENERATED
Final Interview freeze = NOT GENERATED
Final README freeze = NOT PERFORMED
```

没有通过提高 runner 自身的 recursion limit 绕过生产 Service，也没有修改 manifest、
oracle ground truth 或 task prompt。

## 37. Harness Observation Fix After the Blocked Smoke

最终 smoke 同时发现一个独立 harness 观测缺陷：`graph.ainvoke()` 抛异常时，
`run_new_task()` 没有把内部生成的 task_id 返回给 runner；虽然 Trace SQLite 已经保存事件，
当次 immutable raw record 的 `trace_event_count` 被写成 0。

runner 现在会在每题独立 Trace DB 中只读查询唯一 task_id，再聚合已有事件。若没有唯一
task_id，则仍 fail closed 为 0，不猜测、不合并其他任务。新增离线测试证明异常后 task_id
可以从 Trace DB 恢复。这个修复没有修改上述 immutable raw artifact；在生产 recursion
问题修复后，必须重新创建完整 3-task smoke run 才能重新评估 gate。

Failure report 的 task-set 文案也从硬编码 `30 tasks declared` 改为
`Tasks declared in this run`，避免 3-task smoke 报告产生误导。

## 38. Reliability Metric Rename Status

代码、aggregation、tests 与报告 schema 已把覆盖全部 runtime mechanism cases 的：

```text
cases_safely_recovered
```

改名为：

```text
cases_passed
```

并保留 recovery-specific metrics：

- `recovery_interrupts_required`
- `recovery_cases_resolved`
- `terminal_journal_dedup_failures`
- `duplicate_unsafe_side_effects`

旧 reliability raw artifact 属于不可变历史，仍保留旧字段。由于当前 smoke gate 被生产
Runtime bug 阻塞，修名后的正式 reliability artifact 尚未重新生成，因此本轮不把旧 artifact
表述为“最终 9 个 crash recovery cases”。准确状态是：旧运行中的 9 个预定义 Runtime /
fault-injection mechanism cases 通过；新 schema 的 final artifact pending。

## 39. Superseding Test and Static-Check Evidence

Phase 12 定向测试实际命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m pytest tests/test_phase12_benchmark.py -q
```

实际结果：

```text
.......................                                                  [100%]
23 passed, 1 warning in 3.53s
```

完整 clean-env 回归实际命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m pytest -q
```

实际输出：

```text
........................................................................ [ 28%]
..........................................................s............. [ 56%]
........................................................................ [ 84%]
.........................................                                [100%]
============================== warnings summary ===============================
.venv-benchmark\Lib\site-packages\langgraph\checkpoint\base\__init__.py:18
  LangChainPendingDeprecationWarning: The default value of allowed_objects will change in a future version.

256 passed, 1 skipped, 1 warning in 35.47s
```

结果：passed=256，skipped=1，failed=0，errors=0。唯一 skip 仍为显式 opt-in 的真实
external Vision provider integration。

其他实际检查：

```text
pip check: No broken requirements found.
Ruff: All checks passed!
compileall: exit 0, no output
git diff --check: exit 0; only LF→CRLF notices, no whitespace error
```

测试命令仅为本次进程设置新的 `.tmp/phase12-*` TEMP/TMP 根目录，pytest 仍自行创建唯一
run directory；没有恢复固定 `--basetemp`，也没有修改 `pyproject.toml` 或 `conftest.py`
来固定 pytest 临时目录。

## 40. Current Phase Verdict

```text
Credential gate = RESOLVED
DeepSeek structured output compatibility = VERIFIED BY REAL MCP E2E
Benchmark harness tests = PASS
Clean full regression = PASS
Production action-level recursion configuration = BUG FOUND, NOT MODIFIED
Phase 12 final acceptance = BLOCKED
Phase 13 = NOT STARTED
```

下一步需要单独授权修复生产 `TaskPilotService` 的 action-level Graph recursion 配置并补充
Service-level 回归；修复后应从新的 3-task smoke 开始，不能复用或拼接本次 1/3 结果。

## 41. Production Recursion Limit Repair

### 41.1 Root Cause

旧的 `TaskPilotService._graph_config(task_id)` 只有：

```python
{"configurable": {"thread_id": task_id}}
```

action-level Graph 每个 Tool action 会经过 `decide_action → assess_policy →
execute_tool_action → decide_action`，并叠加 analyze、plan、begin、verify、advance、replan
等节点。LangGraph 默认 `recursion_limit=25` 因而会在合法复杂任务正常推进时触发。

### 41.2 Host Configuration

`TaskPilotService.__init__` 新增：

```python
graph_recursion_limit: int = 100
```

值保存为 `self.graph_recursion_limit`；小于 1 会立刻抛出包含字段名的 `ValueError`。它是
Host 确定的配置，不读取模型输出，也不会在失败后动态扩大。

所有生产入口继续调用同一个 `_graph_config()`，最终结构为：

```python
{
    "configurable": {"thread_id": task_id},
    "recursion_limit": self.graph_recursion_limit,
}
```

`recursion_limit` 位于顶层。`run_new_task`、`resume_task`、`continue_task`、CLI、FastAPI
和 Benchmark through `TaskPilotService` 因而使用相同语义；runner 没有单独覆盖 limit。

### 41.3 Safety Boundary

```text
Graph recursion limit != Agent semantic retry limit
```

本次没有改动 `TaskExecutor.max_actions_per_step`、`max_step_attempts`、`max_replans`、
Action Journal、recovery policy、Human approval 或 Verifier retry bound。100 只是最外围
superstep safety fuse。

## 42. Service-level Regression Evidence

新增 deterministic Service 测试使用：

```text
TaskPilotService
→ build_graph
→ action-level production graph
→ 2 个依赖步骤
→ 每个步骤 4 个 replay-safe local Tool actions
→ FINISH_STEP
```

在 `graph_recursion_limit=100` 下，任务最终 `COMPLETED`，8 个外部 Tool calls 的值依次为：

```text
11, 12, 13, 14, 21, 22, 23, 24
```

相同 deterministic task 显式使用 `graph_recursion_limit=25` 时稳定抛出
`GraphRecursionError`。这证明测试路径确实需要超过 25 个 Graph transitions，而不是只
断言配置字典。

同时覆盖：

- 默认值为 100；
- 自定义 60 实际进入顶层 Graph config；
- 0 和 -1 fail fast；
- `_graph_config` 同时保留 `configurable.thread_id`；
- Phase 11 checkpoint-only GET 与 Browser live-runtime 旧测试全部继续通过。

Focused 命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m pytest tests/test_phase11_api.py tests/test_phase12_benchmark.py -q
```

真实结果：

```text
....................................................                     [100%]
52 passed, 1 warning in 10.10s
```

修复后首次完整回归：

```text
261 passed, 1 skipped, 1 warning in 35.37s
```

## 43. Fresh Real LLM Smoke After Repair

没有复用或拼接 `20260902T033133Z-1da399b-smoke`。完整三题重新运行：

```powershell
.\.venv-benchmark\Scripts\python.exe -m benchmarks.taskpilot_bench.runner smoke
```

新 run ID：

```text
20260902T040313Z-1da399b-smoke
```

真实控制台结果：

```text
mcp_record_02                status=completed oracle=True  success=True
browser_table_03             status=completed oracle=False success=False
aggregate_cross_source_01    status=failed    oracle=False success=False
```

Smoke denominator 为 3/3 executed、1 success、2 failures、False Completion 1/3。三题都：

- 使用真实 DeepSeek 与生产 Analyzer/Planner/Executor/Verifier/Replanner；
- 正常启动并持久化 raw record；
- 产生 Trace；
- external oracle 正常执行；
- 没有 harness exception；
- 没有 GraphRecursionError 或其他 Runtime infrastructure crash。

Browser failure 是 `COMPLETED + oracle false`；Aggregation failure 是模型单轮返回多个
Tool Calls、违反单动作 Executor protocol。它们是 Agent quality failures，不阻塞 smoke
pipeline。结论：`Smoke infrastructure gate = PASS`，允许进入 Primary。

## 44. Official Primary Run

Official run ID：

```text
20260902T040441Z-1da399b-primary
```

source raw：

```text
benchmarks/results/20260902T040441Z-1da399b-primary/runs.jsonl
```

Denominator：

```text
declared=30
eligible=30
not_run=0
executed=30
succeeded=8
failed=22
8 + 22 == 30
```

Primary metrics：

| Metric | Result |
|---|---:|
| Task Success Rate | 8/30 = 26.67% |
| Wilson 95% CI | 14.18%–44.45% |
| False Completion | 11/30 = 36.67% |
| Average Tool calls / task | 5.0 |
| Average Tool calls / successful task | 6.75 |
| Tool failure rate | 5/150 = 3.33% |
| Average replans / task | 0.1 |
| Replan trigger rate | 2/30 = 6.67% |
| Replan recovery rate | 1/2 = 50% |
| Step rejection rate | 5/30 = 16.67% |
| Approval request rate | 8/30 = 26.67% |
| Mean latency | 22420.05 ms |
| Median latency | 20540.66 ms |
| P95 latency | 55592.28 ms |

Category success：

| Category | Success |
|---|---:|
| MCP | 1/6 = 16.67% |
| Browser DOM | 1/8 = 12.50% |
| Form Interaction | 2/4 = 50.00% |
| Constraint Aggregation | 0/4 = 0.00% |
| Replanning | 1/4 = 25.00% |
| HITL | 3/4 = 75.00% |

Success 仍严格为 `TaskStatus.COMPLETED AND external_oracle.passed`；11 个内部 completed
但 oracle false 的任务没有被计为成功。

## 45. Failure Analysis

失败归因来自 Primary raw state/error/Trace 的只读分析，不修改 `success`、
`oracle_passed` 或 `task_status`：

| Category | Count |
|---|---:|
| verification_error | 13 |
| wrong_tool_selection | 6 |
| planning_error | 2 |
| human_policy_reject | 1 |
| Total | 22 |

未观测到归为 timeout、environment_error、context_error 或 browser_locator_error 的 Primary
失败。逐题映射保存在 final `evaluation-summary.json`。

## 46. Dynamic Replanning Ablation

No-Replan run ID：

```text
20260902T041621Z-1da399b-no-replan
```

固定同一 4 题、同一模型、同一 prompt、同一 fixture：

```text
Full max_replans=2: 1/4 = 25%
No-Replan max_replans=0: 0/4 = 0%
delta: +25 percentage points
```

该结果只适用于 4 个预定义 dead-end tasks，不外推为普遍提升。Primary 中 accepted replan
分母为 2，其中 1 个最终 oracle success，因此 Replan Recovery Rate 为 1/2，而不是伪造
100%。

## 47. Stability

Source attempts：

- attempt 1：Primary `20260902T040441Z-1da399b-primary` 中冻结的 10 题；
- attempt 2：`20260902T041758Z-1da399b-stability-2`；
- attempt 3：`20260902T042251Z-1da399b-stability-3`。

每题恰好有 executed attempt 1/2/3。逐题成功次数：

| Task | Success / 3 |
|---|---:|
| aggregate_cross_source_01 | 0/3 |
| browser_table_03 | 0/3 |
| form_fill_01 | 0/3 |
| hitl_delete_reject_02 | 0/3 |
| hitl_submit_01 | 2/3 |
| mcp_record_02 | 3/3 |
| replan_browser_02 | 0/3 |
| replan_cross_04 | 2/3 |
| replan_mcp_01 | 0/3 |
| replan_mcp_03 | 0/3 |

汇总：all-three-success=1，instability count=2；三轮 subset success rates 为 0.3、0.2、
0.2，population variance 为 `0.0022222222222222214`。没有 cherry-pick 最佳一轮。

## 48. Final Runtime Reliability

Final run ID：

```text
20260902T042822Z-1da399b-reliability
```

修名后的 `reliability-summary.json`：

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

准确表述是“9 个预定义 Runtime / fault-injection mechanism cases 全部通过”，不是“9 个
crash recovery cases 全部恢复”，也不证明 universal exactly-once。

## 49. Final Evaluation Artifact and Claim Freeze

只读 finalizer 校验 source run IDs、task sets、attempt numbers 和 denominator 后生成：

```text
20260902T043156Z-1da399b-evaluation
```

关键文件：

- `evaluation-summary.json`：机器可读 Primary、failure analysis、ablation、stability、
  reliability 与边界；
- `report.md`：工程报告；
- `environment.json`、`pip-freeze.txt`、`pip-check.txt`：复现快照。

最终冻结文档：

- `docs/resume/taskpilot_resume.md`：先列 Verified Claims Table，每个数字包含 metric、run ID、
  raw file 与 calculation；
- `docs/interview/taskpilot_interview.md`：基于真实实现与 benchmark 的架构/恢复/安全问答；
- `README.md`：明确区分 Implemented、Tested、Benchmark-verified 与 Not implemented。

Vision provider integration 已实现；runtime path 使用 deterministic targeter 和真实 Chromium
验证；external vision-model benchmark **NOT RUN**。没有生成 Vision uplift 或 token reduction
claim。

## 50. Final Test and Static Evidence

最终 clean-env 命令：

```powershell
.\.venv-benchmark\Scripts\python.exe -m pytest -q
```

真实输出：

```text
........................................................................ [ 27%]
...............................................................s........ [ 54%]
........................................................................ [ 81%]
................................................                         [100%]
============================== warnings summary ===============================
.venv-benchmark\Lib\site-packages\langgraph\checkpoint\base\__init__.py:18
  LangChainPendingDeprecationWarning: The default value of allowed_objects will change in a future version.

263 passed, 1 skipped, 1 warning in 35.92s
```

结果：failed=0，errors=0。唯一 skip 是显式 opt-in 的真实 external Vision integration。

其他最终检查：

```text
pip check: No broken requirements found.
Ruff src benchmarks tests: All checks passed!
compileall src benchmarks tests: exit 0, no output
git diff --check: exit 0, no whitespace error; only LF→CRLF notices
```

为满足全仓 Ruff gate，仅机械清理了三个既有测试 lint：两个 unused imports 和一个 unused
assignment；相关 27 项测试再次通过，没有改变产品行为。

## 51. Final Git Snapshot

Commit：

```text
1da399ba2ac0bde44f271557a0c5dfeac0c5dceb
```

`git_dirty=true` 已在所有 official environment snapshots 中如实保存。最终
`git status --short`：

```text
 M .env.example
 M .gitignore
 M README.md
 M benchmarks/README.md
 M docs/review/phase_03.md
 M pyproject.toml
 M src/taskpilot/analyzer.py
 M src/taskpilot/cli.py
 M src/taskpilot/graph.py
 M src/taskpilot/llm.py
 M src/taskpilot/models.py
 M src/taskpilot/planner.py
 M src/taskpilot/state.py
 M tests/test_analyzer.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
 M tests/test_planner.py
?? benchmarks/__init__.py
?? benchmarks/fixtures/
?? benchmarks/manifest.json
?? benchmarks/results/
?? benchmarks/taskpilot_bench/
?? docs/interview/
?? docs/resume/
?? docs/review/phase_04.md
?? docs/review/phase_05.md
?? docs/review/phase_06.md
?? docs/review/phase_07.md
?? docs/review/phase_08.md
?? docs/review/phase_09.md
?? docs/review/phase_10.md
?? docs/review/phase_11.md
?? docs/review/phase_12.md
?? src/taskpilot/api/
?? src/taskpilot/browser/
?? src/taskpilot/context/
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/persistence/
?? src/taskpilot/policy/
?? src/taskpilot/replanner.py
?? src/taskpilot/service.py
?? src/taskpilot/tools/
?? src/taskpilot/trace/
?? src/taskpilot/verifier.py
?? src/taskpilot/vision/
?? tests/conftest.py
?? tests/fixtures/
?? tests/test_browser.py
?? tests/test_browser_executor.py
?? tests/test_browser_policy.py
?? tests/test_context.py
?? tests/test_executor.py
?? tests/test_hitl.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase10_graph.py
?? tests/test_phase10_trace_boundaries.py
?? tests/test_phase11_api.py
?? tests/test_phase11_vision.py
?? tests/test_phase12_benchmark.py
?? tests/test_phase5_graph.py
?? tests/test_phase9_mcp_resume.py
?? tests/test_phase9_persistence.py
?? tests/test_phase9_recovery.py
?? tests/test_policy.py
?? tests/test_replanner.py
?? tests/test_tools.py
?? tests/test_trace.py
?? tests/test_verifier.py
```

用户已有 Phase 3–11 修改没有被删除、reset 或自动 commit。Git 的用户级 ignore permission
warning 没有通过修改用户配置规避。

## 52. Final Phase Verdict

```text
Production recursion repair = COMPLETE
Fresh Real LLM smoke pipeline = PASS
Official Primary 30-task run = COMPLETE
No-Replan ablation = COMPLETE
Three-attempt stability subset = COMPLETE
Final Runtime Reliability = COMPLETE
Resume / Interview / README freeze = COMPLETE
Phase 12 final acceptance = COMPLETE
Phase 13 = NOT STARTED / DOES NOT EXIST
```

结果中的低成功率、False Completion 和 instability 都被原样保留；没有修改 task、prompt、
oracle、ground truth、模型或 raw status 来美化数字。
