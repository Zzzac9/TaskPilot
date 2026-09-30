# TaskPilot Phase 9 Review

## 1. Phase Goal

本阶段目标是把 Phase 8 的进程内 `MemorySaver` 升级为 SQLite durable
checkpoint，并解决“外部 Tool 已产生副作用，但 LangGraph node 尚未完成
checkpoint”时可能重复执行的问题。

实际完成：

- 官方 `AsyncSqliteSaver` 真实文件 checkpoint、关闭、重开与同 thread 恢复。
- `TaskExecutor.decide_action()` 一次只做一个模型 decision；生产 Graph 的
  `execute_tool_action` 一次最多 invoke 一个外部 Tool。
- 独立 SQLite Action Journal，严格执行 `STARTED commit → invoke → terminal
  commit → node return/checkpoint`。
- terminal Journal materialization/dedup、canonical arguments hash 和 identity
  fail-closed。
- replay-safe 自动恢复、unsafe ambiguous recovery interrupt、人工 retry/abort。
- Browser/MCP/Local Tool 的保守 `replay_safe` metadata 规则。
- Human Approval 跨 checkpointer/Graph/Executor 重建恢复。
- CLI `--resume TASK_ID`、`--state-dir PATH` 与完成/失败任务的保守恢复行为。
- 真 SQLite、真实 Chromium、真实本地 MCP STDIO 的离线恢复测试。

明确没有实现 Dynamic Replanning、Vision/OCR、FastAPI、Trace/OpenTelemetry、
Postgres、distributed lock/lease、多 worker 协调、完整 Browser live DOM 恢复或
Benchmark execution。Phase 9 不宣称 universal exactly-once。

## 2. Final Directory Tree

以下省略 `.git`、cache、`__pycache__` 与测试运行产物：

```text
TaskPilot/
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
├── benchmarks/
│   └── README.md
├── docs/
│   └── review/
│       ├── phase_03.md
│       ├── phase_04.md
│       ├── phase_05.md
│       ├── phase_06.md
│       ├── phase_07.md
│       ├── phase_08.md
│       └── phase_09.md
├── src/taskpilot/
│   ├── __init__.py
│   ├── analyzer.py
│   ├── cli.py
│   ├── config.py
│   ├── executor.py
│   ├── graph.py
│   ├── llm.py
│   ├── models.py
│   ├── planner.py
│   ├── state.py
│   ├── verifier.py
│   ├── browser/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── locators.py
│   │   ├── session.py
│   │   └── tools.py
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   ├── config.py
│   │   └── provider.py
│   ├── persistence/
│   │   ├── __init__.py
│   │   ├── checkpoint.py
│   │   ├── config.py
│   │   ├── journal.py
│   │   └── models.py
│   ├── policy/
│   │   ├── __init__.py
│   │   ├── browser.py
│   │   ├── engine.py
│   │   └── models.py
│   └── tools/
│       ├── __init__.py
│       ├── base.py
│       └── registry.py
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── mcp_config.json
    │   ├── mcp_server.py
    │   └── browser_site/
    ├── test_analyzer.py
    ├── test_browser.py
    ├── test_browser_executor.py
    ├── test_browser_policy.py
    ├── test_executor.py
    ├── test_graph_smoke.py
    ├── test_hitl.py
    ├── test_mcp.py
    ├── test_mcp_integration.py
    ├── test_models.py
    ├── test_phase5_graph.py
    ├── test_phase9_mcp_resume.py
    ├── test_phase9_persistence.py
    ├── test_phase9_recovery.py
    ├── test_planner.py
    ├── test_policy.py
    ├── test_tools.py
    └── test_verifier.py
```

## 3. Dependency and Version Compatibility Decision

安装前真实版本：

```text
langgraph=0.2.60
langgraph-checkpoint=2.0.8
langgraph-checkpoint-sqlite=not installed
langchain-core=0.3.63
aiosqlite=not installed
```

`langgraph-checkpoint-sqlite 2.0.0`可以保留 checkpoint 2.0.8，但该 checkpoint
版本的 `JsonPlusSerializer`没有 strict msgpack/allowlist API。选择了同一 major 线
的 `langgraph-checkpoint-sqlite 2.0.11`；它要求
`langgraph-checkpoint>=2.0.21,<3`，pip 解析为 2.1.2，而 `langgraph 0.2.60`
声明兼容 `langgraph-checkpoint>=2.0.4,<3`。没有升级 LangGraph 或
LangChain Core。

首次安装解析到 `aiosqlite 0.22.1`，真实 `setup()`失败：SQLite saver 调用
`Connection.is_alive()`，而 0.22 已移除该方法。最终把 aiosqlite 限定为
`>=0.20,<0.22`，实装 0.21.0。

最终版本：

```text
langgraph=0.2.60
langgraph-checkpoint=2.1.2
langgraph-checkpoint-sqlite=2.0.11
langchain-core=0.3.63
aiosqlite=0.21.0
```

核心 checkpoint 子包升级后先运行 Phase 1–8 回归，结果仍为：

```text
116 passed in 18.97s
```

## 4. AsyncSqliteSaver Actual API and Security

当前环境实测 API：

```text
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
AsyncSqliteSaver.from_conn_string(conn_string: str) -> AsyncIterator[AsyncSqliteSaver]
AsyncSqliteSaver(conn, *, serde: SerializerProtocol | None = None)
AsyncSqliteSaver.setup(self) -> None
```

`SQLiteCheckpointProvider`拥有 async context lifecycle，创建父目录、进入 saver
context、显式 `setup()`，退出时关闭连接；Graph node 不打开数据库。provider 还通过
saver 的公开 `alist()` API 暴露最小 `list_task_checkpoints(task_id)`。

当前兼容组合的 `JsonPlusSerializer`实际签名只有
`pickle_fallback=False`和 custom unpack hook；没有 `allowed_msgpack_modules`，
`BaseCheckpointSaver`也没有 `with_allowlist`。因此代码显式使用
`JsonPlusSerializer(pickle_fallback=False)`，没有添加无效的 strict 环境变量，也没有
使用 pickle fallback。checkpoint DB 必须作为 integrity-sensitive 文件保护；当前版本
不能宣称具备新版本的 schema-derived msgpack allowlist。

真实 round-trip 测试覆盖 `TaskSpec`、`PlanStep`、`StepResult`、
`ToolCallRecord`、`PolicyDecision`、`PendingToolAction`、`ApprovalRecord`和
`ExecutorAction`的类型恢复。

## 5. Why Action-level Refactor Was Required

只把 `MemorySaver`换成 SQLite 不够。旧流程在一个 `execute_step` node 内执行：

```text
LLM → Tool → LLM → Tool → finish_step → node return
```

checkpoint 只知道 node 进入前的位置。如果 Tool 已产生副作用但 node 尚未 return，
重启会重放整个 node。Phase 9 把模型 decision 和每个 external Tool invocation 拆成
不同 Graph node，模型决定先写入 `pending_executor_action`并 checkpoint，之后才进入
Journal + Tool 边界。

生产流程：

```text
START → initialize_task → analyze_task → plan_task → begin_step → decide_action
  ├─ FINISH_STEP → build_step_outcome → verify_step
  └─ TOOL → assess_policy
       ├─ DENY → policy_feedback → decide_action
       ├─ REQUIRE_APPROVAL → prepare_approval → human_approval [interrupt]
       │    ├─ reject → rejection_feedback → decide_action
       │    └─ approve → execute_tool_action
       └─ ALLOW → execute_tool_action
                    ├─ terminal / safe replay → decide_action
                    └─ unsafe STARTED → recovery_review [interrupt]
                         ├─ retry → execute_tool_action
                         └─ abort → END

verify_step
  ├─ reject → reset_step_attempt → decide_action
  └─ accept → advance_step → decide_action / verify_task → END
```

`TaskExecutor.execute()`保留给 Phase 1–8 离线兼容测试；`build_graph()`检测真实
`TaskExecutor`后只构建 action-level 生产图。没有 `decide_action`的旧 Fake 使用兼容图，
不代表生产执行语义。

## 6. ExecutorAction and TaskState Changes

`ExecutorAction`字段为 `step_id`、`action_index`、`call_id`、`action_type`、
`tool_name`和 `arguments`。`TOOL`保存精确工具名/参数；`FINISH_STEP`不执行 Tool，
其 arguments 是 `summary/evidence/output`，之后仍必须通过 Step Verifier。

TaskState 新增：

- `current_action_index`：当前 Step 当前 verification attempt 已产生的模型动作数。
- `pending_executor_action`：decision 与 policy/execution 之间的 frozen JSON-serializable action。
- `recovery_issue`：unsafe `STARTED`动作的人机恢复材料。

新 Step 与 Verifier retry 会把 action counter 重置为 0；approval pause/resume 和
crash/restart 不重置。PlanStep `retry_count`仍由 Step Verifier 管理，并进入
`action_key`，所以同一步不同 verification attempt 不会共用 Journal key。

## 7. Action Journal Schema, Hashing and Transaction Order

Checkpoint DB 只由官方 saver 管理。独立 Action Journal 表包含：

```text
action_key PRIMARY KEY
task_id, step_id, retry_count, action_index
call_id, tool_name, arguments_hash
status, replay_safe, attempt_count
result_json, error, created_at, updated_at
```

不复制完整 Tool arguments；arguments 已在 frozen action checkpoint 中。hash 输入为：

```python
json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

然后计算 SHA-256。key 为
`{task_id}:{step_id}:{retry_count}:{action_index}`，允许同一 Step 合法地多次调用
相同 Tool/arguments。恢复时 task/step/retry/action/call/tool/hash/replay_safe 任一不符
都会 fail closed。

严格 durable ordering：

```text
insert STARTED → commit
external Tool invoke（最多一次）
update SUCCEEDED/FAILED → commit
node return → LangGraph checkpoint
```

`STARTED`只说明已进入危险窗口，不说明 Tool 一定执行或一定未执行。

## 8. Replay Safety Rules

RiskLevel 与 replay-safe 是两条独立轴。高风险动作可能具有业务幂等性；低风险动作
也不自动等于可重放。Local Tool 只采信 Host 显式 `metadata.replay_safe is True`，
缺失默认 False，不按名称猜测。

Browser 规则：navigate/observe/fill/select/check/extract/list 为 True；click、download、
screenshot 为 False；switch_page 因新 session 的 page_id 不能保证对应旧 live Page，
保守为 False。submit/delete click 绝不自动 replay。

MCP 只有 `trust_tool_annotations=True`时，`read_only_hint=True`或
`idempotent_hint=True`才令 replay-safe=True。未信任 Server 即使自称幂等也保持 False。

## 9. Recovery, Materialization and Dedup

恢复查询到 terminal Journal：不 invoke Tool，直接用 frozen action identity 和 Journal
result/error materialize `ToolCallRecord`。append 前按 `(step_id, call_id)`去重；相同内容
不重复追加，内容冲突使任务 fatal，不静默覆盖。

恢复查询到 `STARTED`：

- replay-safe=True：同一 action_key 的 attempt_count +1 后自动 invoke exact action。
- replay-safe=False：生成脱敏 `RecoveryIssue`，TaskStatus 设为 INTERRUPTED，进入真实
  LangGraph recovery interrupt。

`RecoveryResponse`禁止 extra fields，只允许 RETRY/ABORT。RETRY 明确表示 Human 接受
可能重复副作用，再执行 exact frozen action；ABORT 不执行 Tool，任务 FAILED，并保留
RecoveryIssue/reason。不提供 assume_succeeded，因为没有 durable observation 时无法
构造可信结果。

## 10. Durable HITL and Runtime Reconstruction

高风险 decision、`pending_executor_action`、`PendingToolAction`和 interrupt node 位置写入
SQLite。测试在 Run A 关闭 saver/journal/Graph 后，Run B 新建 checkpointer、Graph、
Executor、Registry 和 Tool，再用同一 thread_id 读取 interrupt 并 approve；Tool 只从
0 调用到 1。

Persistent State 不等于 Live Resource。Browser、BrowserContext、Page、MCP
ClientSession、ToolRegistry、Tool object、LLM client、SQLite connection/checkpointer
都不在 TaskState。Host resume 时按 CLI flags 重建 provider/registry/executor，然后读取
同一 thread。

真实 MCP 测试关闭第一个 STDIO subprocess/provider，再启动新 provider、重新发现 Tool，
重建 Graph 并恢复同一 thread。terminal Journal observation 被复用，MCP Tool 没有再次
调用。

Browser 测试关闭第一 BrowserSession，再创建新 session 并成功 observe。新 page 是新的
live resource，不保留旧 URL/DOM。这是 partial environment reconstruction，不是 bitwise
live DOM restoration。

## 11. CLI

新任务与恢复命令：

```powershell
python -m taskpilot.cli "task..." --browser
python -m taskpilot.cli --resume TASK_ID --browser
python -m taskpilot.cli --resume TASK_ID --state-dir .taskpilot
```

默认数据库为 `.taskpilot/checkpoints.sqlite3`和 `.taskpilot/actions.sqlite3`。
`thread_id == task_id`。resume 不创建新 initial state；找不到 checkpoint 清晰失败；已完成
任务打印 `task already completed`且不调用 Graph/Tool；普通 final FAILED 不自动重启。
pending tool approval 与 ambiguous recovery 分别展示不同 prompt。恢复缺失原 Tool
provider 时 frozen action schema 校验会以“未注册的工具”失败，不让模型偷偷换工具。

实际 CLI help：

```text
usage: cli.py [-h] [--resume TASK_ID] [--state-dir STATE_DIR]
              [--mcp-config MCP_CONFIG] [--browser] [--headed]
              [task]

Run the TaskPilot graph.

positional arguments:
  task                  Task description for a new task

options:
  -h, --help            show this help message and exit
  --resume TASK_ID      Resume an existing durable LangGraph thread
  --state-dir STATE_DIR
                        Directory containing checkpoints.sqlite3 and
                        actions.sqlite3
  --mcp-config MCP_CONFIG
                        JSON file containing MCP server configurations
  --browser             Enable the Playwright Chromium Browser Tools
  --headed              Show the Chromium window (requires --browser)
```

## 12. Fault Injection and Test Matrix

`ExecutionFaultInjector`有三个同步 hook：

- `AFTER_JOURNAL_STARTED`
- `AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL`
- `AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN`

生产默认 `NoOpExecutionFaultInjector`。测试 `InjectedCrash`继承 BaseException，避免被普通
Tool/business `Exception`处理吞掉；不使用 sleep 或环境变量模拟 crash。

Phase 9 测试覆盖真实 saver open/setup/close、structured state round-trip、checkpoint
history、process-like Graph recreation、durable approval、completed CLI resume、两步恢复、
STARTED/terminal commits、terminal success/failure materialization、hash mismatch、dedup、
unsafe abort/retry、safe auto retry、risk/replay independence、MCP trust、Browser rules、
Verifier retry/feedback、runtime object exclusion、真实 MCP reconnect、真实 Browser
re-observe 和 action bound。全部离线，只使用 SQLite、localhost、Chromium 和本地 MCP
STDIO subprocess。

## 13. Actual Restart and Crash Smoke Outputs

命令：

```powershell
conda run -n ai python -m pytest -q -s tests/test_phase9_recovery.py -k "terminal_journal_deduplicates or replay_safe_started or unsafe_started_interrupts_and_abort or high_risk_approval_survives or completed_step_is_not_rerun"
```

真实输出：

```text
TERMINAL_JOURNAL_CRASH_SMOKE={"injected_crash": true, "journal_status": "succeeded", "tool_counter_after_execution": 1, "tool_counter_before": 0, "tool_counter_final": 1, "tool_reinvoked": false}
.REPLAY_SAFE_RECOVERY_SMOKE={"attempt_count": 2, "automatic_retry": true, "journal_status_before": "started", "recovery_interrupt": false, "tool_counter_final": 1}
.AMBIGUOUS_UNSAFE_CRASH_SMOKE={"automatic_retry": false, "journal": "started", "recovery_interrupt": true, "replay_safe": false, "side_effect_counter": 1}
.DURABLE_APPROVAL_SMOKE={"pending_approval_restored": true, "runtime_recreated": true, "status": "completed", "tool_counter_after_approval": 1, "tool_counter_before_approval": 0}
.FULL_RESTART_SMOKE={"step_1_status": "completed", "step_1_tool_count": 1, "step_2_status": "completed", "step_2_tool_count": 1, "task_status": "completed"}
.
5 passed, 9 deselected in 1.62s
```

Terminal smoke 使用临时文件 counter。Run A 的 Tool 将文件从 0 写成 1，Journal
SUCCEEDED 后故障注入，Graph node 未 checkpoint；Run B 重建 runtime 后从 Journal
materialize，counter 保持 1。

Ambiguous smoke 在 Tool 已把 side-effect counter 加到 1、terminal Journal 提交前 crash。
Run B 看到的只有 STARTED，所以没有自动 retry，而是 INTERRUPTED + recovery interrupt。

Full restart smoke 的 Step 1 已 verified complete，Step 2 的 STARTED 后 crash；Run B 只
恢复 Step 2，Step 1 counter 未增加。

## 14. Offline Test Results and Windows Temporary Directory

Phase 9 不再配置全局固定 `--basetemp`。`pyproject.toml`只保留：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

三个 Phase 9 测试文件的 SQLite、counter 和 checkpoint 路径都由 pytest
`tmp_path`提供。`conftest.py`没有覆盖 basetemp。`tests/.artifacts/browser*`只用于明确
的 Chromium 下载/截图产物，不是 pytest 全局临时目录。

指定 Phase 9 命令（直接使用 conda `ai`环境解释器，未传 `--basetemp`）：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest tests/test_phase9_persistence.py tests/test_phase9_recovery.py tests/test_phase9_mcp_resume.py -q
```

真实输出：

```text
....................                                                     [100%]
20 passed in 3.72s
```

Codex workspace 文件沙箱内不能读取用户 `%TEMP%\pytest-of-zc115`，会得到 sandbox
`WinError 5`；在真实 Windows 文件权限下运行上述同一解释器/参数后全部通过。因此最终
结论基于默认系统临时目录的真实运行结果，不通过重新指定 basetemp、skip 或测试修改
掩盖错误。

最终命令：

```powershell
D:\Anaconda\envs\ai\python.exe -m pytest -q
```

真实输出：

```text
........................................................................ [ 52%]
.................................................................        [100%]
137 passed in 22.02s
```

passed=137，failed=0。

## 15. Boundary Questions

### Q1. 仅把 MemorySaver 换成 SQLite，是否足以防止 node 内 Tool 重复执行？

No。node 内副作用与 node return/checkpoint 之间仍有 crash window。

### Q2. 为什么把一个 Tool Action 拆成独立持久化执行边界？

让 frozen decision 先 checkpoint，并让每次 external invoke 受独立 Journal intent/terminal
记录保护；恢复只需重放一个动作边界，而不是重跑包含多个副作用的 Python 循环。

### Q3. Tool 执行前是否先 durable 写 STARTED？

Yes。insert 后显式 commit，之后才 invoke。

### Q4. Journal 已 SUCCEEDED、Graph checkpoint 未写就 crash，恢复后是否再次调用 Tool？

No。验证 identity/hash 后 materialize Journal result。

### Q5. Journal STARTED 是否说明 Tool 一定执行过？

No。可能在 invoke 前、invoke 中或远端成功后 crash。

### Q6. STARTED + replay_safe=False 时是否自动 retry？

No。进入 RecoveryIssue + real interrupt。

### Q7. TaskPilot 是否宣称所有外部动作 exactly-once？

No。

### Q8. 目前真正保证什么？

已 terminal-journaled 的 confirmed action 恢复不重复；明确 safe 的 ambiguous action 可
replay；unsafe ambiguous action 不自动重复并交给 Human。

### Q9. RiskLevel 和 replay_safe 是否一回事？

No。测试包含 risk=high、replay_safe=True 的独立轴案例。

### Q10. Human Approval 能否跨 Python runtime / Graph object recreation 恢复？

Yes。基于同一 SQLite checkpoint/thread_id，且真实测试关闭并重建全部对象。

### Q11. Browser Page object 是否写入 SQLite？

No。

### Q12. MCP ClientSession 是否写入 SQLite？

No。

### Q13. 恢复任务时这些资源怎么办？

Host 按配置重建 Browser/MCP provider、ToolRegistry、LLM client、Executor 与 Graph。

### Q14. Browser 是否实现 bitwise live DOM restoration？

No。新 session 只能重新 navigate/observe 建立环境。

### Q15. Phase 9 是否支持多个 worker 并发恢复同一个 task？

No。只支持 single active runner per task_id；SQLite PK 不是 distributed lease。

## 16. Known Limitations

- 远端副作用成功、local terminal commit 前 crash 时无法自动判断远端事实。
- 没有通用 remote idempotency-key injection 或 reconciliation protocol。
- SQLite 适合当前单进程本地阶段，不是 production distributed database。
- 没有多 worker locking/ownership/lease。
- Browser live state 只支持 runtime reconstruction 和 re-observe，不恢复旧 DOM/session。
- MCP reconnect 依赖相同 server config 和 Tool namespace/schema 仍可用。
- 当前兼容 serializer 没有新版 msgpack allowlist API；只明确禁用 pickle fallback。
- 旧 Fake Executor 兼容图仍存在，仅用于保留 Phase 1–8 离线行为测试。
- 没有 Dynamic Replanning、Trace、Vision、FastAPI 或 Benchmark execution。

## 17. Files Changed in Phase 9

新建：

- `src/taskpilot/persistence/__init__.py`：导出 persistence API。
- `src/taskpilot/persistence/config.py`：两个 SQLite 路径配置。
- `src/taskpilot/persistence/checkpoint.py`：AsyncSqliteSaver lifecycle/history。
- `src/taskpilot/persistence/journal.py`：SQLite/Memory Action Journal。
- `src/taskpilot/persistence/models.py`：Journal/recovery/fault models。
- `tests/test_phase9_persistence.py`：checkpoint/journal/metadata/state schema 测试。
- `tests/test_phase9_recovery.py`：crash windows、dedup、HITL、restart、CLI 测试。
- `tests/test_phase9_mcp_resume.py`：真实 MCP provider reconstruction 测试。
- `docs/review/phase_09.md`：本验收文档。

修改：

- `pyproject.toml`：兼容依赖约束；pytest 使用系统默认唯一临时 run directory。
- `.gitignore`：忽略 `.taskpilot/` durable local state。
- `README.md`：Phase 9 能力、CLI、保证边界与限制。
- `models.py`：ExecutorActionType/ExecutorAction。
- `state.py`：action counter、frozen action、RecoveryIssue。
- `executor.py`：一次 decision、policy/schema/replay helpers、single frozen invoke。
- `graph.py`：action-level durable graph、Journal/recovery/materialization/dedup。
- `cli.py`：SQLite provider、resume/state-dir、两种 interrupt。
- `browser/tools.py`：显式 replay-safe metadata。
- `mcp/provider.py`：可信 annotation replay-safe derivation。
- `tests/test_browser.py`：Browser runtime recreation limitation。
- `tests/test_mcp_integration.py`：trusted/untrusted replay metadata assertions。

## 18. Full Relevant Source Code

以下内容来自本阶段最终工作区，不是 git diff。

+### pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "taskpilot"
version = "0.1.0"
description = "A general-purpose task execution agent for web and local environments."
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "langgraph>=0.2.60,<0.3",
    "langgraph-checkpoint>=2.1.2,<3",
    "langgraph-checkpoint-sqlite>=2.0.11,<3",
    # checkpoint-sqlite 2.0.11 uses Connection.is_alive(), removed in 0.22.
    "aiosqlite>=0.20,<0.22",
    "langchain-openai>=0.2",
    "mcp>=2.1,<3",
    "playwright>=1.62,<2",
    "jsonschema>=4",
    "pydantic>=2",
    "pytest>=7",
    "pytest-asyncio>=0.23",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

### src/taskpilot/models.py

```python
"""TaskPilot 任务状态使用的结构化数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PlanStepStatus(str, Enum):
    """单个计划步骤的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolCallStatus(str, Enum):
    """一次工具调用的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """整个任务的生命周期状态。"""

    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutorActionType(str, Enum):
    """Executor 单次模型决策的动作类型。"""

    TOOL = "tool"
    FINISH_STEP = "finish_step"


class TaskSpec(BaseModel):
    """对用户任务进行规范化描述。"""

    goal: str
    # 每个模型实例都创建独立容器，避免可变默认值在实例之间共享。
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output: str | None = None
    completion_criteria: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    """任务计划中的一个可执行步骤。"""

    id: int
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    retry_count: int = 0
    depends_on: list[int] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    """Initial Planner 生成的结构化任务计划。"""

    steps: list[PlanStep]


class StepExecutionOutcome(BaseModel):
    """Executor 对当前步骤执行结果的结构化声明。"""

    step_id: int
    claimed_complete: bool = False
    summary: str | None = None
    evidence: list[str] = Field(default_factory=list)
    # output 在 Verifier 接受前仍是不可信的 Executor 声明。
    output: dict[str, Any] = Field(default_factory=dict)
    action_count: int = 0
    error: str | None = None


class StepResult(BaseModel):
    """已经通过 Step Verifier 的正式步骤产物。"""

    step_id: int
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class CriterionCheck(BaseModel):
    """对一个步骤成功条件的独立验证结论。"""

    criterion_index: int
    satisfied: bool
    reason: str


class StepVerificationResult(BaseModel):
    """Step Verifier 对当前步骤完成声明的验证结果。"""

    step_id: int
    verified: bool
    checks: list[CriterionCheck] = Field(default_factory=list)
    feedback: str | None = None


class ToolCallRecord(BaseModel):
    """一次工具调用的原始执行记录。"""

    call_id: str
    step_id: int | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.PENDING
    result: Any | None = None
    error: str | None = None


class ExecutorAction(BaseModel):
    """已冻结并可写入 checkpoint 的单个 Executor 动作。"""

    step_id: int
    action_index: int
    call_id: str
    action_type: ExecutorActionType
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """任务完成条件的校验结果。"""

    completed: bool
    reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    next_action: str | None = None
```

### src/taskpilot/state.py

```python
"""TaskPilot 使用的 LangGraph 状态结构。"""

from typing import Any, TypedDict

from taskpilot.models import (
    ExecutorAction,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    VerificationResult,
)
from taskpilot.persistence.models import RecoveryIssue
from taskpilot.policy.models import ApprovalRecord, PendingToolAction, PolicyDecision


class TaskState(TypedDict):
    """在 TaskPilot 图节点之间传递的结构化任务状态。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    # step_results 只保存通过 Step Verifier 的正式步骤产物。
    step_results: list[StepResult]
    # step_verification 是当前步骤最近一次验证，不等同于任务级 verification。
    step_verification: StepVerificationResult | None
    # tool_calls 保留原始执行记录，便于后续追踪与恢复。
    tool_calls: list[ToolCallRecord]
    # task_results 只保存与最终目标直接相关的提炼结果，不与工具日志混用。
    task_results: dict[str, Any]
    verification: VerificationResult | None
    # pending_action 是未执行的冻结动作，不属于 ToolCallRecord。
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    # 每次模型 decision 后递增；审批与崩溃恢复都不能偷偷重置。
    current_action_index: int
    # 模型 decision 与真实 Tool execution 之间的 durable frozen action。
    pending_executor_action: ExecutorAction | None
    # STARTED 且不可安全重放时保存给人工恢复审查的问题。
    recovery_issue: RecoveryIssue | None
    status: TaskStatus
    error: str | None


class TaskStateUpdate(TypedDict, total=False):
    """图节点返回的部分状态更新；未返回的字段由 LangGraph 原样保留。"""

    task_id: str
    user_input: str
    task_spec: TaskSpec | None
    plan: list[PlanStep]
    current_step_id: int | None
    step_outcome: StepExecutionOutcome | None
    step_results: list[StepResult]
    step_verification: StepVerificationResult | None
    tool_calls: list[ToolCallRecord]
    task_results: dict[str, Any]
    verification: VerificationResult | None
    pending_action: PendingToolAction | None
    policy_decisions: list[PolicyDecision]
    approval_records: list[ApprovalRecord]
    current_action_index: int
    pending_executor_action: ExecutorAction | None
    recovery_issue: RecoveryIssue | None
    status: TaskStatus
    error: str | None
```

### src/taskpilot/executor.py

```python
"""单个 PlanStep 的有界 Function Calling Executor Runtime。"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from taskpilot.config import redact_sensitive_values
from taskpilot.llm import create_chat_model
from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    StepExecutionOutcome,
    StepResult,
    StepVerificationResult,
    TaskSpec,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    ToolPolicyLike,
)
from taskpilot.tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
)


FINISH_STEP_NAME = "finish_step"
FINISH_STEP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINISH_STEP_NAME,
        "description": (
            "Declare that the current step has sufficient evidence for its "
            "success criteria. This is a claim, not verifier approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Concise summary of what was achieved.",
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Evidence supporting each step success criterion.",
                },
                "output": {
                    "type": "object",
                    "description": (
                        "Structured result produced by this step for verified "
                        "downstream handoff."
                    ),
                },
            },
            "required": ["summary", "evidence", "output"],
            "additionalProperties": False,
        },
    },
}

EXECUTOR_SYSTEM_PROMPT = """你是 Task Executor，只执行当前 PlanStep，不执行未来步骤，也不修改 Task Goal 或 Constraints。
根据当前环境 Observation 决定下一动作；优先使用可用 Tool 获取事实，不得虚构外部结果。
每轮必须且最多选择一个动作：调用一个外部 Tool，或调用内部 finish_step。
Tool 失败后可以根据错误 Observation 调整下一动作，单次 Tool 失败不等于 Step 或 Task 失败。
只有当前 Step 的 success_criteria 已有充分 evidence 时才能调用 finish_step。
finish_step 的 output 必须只包含本步骤实际得到、可交给依赖步骤的结构化结果。
finish_step 只表示 Executor 声称当前 Step 已具备完成条件，最终是否完成由未来 Verifier 判断。
不要直接把 PlanStep 或整个 Task 声称为 completed。"""


@dataclass(frozen=True)
class ExecutorRunResult:
    """TaskExecutor 返回给 Graph 的步骤结果与新增工具记录。"""

    outcome: StepExecutionOutcome | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    pending_action: PendingToolAction | None = None
    policy_decisions: list[PolicyDecision] = field(default_factory=list)
    fatal_error: bool = False


@dataclass(frozen=True)
class ApprovedActionResult:
    """冻结动作获批后单次执行的记录；不重新询问模型或 Policy。"""

    tool_call: ToolCallRecord | None = None
    fatal_error: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ExecutorDecisionResult:
    """一次且仅一次模型调用产生的 frozen action 或 fatal error。"""

    action: ExecutorAction | None = None
    error: str | None = None
    fatal_error: bool = False


class TaskExecutorLike(Protocol):
    """真实 Executor 与离线 Fake 共同遵循的最小接口。"""

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """只执行传入的当前步骤。"""

        ...

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """只执行已获批的 exact frozen action。"""

        ...


def build_executor_messages(
    *,
    task_spec: TaskSpec,
    step: PlanStep,
    task_results: dict[str, Any],
    tool_calls: Sequence[ToolCallRecord],
    step_results: Sequence[StepResult] = (),
    verification_feedback: StepVerificationResult | None = None,
    policy_decisions: Sequence[PolicyDecision] = (),
    approval_records: Sequence[ApprovalRecord] = (),
) -> list[BaseMessage]:
    """只使用当前步骤执行所需的信息构造模型上下文。"""

    observations = [
        {
            "call_id": record.call_id,
            "tool_name": record.tool_name,
            "status": record.status.value,
            "result": record.result,
            "error": record.error,
        }
        for record in tool_calls
        if record.step_id == step.id
    ]
    dependency_results = [
        result.model_dump(mode="json")
        for result in step_results
        if result.step_id in step.depends_on
    ]
    feedback = (
        verification_feedback.model_dump(mode="json")
        if verification_feedback is not None
        and verification_feedback.step_id == step.id
        else None
    )
    current_policy = [
        decision.model_dump(mode="json")
        for decision in policy_decisions
        if decision.step_id == step.id
    ]
    current_approvals = [
        record.model_dump(mode="json")
        for record in approval_records
        if record.step_id == step.id
    ]
    context = {
        "task_goal": task_spec.goal,
        "task_constraints": task_spec.constraints,
        "current_step": {
            "id": step.id,
            "description": step.description,
            "success_criteria": step.success_criteria,
        },
        "current_step_observations": observations,
        "task_results": task_results,
        "dependency_results": dependency_results,
        "verification_feedback": feedback,
        # Policy preview 已递归脱敏；ApprovalRecord 本身不保存 arguments。
        "policy_feedback": current_policy,
        "approval_history": current_approvals,
    }
    return [
        SystemMessage(content=EXECUTOR_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)),
    ]


class TaskExecutor:
    """使用原生 Tool Calling 反复执行当前步骤，直到 finish_step 或动作上限。"""

    def __init__(
        self,
        registry: ToolRegistry,
        model: BaseChatModel | None = None,
        max_actions_per_step: int = 5,
        policy: ToolPolicyLike | None = None,
    ) -> None:
        if max_actions_per_step < 1:
            raise ValueError("max_actions_per_step 必须至少为 1")
        self._registry = registry
        self._model = model
        self._max_actions_per_step = max_actions_per_step
        self._policy = policy

    @property
    def max_actions_per_step(self) -> int:
        """供 Graph 在模型调用前执行 durable action 上限检查。"""

        return self._max_actions_per_step

    async def decide_action(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        action_index: int,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorDecisionResult:
        """调用模型一次并冻结一个动作；绝不执行外部 Tool。"""

        if action_index < 1:
            return ExecutorDecisionResult(
                error="Executor action_index 必须从 1 开始",
                fatal_error=True,
            )
        if not self._registry.list_tools():
            return ExecutorDecisionResult(
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )
        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        messages = build_executor_messages(
            task_spec=task_spec,
            step=step,
            task_results=task_results,
            tool_calls=tool_calls,
            step_results=step_results,
            verification_feedback=verification_feedback,
            policy_decisions=policy_decisions,
            approval_records=approval_records,
        )
        try:
            response = await self._model.bind_tools(schemas).ainvoke(messages)
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            return ExecutorDecisionResult(
                error=f"Executor 模型调用失败: {type(exc).__name__}: {detail}",
                fatal_error=True,
            )
        if not isinstance(response, AIMessage):
            return ExecutorDecisionResult(
                error="Executor 模型未返回 AIMessage",
                fatal_error=True,
            )
        if len(response.tool_calls) != 1:
            return ExecutorDecisionResult(
                error=("Executor 每轮必须返回且只能返回一个 Tool Call; "
                       f"实际数量: {len(response.tool_calls)}"),
                fatal_error=True,
            )
        raw = response.tool_calls[0]
        call_id = raw.get("id")
        if not call_id:
            return ExecutorDecisionResult(
                error="Executor Tool Call 缺少 tool_call_id", fatal_error=True
            )
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )
        if call_id in seen_call_ids:
            return ExecutorDecisionResult(
                error=f"Executor Tool Call ID 重复: {call_id}", fatal_error=True
            )
        name = raw["name"]
        arguments = raw["args"]
        if name == FINISH_STEP_NAME:
            error = self._validate_finish_arguments(arguments)
            if error is not None:
                return ExecutorDecisionResult(error=error, fatal_error=True)
            return ExecutorDecisionResult(
                action=ExecutorAction(
                    step_id=step.id,
                    action_index=action_index,
                    call_id=call_id,
                    action_type=ExecutorActionType.FINISH_STEP,
                    arguments=arguments,
                )
            )
        try:
            self._registry.get(name)
            self._registry.validate_arguments(name, arguments)
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ExecutorDecisionResult(error=str(exc), fatal_error=True)
        return ExecutorDecisionResult(
            action=ExecutorAction(
                step_id=step.id,
                action_index=action_index,
                call_id=call_id,
                action_type=ExecutorActionType.TOOL,
                tool_name=name,
                arguments=arguments,
            )
        )

    async def assess_action(self, action: ExecutorAction) -> PolicyDecision | None:
        """只评估 frozen Tool action；不执行 Tool。"""

        if action.action_type is not ExecutorActionType.TOOL or not action.tool_name:
            raise ValueError("Risk Policy 只能评估外部 Tool action")
        tool = self._registry.get(action.tool_name)
        self._registry.validate_arguments(action.tool_name, action.arguments)
        if self._policy is None:
            return None
        decision = await self._policy.assess(
            call_id=action.call_id,
            step_id=action.step_id,
            tool=tool,
            arguments=action.arguments,
        )
        if (
            decision.call_id != action.call_id
            or decision.step_id != action.step_id
            or decision.tool_name != action.tool_name
        ):
            raise ValueError("Tool Risk Policy 返回的 action identity 不一致，动作未执行")
        return decision

    def validate_frozen_action(self, action: ExecutorAction) -> None:
        """在 journal 与 invoke 前重新校验 frozen action identity/schema。"""

        if action.action_type is not ExecutorActionType.TOOL or not action.tool_name:
            raise ValueError("execute_tool_action 需要完整 Tool action")
        self._registry.get(action.tool_name)
        self._registry.validate_arguments(action.tool_name, action.arguments)

    def is_replay_safe(self, action: ExecutorAction) -> bool:
        """只采信 Host 已写入 Tool metadata 的显式 replay_safe。"""

        self.validate_frozen_action(action)
        tool = self._registry.get(action.tool_name or "")
        metadata = getattr(tool, "metadata", {}) or {}
        return metadata.get("replay_safe") is True

    async def invoke_frozen_action(self, action: ExecutorAction) -> Any:
        """执行一个已确定的 Tool action；调用方负责 journal ordering。"""

        self.validate_frozen_action(action)
        return await self._registry.invoke(action.tool_name or "", action.arguments)

    async def execute(
        self,
        *,
        task_spec: TaskSpec,
        step: PlanStep,
        task_results: dict[str, Any],
        tool_calls: Sequence[ToolCallRecord],
        step_results: Sequence[StepResult] = (),
        verification_feedback: StepVerificationResult | None = None,
        policy_decisions: Sequence[PolicyDecision] = (),
        approval_records: Sequence[ApprovalRecord] = (),
    ) -> ExecutorRunResult:
        """运行当前 Step 的单动作循环，不直接修改 PlanStep 状态。"""

        if not self._registry.list_tools():
            return self._error_result(
                step_id=step.id,
                action_count=0,
                error="Executor 当前没有注册任何外部工具",
                fatal_error=True,
            )

        if self._model is None:
            self._model = create_chat_model()
        schemas = self._registry.list_function_schemas() + [FINISH_STEP_SCHEMA]
        model_with_tools = self._model.bind_tools(schemas)
        messages = build_executor_messages(
            task_spec=task_spec,
            step=step,
            task_results=task_results,
            tool_calls=tool_calls,
            step_results=step_results,
            verification_feedback=verification_feedback,
            policy_decisions=policy_decisions,
            approval_records=approval_records,
        )
        new_records: list[ToolCallRecord] = []
        new_decisions: list[PolicyDecision] = []
        seen_call_ids = (
            {record.call_id for record in tool_calls}
            | {record.call_id for record in approval_records}
            | {decision.call_id for decision in policy_decisions}
        )

        for action_index in range(self._max_actions_per_step):
            action_count = action_index + 1
            try:
                response = await model_with_tools.ainvoke(messages)
            except Exception as exc:
                error_detail = redact_sensitive_values(str(exc))
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 模型调用失败: "
                        f"{type(exc).__name__}: {error_detail}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            if not isinstance(response, AIMessage):
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor 模型未返回 AIMessage",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            actions = response.tool_calls
            if len(actions) != 1:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=(
                        "Executor 每轮必须返回且只能返回一个 Tool Call; "
                        f"实际数量: {len(actions)}"
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            action = actions[0]
            action_name = action["name"]
            arguments = action["args"]
            call_id = action.get("id")
            if not call_id:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error="Executor Tool Call 缺少 tool_call_id",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            if call_id in seen_call_ids:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=f"Executor Tool Call ID 重复: {call_id}",
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            seen_call_ids.add(call_id)

            if action_name == FINISH_STEP_NAME:
                finish_error = self._validate_finish_arguments(arguments)
                if finish_error is not None:
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=finish_error,
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                return ExecutorRunResult(
                    outcome=StepExecutionOutcome(
                        step_id=step.id,
                        claimed_complete=True,
                        summary=arguments["summary"],
                        evidence=arguments["evidence"],
                        output=arguments["output"],
                        action_count=action_count,
                    ),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                )

            try:
                tool = self._registry.get(action_name)
            except UnknownToolError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            try:
                self._registry.validate_arguments(action_name, arguments)
            except ToolArgumentsError as exc:
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )

            if self._policy is not None:
                try:
                    decision = await self._policy.assess(
                        call_id=call_id,
                        step_id=step.id,
                        tool=tool,
                        arguments=arguments,
                    )
                except Exception as exc:
                    error_detail = redact_sensitive_values(str(exc))
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error=(
                            "Tool Risk Policy 评估失败，动作未执行: "
                            f"{type(exc).__name__}: {error_detail}"
                        ),
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                if (
                    decision.call_id != call_id
                    or decision.step_id != step.id
                    or decision.tool_name != action_name
                ):
                    return self._error_result(
                        step_id=step.id,
                        action_count=action_count,
                        error="Tool Risk Policy 返回的 action identity 不一致，动作未执行",
                        tool_calls=new_records,
                        policy_decisions=new_decisions,
                        fatal_error=True,
                    )
                new_decisions.append(decision)
                if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                    return ExecutorRunResult(
                        outcome=None,
                        tool_calls=new_records,
                        pending_action=PendingToolAction(
                            call_id=call_id,
                            step_id=step.id,
                            tool_name=action_name,
                            arguments=dict(arguments),
                            policy_decision=decision,
                        ),
                        policy_decisions=new_decisions,
                    )
                if decision.outcome is PolicyOutcome.DENY:
                    messages.append(response)
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "status": "denied",
                                    "policy_decision": decision.model_dump(
                                        mode="json"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                            tool_call_id=call_id,
                            name=action_name,
                            status="error",
                        )
                    )
                    continue

            try:
                result = await self._registry.invoke(action_name, arguments)
            except ToolArgumentsError as exc:  # pragma: no cover - 已预校验
                return self._error_result(
                    step_id=step.id,
                    action_count=action_count,
                    error=str(exc),
                    tool_calls=new_records,
                    policy_decisions=new_decisions,
                    fatal_error=True,
                )
            except Exception as exc:
                error = redact_sensitive_values(str(exc))
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.FAILED,
                    error=f"{type(exc).__name__}: {error}",
                )
                observation = {
                    "status": "failed",
                    "error": record.error,
                }
                tool_message_status = "error"
            else:
                record = ToolCallRecord(
                    call_id=call_id,
                    step_id=step.id,
                    tool_name=action_name,
                    arguments=arguments,
                    status=ToolCallStatus.SUCCESS,
                    result=result,
                )
                observation = {"status": "success", "result": result}
                tool_message_status = "success"

            new_records.append(record)
            messages.append(response)
            messages.append(
                ToolMessage(
                    content=json.dumps(observation, ensure_ascii=False, default=str),
                    tool_call_id=call_id,
                    name=action_name,
                    status=tool_message_status,
                )
            )

        return self._error_result(
            step_id=step.id,
            action_count=self._max_actions_per_step,
            error=(
                "Executor action limit reached: "
                f"{self._max_actions_per_step}"
            ),
            tool_calls=new_records,
            policy_decisions=new_decisions,
            fatal_error=False,
        )

    async def execute_approved_action(
        self,
        *,
        pending_action: PendingToolAction,
        approval_records: Sequence[ApprovalRecord],
    ) -> ApprovedActionResult:
        """执行一次冻结动作；不重新调用 LLM 或 Risk Policy。"""

        matching = [
            record
            for record in approval_records
            if record.call_id == pending_action.call_id
            and record.step_id == pending_action.step_id
            and record.tool_name == pending_action.tool_name
        ]
        if len(matching) != 1 or matching[0].decision is not ApprovalChoice.APPROVE:
            return ApprovedActionResult(
                fatal_error=True,
                error="冻结 Tool action 缺少唯一 APPROVE record",
            )
        try:
            self._registry.get(pending_action.tool_name)
            self._registry.validate_arguments(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except (UnknownToolError, ToolArgumentsError) as exc:
            return ApprovedActionResult(fatal_error=True, error=str(exc))

        try:
            result = await self._registry.invoke(
                pending_action.tool_name,
                pending_action.arguments,
            )
        except Exception as exc:
            detail = redact_sensitive_values(str(exc))
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.FAILED,
                error=f"{type(exc).__name__}: {detail}",
            )
        else:
            record = ToolCallRecord(
                call_id=pending_action.call_id,
                step_id=pending_action.step_id,
                tool_name=pending_action.tool_name,
                arguments=pending_action.arguments,
                status=ToolCallStatus.SUCCESS,
                result=result,
            )
        return ApprovedActionResult(tool_call=record)

    @staticmethod
    def _validate_finish_arguments(arguments: dict[str, Any]) -> str | None:
        parameters = FINISH_STEP_SCHEMA["function"]["parameters"]
        try:
            Draft202012Validator(parameters).validate(arguments)
        except ValidationError as exc:
            return f"finish_step 参数无效: {exc.message}"
        return None

    @staticmethod
    def _error_result(
        *,
        step_id: int,
        action_count: int,
        error: str,
        tool_calls: list[ToolCallRecord] | None = None,
        policy_decisions: list[PolicyDecision] | None = None,
        fatal_error: bool,
    ) -> ExecutorRunResult:
        return ExecutorRunResult(
            outcome=StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_count,
                error=redact_sensitive_values(error),
            ),
            tool_calls=tool_calls or [],
            policy_decisions=policy_decisions or [],
            fatal_error=fatal_error,
        )
```

### src/taskpilot/persistence/config.py

```python
"""Phase 9 本地持久化路径配置。"""

from pathlib import Path

from pydantic import BaseModel, field_validator


class PersistenceConfig(BaseModel):
    """LangGraph checkpoint 与 action journal 使用彼此独立的 SQLite 文件。"""

    checkpoint_db_path: Path = Path(".taskpilot/checkpoints.sqlite3")
    action_journal_db_path: Path = Path(".taskpilot/actions.sqlite3")

    @field_validator("checkpoint_db_path", "action_journal_db_path", mode="after")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        """把调用方给出的相对路径固定为绝对路径。"""

        return value.expanduser().resolve()

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> "PersistenceConfig":
        """用 CLI 的 state directory 构造默认的两个数据库路径。"""

        resolved = state_dir.expanduser().resolve()
        return cls(
            checkpoint_db_path=resolved / "checkpoints.sqlite3",
            action_journal_db_path=resolved / "actions.sqlite3",
        )
```

### src/taskpilot/persistence/checkpoint.py

```python
"""Host 管理的 AsyncSqliteSaver 生命周期。"""

from contextlib import AbstractAsyncContextManager
import sys
from types import TracebackType
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from taskpilot.persistence.config import PersistenceConfig


class SQLiteCheckpointProvider:
    """打开、setup 并关闭 durable LangGraph SQLite checkpointer。"""

    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self.checkpointer: AsyncSqliteSaver | None = None
        self._context: AbstractAsyncContextManager[AsyncSqliteSaver] | None = None

    async def __aenter__(self) -> "SQLiteCheckpointProvider":
        path = self.config.checkpoint_db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        context = AsyncSqliteSaver.from_conn_string(str(path))
        self._context = context
        try:
            self.checkpointer = await context.__aenter__()
            # 当前兼容版本没有 msgpack allowlist API；明确禁用 pickle fallback。
            self.checkpointer.serde = JsonPlusSerializer(pickle_fallback=False)
            await self.checkpointer.setup()
        except BaseException:
            # Windows 上 aiosqlite worker 未关闭会持续锁定数据库文件。
            await context.__aexit__(*sys.exc_info())
            self._context = None
            self.checkpointer = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        context = self._context
        self._context = None
        self.checkpointer = None
        if context is None:
            return None
        return await context.__aexit__(exc_type, exc, traceback)

    async def list_task_checkpoints(self, task_id: str) -> list[Any]:
        """用 saver 公开 alist API 返回指定 thread 的 checkpoint history。"""

        checkpointer = self.checkpointer
        if checkpointer is None:
            raise RuntimeError("SQLiteCheckpointProvider 必须先进入 async context")
        config = {"configurable": {"thread_id": task_id}}
        return [item async for item in checkpointer.alist(config)]
```

### src/taskpilot/persistence/models.py

```python
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
```

### src/taskpilot/persistence/journal.py

```python
"""独立于 LangGraph schema 的 SQLite external-action journal。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

import aiosqlite

from taskpilot.persistence.models import ActionJournalRecord, JournalStatus


class ActionJournalLike(Protocol):
    """SQLite journal 与离线 memory double 共用的最小异步接口。"""

    async def get(self, action_key: str) -> ActionJournalRecord | None: ...

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord: ...

    async def retry_started(self, action_key: str) -> ActionJournalRecord: ...

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord: ...

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class SQLiteActionJournal:
    """每次状态写入都独立 commit，缩小但不掩盖 crash ambiguity window。"""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "SQLiteActionJournal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        self._connection = connection
        await self.setup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()

    async def setup(self) -> None:
        connection = self._require_connection()
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS action_journal (
                action_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                retry_count INTEGER NOT NULL,
                action_index INTEGER NOT NULL,
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                replay_safe INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await connection.commit()

    async def get(self, action_key: str) -> ActionJournalRecord | None:
        connection = self._require_connection()
        cursor = await connection.execute(
            "SELECT * FROM action_journal WHERE action_key = ?", (action_key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._from_row(row) if row is not None else None

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO action_journal (
                action_key, task_id, step_id, retry_count, action_index,
                call_id, tool_name, arguments_hash, status, replay_safe,
                attempt_count, result_json, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                record.action_key,
                record.task_id,
                record.step_id,
                record.retry_count,
                record.action_index,
                record.call_id,
                record.tool_name,
                record.arguments_hash,
                record.status.value,
                int(record.replay_safe),
                record.attempt_count,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await connection.commit()
        return record

    async def retry_started(self, action_key: str) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """UPDATE action_journal
               SET attempt_count = attempt_count + 1, updated_at = ?
               WHERE action_key = ? AND status = ?""",
            (utc_now().isoformat(), action_key, JournalStatus.STARTED.value),
        )
        await connection.commit()
        return self._require_record(await self.get(action_key), action_key)

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord:
        return await self._terminal(
            action_key,
            JournalStatus.SUCCEEDED,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            error=None,
        )

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord:
        return await self._terminal(
            action_key,
            JournalStatus.FAILED,
            result_json=None,
            error=error,
        )

    async def _terminal(
        self,
        action_key: str,
        status: JournalStatus,
        *,
        result_json: str | None,
        error: str | None,
    ) -> ActionJournalRecord:
        connection = self._require_connection()
        await connection.execute(
            """UPDATE action_journal
               SET status = ?, result_json = ?, error = ?, updated_at = ?
               WHERE action_key = ? AND status = ?""",
            (
                status.value,
                result_json,
                error,
                utc_now().isoformat(),
                action_key,
                JournalStatus.STARTED.value,
            ),
        )
        await connection.commit()
        return self._require_record(await self.get(action_key), action_key)

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteActionJournal 必须先进入 async context")
        return self._connection

    @staticmethod
    def _require_record(
        record: ActionJournalRecord | None, action_key: str
    ) -> ActionJournalRecord:
        if record is None:
            raise RuntimeError(f"Action Journal record 不存在: {action_key}")
        return record

    @staticmethod
    def _from_row(row: aiosqlite.Row) -> ActionJournalRecord:
        return ActionJournalRecord(
            action_key=row["action_key"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            retry_count=row["retry_count"],
            action_index=row["action_index"],
            call_id=row["call_id"],
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            status=JournalStatus(row["status"]),
            replay_safe=bool(row["replay_safe"]),
            attempt_count=row["attempt_count"],
            result=(
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            ),
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class InMemoryActionJournal:
    """无 SQLite Host 时的离线兼容实现；生产 CLI 不使用。"""

    def __init__(self) -> None:
        self.records: dict[str, ActionJournalRecord] = {}

    async def get(self, action_key: str) -> ActionJournalRecord | None:
        return self.records.get(action_key)

    async def start(self, record: ActionJournalRecord) -> ActionJournalRecord:
        if record.action_key in self.records:
            raise ValueError(f"Action Journal key 已存在: {record.action_key}")
        self.records[record.action_key] = record
        return record

    async def retry_started(self, action_key: str) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"attempt_count": self.records[action_key].attempt_count + 1,
                    "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

    async def succeed(self, action_key: str, result: Any) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"status": JournalStatus.SUCCEEDED, "result": result,
                    "error": None, "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record

    async def fail(self, action_key: str, error: str) -> ActionJournalRecord:
        record = self.records[action_key].model_copy(
            update={"status": JournalStatus.FAILED, "result": None,
                    "error": error, "updated_at": utc_now()}
        )
        self.records[action_key] = record
        return record
```

### src/taskpilot/persistence/__init__.py

```python
"""TaskPilot durable checkpoint 与 action journal 基础设施。"""

from taskpilot.persistence.checkpoint import SQLiteCheckpointProvider
from taskpilot.persistence.config import PersistenceConfig
from taskpilot.persistence.journal import InMemoryActionJournal, SQLiteActionJournal
from taskpilot.persistence.models import (
    ActionJournalRecord,
    ExecutionFaultPoint,
    InjectedCrash,
    JournalStatus,
    RecoveryChoice,
    RecoveryIssue,
    RecoveryResponse,
)

__all__ = [
    "ActionJournalRecord",
    "ExecutionFaultPoint",
    "InMemoryActionJournal",
    "InjectedCrash",
    "JournalStatus",
    "PersistenceConfig",
    "RecoveryChoice",
    "RecoveryIssue",
    "RecoveryResponse",
    "SQLiteActionJournal",
    "SQLiteCheckpointProvider",
]
```


+### src/taskpilot/graph.py

```python
"""TaskPilot durable action-level LangGraph 工作流。"""

import hashlib
import json
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Sequence, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from taskpilot.analyzer import TaskAnalyzer, TaskAnalyzerLike
from taskpilot.config import redact_sensitive_values
from taskpilot.executor import ExecutorRunResult, TaskExecutor, TaskExecutorLike
from taskpilot.models import (
    ExecutorActionType,
    PlanStep,
    PlanStepStatus,
    StepExecutionOutcome,
    StepResult,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.persistence.journal import ActionJournalLike, InMemoryActionJournal
from taskpilot.persistence.models import (
    ActionJournalRecord,
    ExecutionFaultInjector,
    ExecutionFaultPoint,
    JournalStatus,
    NoOpExecutionFaultInjector,
    RecoveryChoice,
    RecoveryIssue,
    RecoveryResponse,
)
from taskpilot.planner import TaskPlanner, TaskPlannerLike
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    ApprovalResponse,
    PendingToolAction,
    PolicyOutcome,
    redact_preview,
)
from taskpilot.state import TaskState, TaskStateUpdate
from taskpilot.tools.registry import ToolRegistry
from taskpilot.verifier import (
    TaskVerifier,
    TaskVerifierLike,
    incomplete_outcome_rejection,
    validate_step_verification,
    validate_task_verification,
)


def initialize_task(state: TaskState) -> TaskStateUpdate:
    """将新创建的任务推进到运行状态。"""

    # 只允许 created → running，避免意外覆盖完成、失败等其他状态。
    if state["status"] == TaskStatus.CREATED:
        return {"status": TaskStatus.RUNNING}
    return {}


def analyze_task(
    state: TaskState,
    *,
    analyzer: TaskAnalyzerLike,
) -> TaskStateUpdate:
    """调用 Analyzer，并将结构化结果写回任务状态。"""

    try:
        task_spec = analyzer.analyze(state["user_input"])
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Analyzer 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }
    return {"task_spec": task_spec, "error": None}


def plan_task(
    state: TaskState,
    *,
    planner: TaskPlannerLike,
) -> TaskStateUpdate:
    """调用 Planner，并选出第一条依赖已满足的待执行步骤。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return {
            "status": TaskStatus.FAILED,
            "error": state["error"] or "Task Planner 无法运行: task_spec 缺失",
        }

    try:
        task_plan = planner.plan(task_spec)
        current_step_id = find_next_executable_step(task_plan.steps)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "plan": [],
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Planner 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if current_step_id is None:
        return {
            "plan": task_plan.steps,
            "current_step_id": None,
            "status": TaskStatus.FAILED,
            "error": "Task Planner 返回的计划没有可执行步骤",
        }
    return {
        "plan": task_plan.steps,
        "current_step_id": current_step_id,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_step(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """只执行 current_step_id 对应步骤，并保存 Executor 声明。"""

    current_step_id = state["current_step_id"]
    if current_step_id is None:
        return _failed_update(state["error"] or "Executor 无法运行: current_step_id 缺失")
    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update(state["error"] or "Executor 无法运行: task_spec 缺失")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            "Executor 无法定位唯一当前步骤: "
            f"current_step_id={current_step_id}"
        )

    current_step = state["plan"][step_index]
    if current_step.status not in {
        PlanStepStatus.PENDING,
        PlanStepStatus.RUNNING,
    }:
        return _failed_update(
            f"Executor 不能执行状态为 {current_step.status.value} 的步骤 "
            f"{current_step.id}"
        )

    running_step = current_step.model_copy(
        update={"status": PlanStepStatus.RUNNING}
    )
    updated_plan = list(state["plan"])
    updated_plan[step_index] = running_step
    prior_step_tool_calls = [
        record
        for record in state["tool_calls"]
        if record.step_id == current_step_id
    ]

    try:
        execution = await executor.execute(
            task_spec=task_spec,
            step=running_step,
            task_results=state["task_results"],
            tool_calls=prior_step_tool_calls,
            step_results=state["step_results"],
            verification_feedback=state.get("step_verification"),
            policy_decisions=state.get("policy_decisions", []),
            approval_records=state.get("approval_records", []),
        )
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        error = f"Task Executor 调用失败: {type(exc).__name__}: {error_detail}"
        return {
            "plan": updated_plan,
            "step_outcome": StepExecutionOutcome(
                step_id=current_step_id,
                claimed_complete=False,
                action_count=0,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }

    return _execution_state_update(
        state=state,
        updated_plan=updated_plan,
        execution=execution,
    )


def prepare_approval(state: TaskState) -> TaskStateUpdate:
    """在 interrupt 前提交 WAITING_APPROVAL，且不执行目标 Tool。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("prepare_approval 需要 pending_action")
    if pending.step_id != state["current_step_id"]:
        return _failed_update("pending_action.step_id 与当前步骤不一致")
    return {"status": TaskStatus.WAITING_APPROVAL, "error": None}


def human_approval(state: TaskState) -> TaskStateUpdate:
    """通过 LangGraph interrupt 获取只含 approve/reject 的 resume value。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("human_approval 需要 pending_action")
    decision = pending.policy_decision
    payload = {
        "type": "tool_approval",
        "call_id": pending.call_id,
        "step_id": pending.step_id,
        "tool_name": pending.tool_name,
        "risk_level": decision.risk_level.value,
        "reason": decision.reason,
        "arguments_preview": decision.preview,
        "tool_source": decision.tool_source,
    }
    # interrupt 之前没有外部副作用；resume 时该 node 会从头重放到这里。
    resume_value = interrupt(payload)
    try:
        response = ApprovalResponse.model_validate(resume_value)
    except ValidationError as exc:
        return {
            "status": TaskStatus.FAILED,
            "error": f"Human approval resume value 无效，Tool 未执行: {exc}",
        }
    record = ApprovalRecord(
        call_id=pending.call_id,
        step_id=pending.step_id,
        tool_name=pending.tool_name,
        decision=response.decision,
        reason=response.reason,
    )
    return {
        "approval_records": state.get("approval_records", []) + [record],
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_approved_action(
    state: TaskState,
    *,
    executor: TaskExecutorLike,
) -> TaskStateUpdate:
    """不再询问模型，执行 PendingToolAction 中冻结的 exact action 一次。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("execute_approved_action 需要 pending_action")
    result = await executor.execute_approved_action(
        pending_action=pending,
        approval_records=state.get("approval_records", []),
    )
    if result.fatal_error or result.tool_call is None:
        return {
            "status": TaskStatus.FAILED,
            "error": result.error or "获批 Tool action 未返回执行记录",
        }
    return {
        "tool_calls": state["tool_calls"] + [result.tool_call],
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def handle_rejection(state: TaskState) -> TaskStateUpdate:
    """清除被拒动作，让 Executor 从审批历史中看到拒绝并选择替代方案。"""

    pending = state.get("pending_action")
    if pending is None:
        return _failed_update("handle_rejection 需要 pending_action")
    matches = [
        record
        for record in state.get("approval_records", [])
        if record.call_id == pending.call_id
        and record.step_id == pending.step_id
        and record.tool_name == pending.tool_name
    ]
    if len(matches) != 1 or matches[0].decision is not ApprovalChoice.REJECT:
        return _failed_update("被拒动作缺少唯一 REJECT approval record")
    return {
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_step(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
) -> TaskStateUpdate:
    """验证当前 Step 声明，并记录接受或拒绝结论。"""

    current_step_id = state["current_step_id"]
    task_spec = state["task_spec"]
    outcome = state["step_outcome"]
    if current_step_id is None or task_spec is None or outcome is None:
        return _failed_update("Step Verifier 无法运行: 当前步骤上下文不完整")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"Step Verifier 无法定位唯一当前步骤: {current_step_id}"
        )
    step = state["plan"][step_index]
    if outcome.step_id != step.id:
        return _failed_update(
            "StepExecutionOutcome.step_id 与当前步骤不一致: "
            f"expected={step.id}, actual={outcome.step_id}"
        )

    current_calls = [
        record for record in state["tool_calls"] if record.step_id == step.id
    ]
    dependency_results = [
        result
        for result in state["step_results"]
        if result.step_id in step.depends_on
    ]
    try:
        if outcome.claimed_complete:
            result = verifier.verify_step(
                task_spec=task_spec,
                step=step,
                outcome=outcome,
                tool_calls=current_calls,
                dependency_results=dependency_results,
            )
        else:
            # 尚未 claim complete 时确定性拒绝，不浪费一次模型调用。
            result = incomplete_outcome_rejection(step, outcome)
        validate_step_verification(step, result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        failed_step = step.model_copy(update={"status": PlanStepStatus.FAILED})
        return {
            "plan": _replace_step(state["plan"], step_index, failed_step),
            "status": TaskStatus.FAILED,
            "error": (
                f"Step Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.verified:
        return {
            "step_verification": result,
            "status": TaskStatus.RUNNING,
            "error": None,
        }

    retry_count = step.retry_count + 1
    reached_limit = retry_count >= max_step_attempts
    rejected_step = step.model_copy(
        update={
            "retry_count": retry_count,
            "status": (
                PlanStepStatus.FAILED
                if reached_limit
                else PlanStepStatus.RUNNING
            ),
        }
    )
    update: TaskStateUpdate = {
        "plan": _replace_step(state["plan"], step_index, rejected_step),
        "step_verification": result,
        "status": TaskStatus.FAILED if reached_limit else TaskStatus.RUNNING,
        "error": None,
    }
    if reached_limit:
        update["error"] = (
            f"Step {step.id} 连续被 Verifier 拒绝，已达到 "
            f"max_step_attempts={max_step_attempts}: "
            f"{result.feedback or '未提供反馈'}"
        )
    return update


def advance_step(state: TaskState) -> TaskStateUpdate:
    """接受已验证 Claim，沉淀 StepResult 并选择下一可执行步骤。"""

    current_step_id = state["current_step_id"]
    outcome = state["step_outcome"]
    verification = state["step_verification"]
    if (
        current_step_id is None
        or outcome is None
        or verification is None
        or not verification.verified
    ):
        return _failed_update("advance_step 需要已通过验证的当前步骤")
    if outcome.step_id != current_step_id or verification.step_id != current_step_id:
        return _failed_update("advance_step 的步骤 ID 不一致")
    if not outcome.summary:
        return _failed_update("已验证的 StepExecutionOutcome 缺少 summary")

    step_index = _find_unique_step_index(state["plan"], current_step_id)
    if step_index is None:
        return _failed_update(
            f"advance_step 无法定位唯一当前步骤: {current_step_id}"
        )
    completed_step = state["plan"][step_index].model_copy(
        update={"status": PlanStepStatus.COMPLETED}
    )
    updated_plan = _replace_step(state["plan"], step_index, completed_step)
    result = StepResult(
        step_id=current_step_id,
        summary=outcome.summary,
        output=outcome.output,
        evidence=outcome.evidence,
    )
    updated_results = _upsert_step_result(
        state["step_results"],
        result,
        updated_plan,
    )

    try:
        next_step_id = find_next_executable_step(updated_plan)
    except ValueError as exc:
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": str(exc),
        }

    if next_step_id is None and not all(
        step.status == PlanStepStatus.COMPLETED for step in updated_plan
    ):
        return {
            "plan": updated_plan,
            "step_results": updated_results,
            "current_step_id": None,
            "step_outcome": None,
            "step_verification": None,
            "status": TaskStatus.FAILED,
            "error": "Plan 状态异常: 没有 pending 步骤，但并非所有步骤已完成",
        }

    return {
        "plan": updated_plan,
        "step_results": updated_results,
        "current_step_id": next_step_id,
        # 新步骤不能继承上一条步骤的 Claim 或反馈。
        "step_outcome": None,
        "step_verification": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def verify_task(
    state: TaskState,
    *,
    verifier: TaskVerifierLike,
) -> TaskStateUpdate:
    """所有步骤完成后，独立验证 Task completion criteria。"""

    task_spec = state["task_spec"]
    if task_spec is None:
        return _failed_update("Task Verifier 无法运行: task_spec 缺失")
    try:
        result = verifier.verify_task(
            task_spec=task_spec,
            step_results=state["step_results"],
            task_results=state["task_results"],
        )
        validate_task_verification(result)
    except Exception as exc:
        error_detail = redact_sensitive_values(str(exc))
        return {
            "status": TaskStatus.FAILED,
            "error": (
                f"Task Verifier 调用失败: {type(exc).__name__}: {error_detail}"
            ),
        }

    if result.completed:
        return {
            "verification": result,
            "current_step_id": None,
            "status": TaskStatus.COMPLETED,
            "error": None,
        }
    next_hint = f"; next_action={result.next_action}" if result.next_action else ""
    return {
        "verification": result,
        "current_step_id": None,
        "status": TaskStatus.FAILED,
        "error": f"Task Verifier 拒绝任务完成: {result.reason}{next_hint}",
    }


def find_next_executable_step(plan: Sequence[PlanStep]) -> int | None:
    """按计划顺序选择第一条依赖均已完成的 pending 步骤。"""

    completed_ids = {
        step.id for step in plan if step.status == PlanStepStatus.COMPLETED
    }
    pending_steps = [
        step for step in plan if step.status == PlanStepStatus.PENDING
    ]
    for step in pending_steps:
        if all(dependency_id in completed_ids for dependency_id in step.depends_on):
            return step.id
    if pending_steps:
        blocked = {
            step.id: [
                dependency_id
                for dependency_id in step.depends_on
                if dependency_id not in completed_ids
            ]
            for step in pending_steps
        }
        raise ValueError(
            "Plan 依赖阻塞: 存在 pending 步骤但没有可执行步骤; "
            f"unmet_dependencies={blocked}"
        )
    return None


def _execution_state_update(
    *,
    state: TaskState,
    updated_plan: list[PlanStep],
    execution: ExecutorRunResult,
) -> TaskStateUpdate:
    """把 Executor 返回值合并到 LangGraph 状态。"""

    update: TaskStateUpdate = {
        "plan": updated_plan,
        "tool_calls": state["tool_calls"] + execution.tool_calls,
        "policy_decisions": (
            state.get("policy_decisions", []) + execution.policy_decisions
        ),
        "step_outcome": execution.outcome,
        "pending_action": execution.pending_action,
        "status": (
            TaskStatus.FAILED if execution.fatal_error else TaskStatus.RUNNING
        ),
        "error": (
            execution.outcome.error
            if execution.fatal_error and execution.outcome is not None
            else None
        ),
    }
    if execution.fatal_error and execution.outcome is None:
        update["error"] = "Executor fatal failure 未提供 StepExecutionOutcome"
    if not execution.fatal_error and execution.outcome is None:
        if execution.pending_action is None:
            update["status"] = TaskStatus.FAILED
            update["error"] = "Executor 未返回 outcome 或 pending_action"
    return update


def _find_unique_step_index(plan: Sequence[PlanStep], step_id: int) -> int | None:
    matches = [index for index, step in enumerate(plan) if step.id == step_id]
    return matches[0] if len(matches) == 1 else None


def _replace_step(
    plan: Sequence[PlanStep],
    index: int,
    step: PlanStep,
) -> list[PlanStep]:
    updated = list(plan)
    updated[index] = step
    return updated


def _upsert_step_result(
    step_results: Sequence[StepResult],
    result: StepResult,
    plan: Sequence[PlanStep],
) -> list[StepResult]:
    """按 step_id 替换正式结果，并保持计划顺序且不产生重复。"""

    by_step_id = {
        item.step_id: item for item in step_results if item.step_id != result.step_id
    }
    by_step_id[result.step_id] = result
    plan_order = {step.id: index for index, step in enumerate(plan)}
    return sorted(
        by_step_id.values(),
        key=lambda item: plan_order.get(item.step_id, len(plan)),
    )


def _failed_update(error: str) -> TaskStateUpdate:
    return {"status": TaskStatus.FAILED, "error": error}


def _route_after_plan(state: TaskState) -> Literal["execute_step", "end"]:
    return "end" if state["status"] == TaskStatus.FAILED else "execute_step"


def _route_after_execute(
    state: TaskState,
) -> Literal["prepare_approval", "verify_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    return "verify_step"


def _route_after_human_approval(
    state: TaskState,
) -> Literal["execute_approved_action", "handle_rejection", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    records = state.get("approval_records", [])
    if not records:
        return "end"
    return (
        "execute_approved_action"
        if records[-1].decision is ApprovalChoice.APPROVE
        else "handle_rejection"
    )


def _route_after_step_verification(
    state: TaskState,
) -> Literal["advance_step", "execute_step", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    verification = state["step_verification"]
    if verification is not None and verification.verified:
        return "advance_step"
    return "execute_step"


def _route_after_advance(
    state: TaskState,
) -> Literal["execute_step", "verify_task", "end"]:
    if state["status"] == TaskStatus.FAILED:
        return "end"
    if state["current_step_id"] is None:
        return "verify_task"
    return "execute_step"


def _build_legacy_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """构建带 Policy/HITL、两层验证和有限重试的状态图。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = (
        executor
        if executor is not None
        else TaskExecutor(registry=ToolRegistry())
    )
    active_verifier = verifier if verifier is not None else TaskVerifier()

    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node(
        "analyze_task",
        partial(analyze_task, analyzer=active_analyzer),
    )
    builder.add_node("plan_task", partial(plan_task, planner=active_planner))
    builder.add_node(
        "execute_step",
        partial(execute_step, executor=active_executor),
    )
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("human_approval", human_approval)
    builder.add_node(
        "execute_approved_action",
        partial(execute_approved_action, executor=active_executor),
    )
    builder.add_node("handle_rejection", handle_rejection)
    builder.add_node(
        "verify_step",
        partial(
            verify_step,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
        ),
    )
    builder.add_node("advance_step", advance_step)
    builder.add_node(
        "verify_task",
        partial(verify_task, verifier=active_verifier),
    )

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task",
        _route_after_plan,
        {"execute_step": "execute_step", "end": END},
    )
    builder.add_conditional_edges(
        "execute_step",
        _route_after_execute,
        {
            "prepare_approval": "prepare_approval",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_approved_action",
            "handle_rejection": "handle_rejection",
            "end": END,
        },
    )
    builder.add_edge("execute_approved_action", "execute_step")
    builder.add_edge("handle_rejection", "execute_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_step_verification,
        {
            "advance_step": "advance_step",
            "execute_step": "execute_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "advance_step",
        _route_after_advance,
        {
            "execute_step": "execute_step",
            "verify_task": "verify_task",
            "end": END,
        },
    )
    builder.add_edge("verify_task", END)
    return builder.compile(checkpointer=checkpointer)


def begin_step(state: TaskState) -> TaskStateUpdate:
    """进入新 Step，并为本次 verification attempt 初始化 action counter。"""

    step_id = state.get("current_step_id")
    if step_id is None:
        return _failed_update("begin_step 需要 current_step_id")
    index = _find_unique_step_index(state["plan"], step_id)
    if index is None:
        return _failed_update(f"begin_step 无法定位唯一当前步骤: {step_id}")
    step = state["plan"][index]
    running = step.model_copy(update={"status": PlanStepStatus.RUNNING})
    return {
        "plan": _replace_step(state["plan"], index, running),
        "current_action_index": 0,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "step_outcome": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def decide_action(
    state: TaskState,
    *,
    executor: TaskExecutor,
) -> TaskStateUpdate:
    """最多调用模型一次，并把 frozen ExecutorAction 写回 state。"""

    step_id = state.get("current_step_id")
    task_spec = state.get("task_spec")
    if step_id is None or task_spec is None:
        return _failed_update("decide_action 缺少当前 Step 或 TaskSpec")
    index = _find_unique_step_index(state["plan"], step_id)
    if index is None:
        return _failed_update(f"decide_action 无法定位唯一当前步骤: {step_id}")
    current_index = state.get("current_action_index", 0)
    if current_index >= executor.max_actions_per_step:
        return {
            "step_outcome": StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=current_index,
                error=f"Executor action limit reached: {executor.max_actions_per_step}",
            ),
            "pending_executor_action": None,
            "status": TaskStatus.RUNNING,
            "error": None,
        }
    action_index = current_index + 1
    result = await executor.decide_action(
        task_spec=task_spec,
        step=state["plan"][index],
        action_index=action_index,
        task_results=state["task_results"],
        tool_calls=state["tool_calls"],
        step_results=state["step_results"],
        verification_feedback=state.get("step_verification"),
        policy_decisions=state.get("policy_decisions", []),
        approval_records=state.get("approval_records", []),
    )
    if result.fatal_error or result.action is None:
        error = result.error or "Executor decision 未返回 action"
        return {
            "current_action_index": action_index,
            "step_outcome": StepExecutionOutcome(
                step_id=step_id,
                claimed_complete=False,
                action_count=action_index,
                error=error,
            ),
            "status": TaskStatus.FAILED,
            "error": error,
        }
    action = result.action
    if action.step_id != step_id or action.action_index != action_index:
        return _failed_update("ExecutorAction identity 与 Graph 当前边界不一致")
    return {
        "current_action_index": action_index,
        "pending_executor_action": action,
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def build_step_outcome(state: TaskState) -> TaskStateUpdate:
    """把 FINISH_STEP frozen arguments 转换为未验证的完成声明。"""

    action = state.get("pending_executor_action")
    if action is None or action.action_type is not ExecutorActionType.FINISH_STEP:
        return _failed_update("build_step_outcome 需要 FINISH_STEP action")
    arguments = action.arguments
    return {
        "step_outcome": StepExecutionOutcome(
            step_id=action.step_id,
            claimed_complete=True,
            summary=arguments["summary"],
            evidence=arguments["evidence"],
            output=arguments["output"],
            action_count=action.action_index,
        ),
        "pending_executor_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def assess_policy(
    state: TaskState,
    *,
    executor: TaskExecutor,
) -> TaskStateUpdate:
    """评估 frozen action；审批之前绝不执行目标 Tool。"""

    action = state.get("pending_executor_action")
    if action is None:
        return _failed_update("assess_policy 需要 pending_executor_action")
    try:
        decision = await executor.assess_action(action)
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        return _failed_update(
            f"Tool Risk Policy 评估失败，动作未执行: {type(exc).__name__}: {detail}"
        )
    if decision is None:
        return {"status": TaskStatus.RUNNING, "error": None}
    update: TaskStateUpdate = {
        "policy_decisions": state.get("policy_decisions", []) + [decision],
        "status": TaskStatus.RUNNING,
        "error": None,
    }
    if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
        update["pending_action"] = PendingToolAction(
            call_id=action.call_id,
            step_id=action.step_id,
            tool_name=action.tool_name or "",
            arguments=dict(action.arguments),
            policy_decision=decision,
        )
    return update


def policy_feedback(state: TaskState) -> TaskStateUpdate:
    """保留 DENY decision，清除动作后让模型选择替代方案。"""

    return {
        "pending_executor_action": None,
        "pending_action": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def rejection_feedback(state: TaskState) -> TaskStateUpdate:
    """校验 REJECT record 后清除两种 pending action。"""

    update = handle_rejection(state)
    if update.get("status") is not TaskStatus.FAILED:
        update["pending_executor_action"] = None
    return update


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    """按 Phase 9 约定计算稳定 JSON SHA-256。"""

    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def action_key_for(state: TaskState) -> str:
    """生成包含 task/step/retry/action index 的稳定 journal key。"""

    action = state.get("pending_executor_action")
    if action is None:
        raise ValueError("action_key_for 需要 pending_executor_action")
    index = _find_unique_step_index(state["plan"], action.step_id)
    if index is None:
        raise ValueError("action_key_for 无法定位唯一 PlanStep")
    retry_count = state["plan"][index].retry_count
    return f"{state['task_id']}:{action.step_id}:{retry_count}:{action.action_index}"


def _journal_identity_error(
    record: ActionJournalRecord,
    *,
    state: TaskState,
    arguments_hash: str,
    replay_safe: bool,
) -> str | None:
    action = state["pending_executor_action"]
    assert action is not None
    index = _find_unique_step_index(state["plan"], action.step_id)
    assert index is not None
    expected = (
        state["task_id"], action.step_id, state["plan"][index].retry_count,
        action.action_index, action.call_id, action.tool_name, arguments_hash,
        replay_safe,
    )
    actual = (
        record.task_id, record.step_id, record.retry_count, record.action_index,
        record.call_id, record.tool_name, record.arguments_hash, record.replay_safe,
    )
    return None if actual == expected else "Action Journal identity/hash 不一致，拒绝恢复"


def _materialize_tool_call(
    state: TaskState,
    record: ActionJournalRecord,
) -> TaskStateUpdate:
    action = state["pending_executor_action"]
    assert action is not None and action.tool_name is not None
    tool_call = ToolCallRecord(
        call_id=action.call_id,
        step_id=action.step_id,
        tool_name=action.tool_name,
        arguments=action.arguments,
        status=(
            ToolCallStatus.SUCCESS
            if record.status is JournalStatus.SUCCEEDED
            else ToolCallStatus.FAILED
        ),
        result=record.result,
        error=record.error,
    )
    existing = [
        item for item in state["tool_calls"]
        if item.step_id == tool_call.step_id and item.call_id == tool_call.call_id
    ]
    if existing:
        if len(existing) != 1 or existing[0].model_dump(mode="json") != tool_call.model_dump(mode="json"):
            return _failed_update("ToolCallRecord identity 内容冲突，拒绝静默覆盖")
        updated_calls = state["tool_calls"]
    else:
        updated_calls = state["tool_calls"] + [tool_call]
    return {
        "tool_calls": updated_calls,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


async def execute_tool_action(
    state: TaskState,
    *,
    executor: TaskExecutor,
    journal: ActionJournalLike,
    fault_injector: ExecutionFaultInjector,
) -> TaskStateUpdate:
    """按 STARTED → one invoke → terminal → node return 的顺序执行一次。"""

    action = state.get("pending_executor_action")
    if action is None or action.action_type is not ExecutorActionType.TOOL:
        return _failed_update("execute_tool_action 需要 frozen Tool action")
    try:
        executor.validate_frozen_action(action)
        replay_safe = executor.is_replay_safe(action)
        action_key = action_key_for(state)
        arguments_hash = canonical_arguments_hash(action.arguments)
    except Exception as exc:
        return _failed_update(f"Frozen Tool action 校验失败: {exc}")

    record = await journal.get(action_key)
    if record is not None:
        identity_error = _journal_identity_error(
            record,
            state=state,
            arguments_hash=arguments_hash,
            replay_safe=replay_safe,
        )
        if identity_error is not None:
            return _failed_update(identity_error)
        if record.status in {JournalStatus.SUCCEEDED, JournalStatus.FAILED}:
            return _materialize_tool_call(state, record)
        issue = state.get("recovery_issue")
        authorized = (
            issue is not None
            and issue.action_key == action_key
            and issue.retry_authorized
        )
        if not replay_safe and not authorized:
            preview = redact_preview(action.arguments)
            return {
                "recovery_issue": RecoveryIssue(
                    action_key=action_key,
                    task_id=state["task_id"],
                    step_id=action.step_id,
                    action_index=action.action_index,
                    tool_name=action.tool_name or "",
                    call_id=action.call_id,
                    reason=("Journal 仅有 STARTED；Tool 可能尚未执行、执行中，"
                            "或远端已产生副作用但终态未提交"),
                    replay_safe=False,
                    arguments_preview=cast(dict[str, Any], preview),
                ),
                "status": TaskStatus.INTERRUPTED,
                "error": None,
            }
        record = await journal.retry_started(action_key)
    else:
        index = _find_unique_step_index(state["plan"], action.step_id)
        assert index is not None
        now = datetime.now(UTC)
        record = ActionJournalRecord(
            action_key=action_key,
            task_id=state["task_id"],
            step_id=action.step_id,
            retry_count=state["plan"][index].retry_count,
            action_index=action.action_index,
            call_id=action.call_id,
            tool_name=action.tool_name or "",
            arguments_hash=arguments_hash,
            status=JournalStatus.STARTED,
            replay_safe=replay_safe,
            created_at=now,
            updated_at=now,
        )
        await journal.start(record)
        fault_injector.hit(ExecutionFaultPoint.AFTER_JOURNAL_STARTED, action_key)

    try:
        result = await executor.invoke_frozen_action(action)
        fault_injector.hit(
            ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL,
            action_key,
        )
    except Exception as exc:
        detail = redact_sensitive_values(str(exc))
        record = await journal.fail(action_key, f"{type(exc).__name__}: {detail}")
    else:
        record = await journal.succeed(action_key, result)
    fault_injector.hit(
        ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN,
        action_key,
    )
    return _materialize_tool_call(state, record)


def recovery_review(state: TaskState) -> TaskStateUpdate:
    """对不可安全重放的 ambiguous STARTED action 发起真实 interrupt。"""

    issue = state.get("recovery_issue")
    if issue is None:
        return _failed_update("recovery_review 需要 RecoveryIssue")
    payload = {
        "type": "ambiguous_tool_recovery",
        "action_key": issue.action_key,
        "tool_name": issue.tool_name,
        "reason": issue.reason,
        "arguments_preview": issue.arguments_preview,
        "choices": [RecoveryChoice.RETRY.value, RecoveryChoice.ABORT.value],
    }
    resume_value = interrupt(payload)
    try:
        response = RecoveryResponse.model_validate(resume_value)
    except ValidationError as exc:
        return _failed_update(f"Recovery resume value 无效，Tool 未执行: {exc}")
    if response.choice is RecoveryChoice.ABORT:
        updated = issue.model_copy(update={"resolution_reason": response.reason})
        return {
            "recovery_issue": updated,
            "status": TaskStatus.FAILED,
            "error": response.reason or f"Human aborted ambiguous action {issue.action_key}",
        }
    updated = issue.model_copy(
        update={"retry_authorized": True, "resolution_reason": response.reason}
    )
    return {
        "recovery_issue": updated,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def reset_step_attempt(state: TaskState) -> TaskStateUpdate:
    """Verifier reject 后开始新 attempt，不继承上一轮 action counter。"""

    return {
        "current_action_index": 0,
        "pending_executor_action": None,
        "pending_action": None,
        "recovery_issue": None,
        "step_outcome": None,
        "status": TaskStatus.RUNNING,
        "error": None,
    }


def advance_step_action_level(state: TaskState) -> TaskStateUpdate:
    """复用 Step 推进逻辑，并为下一 Step 重置 action-level 字段。"""

    update = advance_step(state)
    update["current_action_index"] = 0
    update["pending_executor_action"] = None
    update["pending_action"] = None
    update["recovery_issue"] = None
    return update


def _route_after_decision(
    state: TaskState,
) -> Literal["assess_policy", "build_step_outcome", "verify_step", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    action = state.get("pending_executor_action")
    if action is None:
        return "verify_step"
    return (
        "build_step_outcome"
        if action.action_type is ExecutorActionType.FINISH_STEP
        else "assess_policy"
    )


def _route_after_policy(
    state: TaskState,
) -> Literal["policy_feedback", "prepare_approval", "execute_tool_action", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    if state.get("pending_action") is not None:
        return "prepare_approval"
    decisions = state.get("policy_decisions", [])
    action = state.get("pending_executor_action")
    if decisions and action is not None and decisions[-1].call_id == action.call_id:
        if decisions[-1].outcome is PolicyOutcome.DENY:
            return "policy_feedback"
    return "execute_tool_action"


def _route_after_tool_execution(
    state: TaskState,
) -> Literal["decide_action", "recovery_review", "end"]:
    if state["status"] is TaskStatus.INTERRUPTED:
        return "recovery_review"
    if state["status"] is TaskStatus.FAILED:
        return "end"
    return "decide_action"


def _route_after_recovery(
    state: TaskState,
) -> Literal["execute_tool_action", "end"]:
    return "end" if state["status"] is TaskStatus.FAILED else "execute_tool_action"


def _route_after_action_verification(
    state: TaskState,
) -> Literal["advance_step", "reset_step_attempt", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    result = state.get("step_verification")
    return "advance_step" if result is not None and result.verified else "reset_step_attempt"


def _route_after_action_advance(
    state: TaskState,
) -> Literal["decide_action", "verify_task", "end"]:
    if state["status"] is TaskStatus.FAILED:
        return "end"
    return "verify_task" if state["current_step_id"] is None else "decide_action"


def _build_action_level_graph(
    *,
    analyzer: TaskAnalyzerLike,
    planner: TaskPlannerLike,
    executor: TaskExecutor,
    verifier: TaskVerifierLike,
    max_step_attempts: int,
    checkpointer: BaseCheckpointSaver[Any] | None,
    journal: ActionJournalLike,
    fault_injector: ExecutionFaultInjector,
) -> CompiledStateGraph:
    builder = StateGraph(TaskState)
    builder.add_node("initialize_task", initialize_task)
    builder.add_node("analyze_task", partial(analyze_task, analyzer=analyzer))
    builder.add_node("plan_task", partial(plan_task, planner=planner))
    builder.add_node("begin_step", begin_step)
    builder.add_node("decide_action", partial(decide_action, executor=executor))
    builder.add_node("assess_policy", partial(assess_policy, executor=executor))
    builder.add_node("policy_feedback", policy_feedback)
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("human_approval", human_approval)
    builder.add_node("rejection_feedback", rejection_feedback)
    builder.add_node(
        "execute_tool_action",
        partial(
            execute_tool_action,
            executor=executor,
            journal=journal,
            fault_injector=fault_injector,
        ),
    )
    builder.add_node("recovery_review", recovery_review)
    builder.add_node("build_step_outcome", build_step_outcome)
    builder.add_node(
        "verify_step",
        partial(verify_step, verifier=verifier, max_step_attempts=max_step_attempts),
    )
    builder.add_node("reset_step_attempt", reset_step_attempt)
    builder.add_node("advance_step", advance_step_action_level)
    builder.add_node("verify_task", partial(verify_task, verifier=verifier))

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "analyze_task")
    builder.add_edge("analyze_task", "plan_task")
    builder.add_conditional_edges(
        "plan_task", _route_after_plan, {"execute_step": "begin_step", "end": END}
    )
    builder.add_edge("begin_step", "decide_action")
    builder.add_conditional_edges(
        "decide_action",
        _route_after_decision,
        {
            "assess_policy": "assess_policy",
            "build_step_outcome": "build_step_outcome",
            "verify_step": "verify_step",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "assess_policy",
        _route_after_policy,
        {
            "policy_feedback": "policy_feedback",
            "prepare_approval": "prepare_approval",
            "execute_tool_action": "execute_tool_action",
            "end": END,
        },
    )
    builder.add_edge("policy_feedback", "decide_action")
    builder.add_edge("prepare_approval", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        _route_after_human_approval,
        {
            "execute_approved_action": "execute_tool_action",
            "handle_rejection": "rejection_feedback",
            "end": END,
        },
    )
    builder.add_edge("rejection_feedback", "decide_action")
    builder.add_conditional_edges(
        "execute_tool_action",
        _route_after_tool_execution,
        {"decide_action": "decide_action", "recovery_review": "recovery_review", "end": END},
    )
    builder.add_conditional_edges(
        "recovery_review",
        _route_after_recovery,
        {"execute_tool_action": "execute_tool_action", "end": END},
    )
    builder.add_edge("build_step_outcome", "verify_step")
    builder.add_conditional_edges(
        "verify_step",
        _route_after_action_verification,
        {"advance_step": "advance_step", "reset_step_attempt": "reset_step_attempt", "end": END},
    )
    builder.add_edge("reset_step_attempt", "decide_action")
    builder.add_conditional_edges(
        "advance_step",
        _route_after_action_advance,
        {"decide_action": "decide_action", "verify_task": "verify_task", "end": END},
    )
    builder.add_edge("verify_task", END)
    return builder.compile(checkpointer=checkpointer)


def build_graph(
    analyzer: TaskAnalyzerLike | None = None,
    planner: TaskPlannerLike | None = None,
    executor: TaskExecutorLike | None = None,
    verifier: TaskVerifierLike | None = None,
    max_step_attempts: int = 3,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    action_journal: ActionJournalLike | None = None,
    fault_injector: ExecutionFaultInjector | None = None,
) -> CompiledStateGraph:
    """构建 Graph；真实 TaskExecutor 使用 action-level durable execution。"""

    if max_step_attempts < 1:
        raise ValueError("max_step_attempts 必须至少为 1")
    active_analyzer = analyzer if analyzer is not None else TaskAnalyzer()
    active_planner = planner if planner is not None else TaskPlanner()
    active_executor = executor if executor is not None else TaskExecutor(ToolRegistry())
    active_verifier = verifier if verifier is not None else TaskVerifier()
    if isinstance(active_executor, TaskExecutor):
        return _build_action_level_graph(
            analyzer=active_analyzer,
            planner=active_planner,
            executor=active_executor,
            verifier=active_verifier,
            max_step_attempts=max_step_attempts,
            checkpointer=checkpointer,
            journal=action_journal or InMemoryActionJournal(),
            fault_injector=fault_injector or NoOpExecutionFaultInjector(),
        )
    # Phase 1–8 的离线 Fake 只实现 execute；保留兼容图，不用于生产 CLI。
    return _build_legacy_graph(
        analyzer=active_analyzer,
        planner=active_planner,
        executor=active_executor,
        verifier=active_verifier,
        max_step_attempts=max_step_attempts,
        checkpointer=checkpointer,
    )
```

### src/taskpilot/cli.py

```python
"""TaskPilot 的 async Host/CLI 入口。"""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from langgraph.types import Command

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp.config import load_mcp_server_configs
from taskpilot.mcp.provider import MCPToolProvider
from taskpilot.models import TaskStatus
from taskpilot.persistence import (
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


def create_initial_state(user_input: str) -> TaskState:
    """根据用户输入创建结构化初始任务状态。"""

    return {
        "task_id": str(uuid4()),
        "user_input": user_input,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "pending_action": None,
        "policy_decisions": [],
        "approval_records": [],
        "current_action_index": 0,
        "pending_executor_action": None,
        "recovery_issue": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


async def async_main(argv: Sequence[str] | None = None) -> int:
    """在可选 MCP/Browser 生命周期内异步运行一次 TaskPilot Graph。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot graph.")
    parser.add_argument("task", nargs="?", help="Task description for a new task")
    parser.add_argument(
        "--resume",
        metavar="TASK_ID",
        help="Resume an existing durable LangGraph thread",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".taskpilot"),
        help="Directory containing checkpoints.sqlite3 and actions.sqlite3",
    )
    parser.add_argument(
        "--mcp-config",
        type=Path,
        help="JSON file containing MCP server configurations",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Enable the Playwright Chromium Browser Tools",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chromium window (requires --browser)",
    )
    args = parser.parse_args(argv)

    if args.headed and not args.browser:
        parser.error("--headed requires --browser")
    if (args.task is None) == (args.resume is None):
        parser.error("provide exactly one of TASK or --resume TASK_ID")

    registry = ToolRegistry()
    initial_state = create_initial_state(args.task) if args.task is not None else None
    task_id = initial_state["task_id"] if initial_state is not None else args.resume
    assert task_id is not None
    persistence_config = PersistenceConfig.from_state_dir(args.state_dir)
    # Host 持有所有外部资源；Graph 只接收已配置好的 Executor。
    async with AsyncExitStack() as stack:
        if args.mcp_config is not None:
            configs = load_mcp_server_configs(args.mcp_config)
            provider = await stack.enter_async_context(MCPToolProvider(configs))
            await provider.register_tools(registry)
        if args.browser:
            browser_provider = await stack.enter_async_context(
                BrowserToolProvider(BrowserConfig(headless=not args.headed))
            )
            browser_provider.register_tools(registry)

        checkpoint_provider = await stack.enter_async_context(
            SQLiteCheckpointProvider(persistence_config)
        )
        action_journal = await stack.enter_async_context(
            SQLiteActionJournal(persistence_config.action_journal_db_path)
        )
        if checkpoint_provider.checkpointer is None:  # pragma: no cover - lifecycle 防御
            raise RuntimeError("SQLite checkpointer 未初始化")

        # 真实外部 Tool 默认经过 Policy；runtime provider 每次启动都重新构造。
        policy = DefaultRiskPolicy() if (args.browser or args.mcp_config) else None
        executor = TaskExecutor(registry=registry, policy=policy)
        graph = build_graph(
            executor=executor,
            checkpointer=checkpoint_provider.checkpointer,
            action_journal=action_journal,
        )
        graph_config = {"configurable": {"thread_id": task_id}}
        if initial_state is None:
            snapshot = await graph.aget_state(graph_config)
            if not snapshot.values:
                print(f"error=no durable checkpoint found for task_id={task_id}")
                return 1
            result = dict(snapshot.values)
            restored_status = result.get("status")
            if restored_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED.value}:
                print("task already completed")
            elif (
                restored_status in {TaskStatus.FAILED, TaskStatus.FAILED.value}
                and not snapshot.next
            ):
                print("task already failed; automatic restart is disabled")
            # 非 interrupt 的 crash 会留下 next node；None 表示从 durable position 续跑。
            if snapshot.next and not any(
                item for task in snapshot.tasks for item in task.interrupts
            ):
                result = await graph.ainvoke(None, config=graph_config)
        else:
            result = await graph.ainvoke(initial_state, config=graph_config)
        snapshot = await graph.aget_state(graph_config)
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        while interrupts:
            payload = interrupts[0].value
            print(f"interrupt_type={payload['type']}")
            print(f"approval_tool={payload['tool_name']}")
            print(f"approval_reason={payload['reason']}")
            print(
                "approval_arguments_preview="
                + json.dumps(
                    payload["arguments_preview"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            if payload["type"] == "tool_approval":
                print(f"approval_risk={payload['risk_level']}")
                answer = await asyncio.to_thread(input, "Approve? [y/N] ")
                resume = (
                    {"decision": "approve"}
                    if answer.strip().lower() == "y"
                    else {"decision": "reject", "reason": "Rejected from CLI"}
                )
            elif payload["type"] == "ambiguous_tool_recovery":
                answer = await asyncio.to_thread(
                    input,
                    "Action may already have run. Retry anyway? [y/N] ",
                )
                resume = (
                    {"choice": "retry", "reason": "Retry accepted from CLI"}
                    if answer.strip().lower() == "y"
                    else {"choice": "abort", "reason": "Aborted from CLI"}
                )
            else:
                print(f"error=unsupported interrupt type: {payload['type']}")
                return 1
            result = await graph.ainvoke(
                Command(resume=resume),
                config=graph_config,
            )
            snapshot = await graph.aget_state(graph_config)
            interrupts = [
                item
                for task in snapshot.tasks
                for item in task.interrupts
            ]
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    task_spec = result.get("task_spec")
    if task_spec is not None:
        print(f"goal={task_spec.goal}")
        print(
            "constraints="
            + json.dumps(task_spec.constraints, ensure_ascii=False, sort_keys=True)
        )
        print(f"expected_output={task_spec.expected_output}")
        print(
            "completion_criteria="
            + json.dumps(task_spec.completion_criteria, ensure_ascii=False)
        )
    print("plan:")
    for step in result.get("plan", []):
        print(f"  step_id={step.id}")
        print(f"    description={step.description}")
        print(f"    depends_on={json.dumps(step.depends_on)}")
        print(
            "    success_criteria="
            + json.dumps(step.success_criteria, ensure_ascii=False)
        )
        print(f"    status={step.status.value}")
    print(f"current_step_id={result.get('current_step_id')}")
    step_outcome = result.get("step_outcome")
    if step_outcome is not None:
        print(f"claimed_complete={step_outcome.claimed_complete}")
        print(f"action_count={step_outcome.action_count}")
        print(f"execution_summary={step_outcome.summary}")
        print(
            "execution_evidence="
            + json.dumps(step_outcome.evidence, ensure_ascii=False)
        )
        print(
            "execution_output="
            + json.dumps(step_outcome.output, ensure_ascii=False)
        )
        if step_outcome.error:
            print(f"execution_error={step_outcome.error}")
    print(
        "step_results="
        + json.dumps(
            [item.model_dump(mode="json") for item in result.get("step_results", [])],
            ensure_ascii=False,
        )
    )
    verification = result.get("verification")
    if verification is not None:
        print(f"task_verified={verification.completed}")
        print(f"verification_reason={verification.reason}")
    if result.get("error"):
        print(f"error={result['error']}")
        return 1
    if step_outcome is not None and step_outcome.error:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 顶层创建一次事件循环；Graph node 内不会创建 event loop。"""

    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
```

### src/taskpilot/browser/tools.py

```python
"""DOM-first Playwright Browser Tools 与长生命周期 Provider。"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from taskpilot.browser.config import BrowserConfig
from taskpilot.browser.locators import LOCATOR_JSON_SCHEMA, LocatorSpec, resolve_locator
from taskpilot.browser.session import (
    BrowserDownloadError,
    BrowserError,
    BrowserLocatorError,
    BrowserNavigationError,
    BrowserSession,
    concise_playwright_error,
)
from taskpilot.tools import ToolRegistry


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_LOCATOR_PRIORITY = (
    "Prefer role + accessible name, then label, test_id, text/placeholder, "
    "and use CSS only as a fallback. Strict matching is preserved."
)


@dataclass
class BrowserTool:
    """把一个 Browser handler 暴露为统一 async Tool。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    _handler: Callable[[dict[str, Any]], Awaitable[Any]]
    metadata: dict[str, Any] = field(default_factory=dict)
    _preflight: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return await self._handler(arguments)

    async def preflight(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """只读取 dynamic action metadata，不执行目标动作。"""

        if self._preflight is None:
            raise BrowserError(f"Browser Tool {self.name} 没有 dynamic preflight")
        return await self._preflight(arguments)


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


class BrowserTools:
    """BrowserSession 上的 JSON-serializable Tool handlers。"""

    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self._screenshot_number = 1

    def build(self) -> list[BrowserTool]:
        page_id = {"type": "string", "minLength": 1}
        locator = LOCATOR_JSON_SCHEMA
        return [
            BrowserTool(
                "browser_navigate",
                "Navigate an existing page to an HTTP(S) URL.",
                _object_schema(
                    {"url": {"type": "string", "minLength": 1}, "page_id": page_id},
                    ["url"],
                ),
                self.navigate,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_observe",
                "Return an AI-mode ARIA snapshot, URL, title and stable page IDs.",
                _object_schema(
                    {
                        "page_id": page_id,
                        "depth": {"type": "integer", "minimum": 1, "maximum": 30},
                    }
                ),
                self.observe,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_click",
                f"Click one strict semantic locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "page_id": page_id,
                        "expect_popup": {"type": "boolean", "default": False},
                    },
                    ["locator"],
                ),
                self.click,
                metadata={"source": "browser", "risk": "dynamic", "replay_safe": False},
                _preflight=self.inspect_click,
            ),
            BrowserTool(
                "browser_fill",
                f"Fill one strict textbox locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "value": {"type": "string"},
                        "page_id": page_id,
                    },
                    ["locator", "value"],
                ),
                self.fill,
                metadata={"source": "browser", "risk": "medium", "replay_safe": True},
            ),
            BrowserTool(
                "browser_select",
                f"Select one option by value on a strict locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "value": {"type": "string"},
                        "page_id": page_id,
                    },
                    ["locator", "value"],
                ),
                self.select,
                metadata={"source": "browser", "risk": "medium", "replay_safe": True},
            ),
            BrowserTool(
                "browser_check",
                f"Check one checkbox/radio locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.check,
                metadata={"source": "browser", "risk": "medium", "replay_safe": True},
            ),
            BrowserTool(
                "browser_extract_text",
                f"Extract bounded text from one locator or body. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "page_id": page_id,
                        "max_chars": {"type": "integer", "minimum": 1},
                    }
                ),
                self.extract_text,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_list_pages",
                "List stable page IDs with active state, title and URL.",
                _object_schema({}),
                self.list_pages,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_switch_page",
                "Switch the active page using a stable page_id.",
                _object_schema({"page_id": page_id}, ["page_id"]),
                self.switch_page,
                metadata={"source": "browser", "risk": "low", "replay_safe": False},
            ),
            BrowserTool(
                "browser_download",
                f"Click one locator and save its download only in the controlled directory. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.download,
                metadata={"source": "browser", "risk": "medium", "replay_safe": False},
            ),
            BrowserTool(
                "browser_screenshot",
                "Save a system-named PNG in the controlled artifact directory; no Vision call.",
                _object_schema(
                    {
                        "page_id": page_id,
                        "full_page": {"type": "boolean", "default": False},
                    }
                ),
                self.screenshot,
                metadata={"source": "browser", "risk": "medium", "replay_safe": False},
            ),
        ]

    async def navigate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        url = arguments["url"]
        if urlsplit(url).scheme.lower() not in {"http", "https"}:
            raise BrowserNavigationError(
                f"browser_navigate 拒绝非 HTTP(S) URL: {urlsplit(url).scheme or 'missing'}"
            )
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.session.config.navigation_timeout_ms,
            )
            return {
                "page_id": page_id,
                "final_url": page.url,
                "title": await page.title(),
                "status": response.status if response is not None else None,
            }
        except PlaywrightError as exc:
            raise BrowserNavigationError(
                f"browser_navigate failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc

    async def observe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        depth = arguments.get("depth", self.session.config.default_snapshot_depth)
        try:
            snapshot = await page.aria_snapshot(mode="ai", depth=depth)
        except PlaywrightError as exc:
            raise BrowserError(
                f"browser_observe failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        original_length = len(snapshot)
        truncated = original_length > self.session.config.max_snapshot_chars
        if truncated:
            snapshot = snapshot[: self.session.config.max_snapshot_chars]
        return {
            "page_id": page_id,
            "url": page.url,
            "title": await page.title(),
            "aria_snapshot": snapshot,
            "snapshot_mode": "ai",
            "depth": depth,
            "truncated": truncated,
            "original_length": original_length,
            "pages": await self.session.list_pages(),
        }

    async def click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "click")
        try:
            if arguments.get("expect_popup", False):
                async with page.expect_popup() as popup_info:
                    await locator.click()
                popup = await popup_info.value
                await popup.wait_for_load_state("domcontentloaded")
                new_page_id = self.session.register_page(popup)
            else:
                await locator.click()
                new_page_id = None
            return {
                "clicked": True,
                "page_id": page_id,
                "current_url": page.url,
                "new_page_id": new_page_id,
            }
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_click", page.url, spec, exc) from exc

    async def fill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "fill")
        try:
            await locator.fill(arguments["value"])
            return {"filled": True, "page_id": page_id, "value": arguments["value"]}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_fill", page.url, spec, exc) from exc

    async def select(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "select")
        try:
            selected = await locator.select_option(value=arguments["value"])
            return {"selected": selected, "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_select", page.url, spec, exc) from exc

    async def check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "check")
        try:
            await locator.check()
            return {"checked": await locator.is_checked(), "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error("browser_check", page.url, spec, exc) from exc

    async def extract_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        try:
            if "locator" in arguments:
                _, locator = self._action_locator(
                    page, arguments["locator"], "extract_text"
                )
                text = await locator.inner_text()
            else:
                text = await page.locator("body").inner_text()
        except PlaywrightError as exc:
            raise BrowserLocatorError(
                f"browser_extract_text failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        limit = min(
            arguments.get("max_chars", self.session.config.max_extract_chars),
            self.session.config.max_extract_chars,
        )
        original_length = len(text)
        return {
            "page_id": page_id,
            "text": text[:limit],
            "truncated": original_length > limit,
            "original_length": original_length,
        }

    async def list_pages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"pages": await self.session.list_pages()}

    async def switch_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page = self.session.switch_page(arguments["page_id"])
        await page.bring_to_front()
        return {
            "page_id": arguments["page_id"],
            "active": True,
            "url": page.url,
            "title": await page.title(),
        }

    async def download(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "download")
        try:
            async with page.expect_download() as download_info:
                await locator.click()
            download = await download_info.value
            suggested = download.suggested_filename
            filename = self._sanitize_filename(suggested)
            destination = self._unique_path(self.session.config.download_dir, filename)
            await download.save_as(destination)
            return {
                "filename": destination.name,
                "saved_path": str(destination),
                "suggested_filename": suggested,
                "page_id": page_id,
            }
        except BrowserDownloadError:
            raise
        except PlaywrightError as exc:
            raise BrowserDownloadError(
                f"browser_download failed; page_url={page.url}; "
                f"strategy={spec.strategy.value}; detail={concise_playwright_error(exc)}"
            ) from exc

    async def screenshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        filename = f"screenshot_{self._screenshot_number:04d}.png"
        self._screenshot_number += 1
        destination = self._unique_path(self.session.config.artifact_dir, filename)
        try:
            await page.screenshot(
                path=str(destination),
                full_page=arguments.get("full_page", False),
            )
        except PlaywrightError as exc:
            raise BrowserError(
                f"browser_screenshot failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        return {"saved_path": str(destination), "page_id": page_id, "url": page.url}

    async def inspect_click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """等待目标出现后只读取 click 风险 metadata，不触发 click。"""

        _, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(
            page,
            arguments["locator"],
            "click_preflight",
        )
        try:
            # preflight 可以等待 attached；普通 action 不提前 count，保留 auto-wait。
            await locator.wait_for(state="attached")
            count = await locator.count()
            if count != 1:
                raise BrowserLocatorError(
                    "browser_click preflight 必须唯一定位目标; "
                    f"page_url={page.url}; strategy={spec.strategy.value}; count={count}"
                )
            metadata = await locator.evaluate(
                """element => {
                    const tag = element.tagName.toLowerCase();
                    const form = element.closest("form");
                    const typeValue = "type" in element
                        ? String(element.type || "").toLowerCase()
                        : String(element.getAttribute("type") || "").toLowerCase();
                    const isSubmit =
                        (tag === "button" && typeValue === "submit") ||
                        (tag === "input" && ["submit", "image"].includes(typeValue));
                    return {
                        tag_name: tag,
                        input_type: typeValue,
                        text: String(element.innerText || element.textContent || "").trim().slice(0, 500),
                        value: String(element.value || "").slice(0, 500),
                        aria_label: element.getAttribute("aria-label"),
                        href: tag === "a" ? String(element.href || "") : null,
                        in_form: form !== null,
                        form_method: form ? String(form.method || "get").toLowerCase() : null,
                        is_submit: isSubmit,
                    };
                }"""
            )
        except BrowserLocatorError:
            raise
        except PlaywrightError as exc:
            raise BrowserLocatorError(
                "browser_click preflight failed; "
                f"page_url={page.url}; strategy={spec.strategy.value}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        metadata["accessible_name"] = spec.name
        metadata["page_url"] = page.url
        metadata["strategy"] = spec.strategy.value
        return metadata

    def _action_locator(
        self,
        page: Page,
        raw_spec: dict[str, Any],
        action: str,
    ) -> tuple[LocatorSpec, Locator]:
        try:
            spec = LocatorSpec.model_validate(raw_spec)
            locator = resolve_locator(page, spec)
        except Exception as exc:
            if isinstance(exc, BrowserError):
                raise
            raise BrowserLocatorError(
                f"browser_{action} locator invalid; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        return spec, locator

    @staticmethod
    def _locator_action_error(
        action: str,
        url: str,
        spec: LocatorSpec,
        exc: Exception,
    ) -> BrowserLocatorError:
        return BrowserLocatorError(
            f"{action} failed; page_url={url}; strategy={spec.strategy.value}; "
            f"detail={concise_playwright_error(exc)}"
        )

    @staticmethod
    def _sanitize_filename(suggested: str) -> str:
        basename = Path(suggested).name
        sanitized = _SAFE_FILENAME.sub("_", basename).strip("._")
        if not sanitized or sanitized in {".", ".."}:
            raise BrowserDownloadError("下载 suggested_filename 无法安全保存")
        return sanitized

    @staticmethod
    def _unique_path(directory: Path, filename: str) -> Path:
        directory = directory.resolve()
        candidate = (directory / filename).resolve()
        if candidate.parent != directory:
            raise BrowserDownloadError("Browser artifact path 超出受控目录")
        counter = 1
        while candidate.exists():
            candidate = directory / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
            counter += 1
        return candidate


class BrowserToolProvider:
    """Host 侧 BrowserSession lifecycle 与 Tool 注册，不参与推理。"""

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self.session = BrowserSession(self.config)
        self._tools: list[BrowserTool] = []

    async def __aenter__(self) -> "BrowserToolProvider":
        await self.session.__aenter__()
        self._tools = BrowserTools(self.session).build()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self._tools = []
        await self.session.__aexit__(exc_type, exc, traceback)

    def register_tools(self, registry: ToolRegistry) -> list[BrowserTool]:
        if not self.session.is_started:
            raise BrowserError("BrowserToolProvider 必须先进入 async context")
        existing = {tool.name for tool in registry.list_tools()}
        conflicts = sorted(tool.name for tool in self._tools if tool.name in existing)
        if conflicts:
            raise BrowserError(f"Browser Tool 与 Registry 名称冲突: {conflicts}")
        for tool in self._tools:
            registry.register(tool)
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]
```

### src/taskpilot/mcp/provider.py

```python
"""多个 MCP Server 的长生命周期连接、发现与注册。"""

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams, Tool as MCPTool

from taskpilot.config import redact_sensitive_values
from taskpilot.mcp.adapter import MCPToolAdapter, build_public_tool_name
from taskpilot.mcp.config import MCPServerConfig, MCPTransport
from taskpilot.tools import ToolRegistry


class MCPConfigurationError(ValueError):
    """MCP 配置、namespace 或注册冲突。"""


class MCPConnectionError(RuntimeError):
    """MCP transport 建连或 initialize 失败。"""


@dataclass(frozen=True)
class MCPDiscoveryInfo:
    """供检查与测试使用的发现映射，不参与 Tool 选择。"""

    server_id: str
    remote_tool_name: str
    public_tool_name: str


class MCPToolProvider:
    """Host 侧 MCP 连接基础设施；不参与 Graph、Planner 或 Verifier。"""

    def __init__(self, configs: Sequence[MCPServerConfig]) -> None:
        self._configs = list(configs)
        self._validate_server_ids()
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._adapters: list[MCPToolAdapter] = []
        self._discovery_info: list[MCPDiscoveryInfo] = []

    @property
    def connected_server_ids(self) -> list[str]:
        """返回当前仍处于 provider lifecycle 内的 server IDs。"""

        return list(self._sessions)

    @property
    def discovery_info(self) -> list[MCPDiscoveryInfo]:
        """返回动态发现产生的 namespace 映射副本。"""

        return list(self._discovery_info)

    async def __aenter__(self) -> "MCPToolProvider":
        """一次连接并 initialize 所有配置的 MCP Servers。"""

        if self._stack is not None:
            raise RuntimeError("MCPToolProvider 不能重复进入 context")
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for config in self._configs:
                try:
                    await self._connect(config)
                except Exception as exc:
                    detail = redact_sensitive_values(str(exc))
                    for secret in config.resolved_env().values():
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    raise MCPConnectionError(
                        f"MCP Server {config.server_id} 连接或 initialize 失败: "
                        f"{type(exc).__name__}: {detail}"
                    ) from exc
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            self._sessions.clear()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool | None:
        """按 LIFO 顺序统一关闭 session 与 transport/subprocess。"""

        stack = self._stack
        self._stack = None
        self._sessions.clear()
        if stack is None:
            return None
        return await stack.__aexit__(exc_type, exc, traceback)

    async def discover_tools(self) -> list[MCPToolAdapter]:
        """完整分页调用 list_tools，并创建 TaskPilot adapters。"""

        if self._stack is None:
            raise RuntimeError("discover_tools 必须在 MCPToolProvider context 内调用")
        if self._adapters:
            return list(self._adapters)

        adapters: list[MCPToolAdapter] = []
        public_names: set[str] = set()
        discovery_info: list[MCPDiscoveryInfo] = []
        for config in self._configs:
            session = self._sessions[config.server_id]
            for remote_tool in await self._list_all_tools(session):
                public_name = build_public_tool_name(
                    config.server_id,
                    remote_tool.name,
                )
                if public_name in public_names:
                    raise MCPConfigurationError(
                        "MCP Tool namespace 冲突: "
                        f"public_tool_name={public_name}"
                    )
                public_names.add(public_name)
                adapters.append(
                    self._make_adapter(
                        config,
                        public_name,
                        remote_tool,
                        session,
                    )
                )
                discovery_info.append(
                    MCPDiscoveryInfo(
                        server_id=config.server_id,
                        remote_tool_name=remote_tool.name,
                        public_tool_name=public_name,
                    )
                )

        self._adapters = adapters
        self._discovery_info = discovery_info
        return list(adapters)

    async def register_tools(self, registry: ToolRegistry) -> list[MCPToolAdapter]:
        """发现并注册 adapters；不会静默覆盖 Local 或其他 MCP Tool。"""

        adapters = await self.discover_tools()
        existing_names = {tool.name for tool in registry.list_tools()}
        conflicts = sorted(
            adapter.name for adapter in adapters if adapter.name in existing_names
        )
        if conflicts:
            raise MCPConfigurationError(
                f"MCP Tool 与现有 Registry 名称冲突: {conflicts}"
            )
        for adapter in adapters:
            registry.register(adapter)
        return adapters

    async def _connect(self, config: MCPServerConfig) -> None:
        """根据当前 MCP SDK v2 transport API 建立并初始化 session。"""

        if self._stack is None:  # pragma: no cover - 仅防御错误内部调用
            raise RuntimeError("MCPToolProvider context 尚未进入")
        if config.transport is MCPTransport.STDIO:
            parameters = StdioServerParameters(
                command=config.command or "",
                args=config.args,
                env=config.resolved_env(),
            )
            streams = await self._stack.enter_async_context(
                stdio_client(parameters)
            )
        else:
            streams = await self._stack.enter_async_context(
                streamable_http_client(config.url or "")
            )
        session = await self._stack.enter_async_context(ClientSession(*streams))
        await session.initialize()
        self._sessions[config.server_id] = session

    @staticmethod
    async def _list_all_tools(session: ClientSession) -> list[MCPTool]:
        """跟随 next_cursor 直到 MCP Server 的最后一页。"""

        tools: list[MCPTool] = []
        cursor: str | None = None
        while True:
            params = (
                PaginatedRequestParams(cursor=cursor)
                if cursor is not None
                else None
            )
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tools

    @staticmethod
    def _make_adapter(
        config: MCPServerConfig,
        public_name: str,
        remote_tool: MCPTool,
        session: ClientSession,
    ) -> MCPToolAdapter:
        """保留 SDK 实际暴露的 metadata，不执行 Phase 8 风险策略。"""

        annotations = (
            remote_tool.annotations.model_dump(mode="json", by_alias=False)
            if remote_tool.annotations is not None
            else None
        )
        # 只有 Host 信任 annotations 时，远端 read-only/idempotent hint 才能
        # 降低重放风险；不可信或缺失 annotation 一律保守为 False。
        replay_safe = bool(
            config.trust_tool_annotations
            and isinstance(annotations, dict)
            and (
                annotations.get("read_only_hint") is True
                or annotations.get("idempotent_hint") is True
            )
        )
        metadata = {
            "source": "mcp",
            "trust_tool_annotations": config.trust_tool_annotations,
            "replay_safe": replay_safe,
            "title": remote_tool.title,
            "annotations": annotations,
            "output_schema": remote_tool.output_schema,
            "icons": (
                [icon.model_dump(mode="json", by_alias=False) for icon in remote_tool.icons]
                if remote_tool.icons is not None
                else None
            ),
            "execution": (
                remote_tool.execution.model_dump(mode="json", by_alias=False)
                if remote_tool.execution is not None
                else None
            ),
            "meta": remote_tool.meta,
        }
        return MCPToolAdapter(
            name=public_name,
            remote_tool_name=remote_tool.name,
            server_id=config.server_id,
            description=remote_tool.description or remote_tool.title or "",
            input_schema=remote_tool.input_schema,
            session=session,
            metadata=metadata,
        )

    def _validate_server_ids(self) -> None:
        server_ids = [config.server_id for config in self._configs]
        if len(server_ids) != len(set(server_ids)):
            raise MCPConfigurationError("MCP server_id 必须唯一")
        namespace_ids = [
            build_public_tool_name(config.server_id, "tool").rsplit("__", 1)[0]
            for config in self._configs
        ]
        if len(namespace_ids) != len(set(namespace_ids)):
            raise MCPConfigurationError(
                "MCP server_id 清理后产生 namespace 冲突"
            )
```


以上为 Phase 9 实质修改的完整核心源码。

## 19. Full Relevant Phase 9 Tests

+### tests/test_phase9_persistence.py

```python
"""Phase 9 SQLite checkpoint、journal 与 metadata 的离线测试。"""

from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph

from taskpilot.models import (
    ExecutorAction,
    ExecutorActionType,
    PlanStep,
    StepResult,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from taskpilot.browser.tools import BrowserTools
from taskpilot.persistence import (
    ActionJournalRecord,
    JournalStatus,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.persistence.journal import utc_now
from taskpilot.policy import (
    ApprovalChoice,
    ApprovalRecord,
    PendingToolAction,
    PolicyDecision,
    PolicyOutcome,
    RiskLevel,
)
from taskpilot.state import TaskState


def complete_state() -> TaskState:
    decision = PolicyDecision(
        call_id="call-1",
        step_id=1,
        tool_name="local_read",
        risk_level=RiskLevel.HIGH,
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        reason="offline fixture",
        tool_source="local",
        preview={"query": "safe"},
    )
    return {
        "task_id": "round-trip-task",
        "user_input": "持久化完整状态",
        "task_spec": TaskSpec(
            goal="持久化完整状态",
            constraints={"offline": True},
            completion_criteria=["类型可恢复"],
        ),
        "plan": [PlanStep(id=1, description="测试", success_criteria=["通过"])],
        "current_step_id": 1,
        "step_outcome": None,
        "step_results": [StepResult(step_id=1, summary="已有结果")],
        "step_verification": None,
        "tool_calls": [
            ToolCallRecord(
                call_id="old-call",
                step_id=1,
                tool_name="local_read",
                status=ToolCallStatus.SUCCESS,
                result={"ok": True},
            )
        ],
        "task_results": {"answer": 42},
        "verification": None,
        "pending_action": PendingToolAction(
            call_id="call-1",
            step_id=1,
            tool_name="local_read",
            arguments={"query": "safe"},
            policy_decision=decision,
        ),
        "policy_decisions": [decision],
        "approval_records": [
            ApprovalRecord(
                call_id="call-1",
                step_id=1,
                tool_name="local_read",
                decision=ApprovalChoice.APPROVE,
            )
        ],
        "current_action_index": 1,
        "pending_executor_action": ExecutorAction(
            step_id=1,
            action_index=1,
            call_id="call-1",
            action_type=ExecutorActionType.TOOL,
            tool_name="local_read",
            arguments={"query": "safe"},
        ),
        "recovery_issue": None,
        "status": TaskStatus.WAITING_APPROVAL,
        "error": None,
    }


def checkpoint_graph(checkpointer: Any):
    builder = StateGraph(TaskState)
    builder.add_node("persist", lambda state: {})
    builder.add_edge(START, "persist")
    builder.add_edge("persist", END)
    return builder.compile(checkpointer=checkpointer)


def test_persistence_config_resolves_paths(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path / "state")

    assert config.checkpoint_db_path.is_absolute()
    assert config.action_journal_db_path.is_absolute()
    assert config.checkpoint_db_path.name == "checkpoints.sqlite3"
    assert config.action_journal_db_path.name == "actions.sqlite3"


def test_browser_replay_rules_are_conservative_and_explicit() -> None:
    tools = {tool.name: tool.metadata for tool in BrowserTools(object()).build()}

    assert tools["browser_observe"]["replay_safe"] is True
    assert tools["browser_navigate"]["replay_safe"] is True
    assert tools["browser_fill"]["replay_safe"] is True
    assert tools["browser_click"]["replay_safe"] is False
    assert tools["browser_download"]["replay_safe"] is False
    assert tools["browser_screenshot"]["replay_safe"] is False
    assert tools["browser_switch_page"]["replay_safe"] is False


def test_task_state_schema_contains_no_live_runtime_objects() -> None:
    forbidden = {
        "registry", "browser_session", "page", "mcp_session",
        "llm", "sqlite_connection", "checkpointer",
    }

    assert forbidden.isdisjoint(TaskState.__annotations__)


@pytest.mark.asyncio
async def test_sqlite_checkpoint_round_trip_restores_structured_state(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    graph_config = {"configurable": {"thread_id": "round-trip-task"}}

    async with SQLiteCheckpointProvider(config) as first:
        assert first.checkpointer is not None
        await checkpoint_graph(first.checkpointer).ainvoke(
            complete_state(), config=graph_config
        )
        history = await first.list_task_checkpoints("round-trip-task")
        assert len(history) >= 2

    async with SQLiteCheckpointProvider(config) as second:
        assert second.checkpointer is not None
        snapshot = await checkpoint_graph(second.checkpointer).aget_state(graph_config)

    restored = snapshot.values
    assert isinstance(restored["task_spec"], TaskSpec)
    assert isinstance(restored["plan"][0], PlanStep)
    assert isinstance(restored["step_results"][0], StepResult)
    assert isinstance(restored["tool_calls"][0], ToolCallRecord)
    assert isinstance(restored["policy_decisions"][0], PolicyDecision)
    assert isinstance(restored["pending_action"], PendingToolAction)
    assert isinstance(restored["approval_records"][0], ApprovalRecord)
    assert isinstance(restored["pending_executor_action"], ExecutorAction)
    assert restored["task_id"] == "round-trip-task"


@pytest.mark.asyncio
async def test_sqlite_action_journal_commits_started_retry_and_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    now = utc_now()
    original = ActionJournalRecord(
        action_key="task:1:0:1",
        task_id="task",
        step_id=1,
        retry_count=0,
        action_index=1,
        call_id="call-1",
        tool_name="local_read",
        arguments_hash="abc",
        status=JournalStatus.STARTED,
        replay_safe=True,
        created_at=now,
        updated_at=now,
    )
    async with SQLiteActionJournal(path) as journal:
        await journal.start(original)
    async with SQLiteActionJournal(path) as journal:
        started = await journal.get(original.action_key)
        assert started is not None
        assert started.status is JournalStatus.STARTED
        retried = await journal.retry_started(original.action_key)
        assert retried.attempt_count == 2
        terminal = await journal.succeed(original.action_key, {"ok": True})
        assert terminal.status is JournalStatus.SUCCEEDED
    async with SQLiteActionJournal(path) as journal:
        restored = await journal.get(original.action_key)
        assert restored is not None
        assert restored.result == {"ok": True}
        assert restored.attempt_count == 2
```

### tests/test_phase9_recovery.py

```python
"""Phase 9 action boundary、crash window、recovery 与 durable HITL 测试。"""

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
import aiosqlite
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.types import Command

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.cli import async_main
from taskpilot.graph import build_graph
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.persistence import (
    ExecutionFaultPoint,
    InjectedCrash,
    JournalStatus,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


class FakeFunctionCallingModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.invocations = 0
        self.messages: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations += 1
        self.messages.append(list(messages))
        return self.responses.pop(0)


def tool_call(call_id: str = "tool-1", value: int = 7) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "counter",
            "args": {"value": value},
            "id": call_id,
            "type": "tool_call",
        }],
    )


def finish_call(call_id: str = "finish-2") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": FINISH_STEP_NAME,
            "args": {
                "summary": "durable observation available",
                "evidence": ["counter observation is journaled"],
                "output": {"done": True},
            },
            "id": call_id,
            "type": "tool_call",
        }],
    )


@dataclass
class CountingTool:
    replay_safe: bool
    risk: str = "low"
    calls: list[dict[str, Any]] = field(default_factory=list)
    name: str = "counter"
    description: str = "Count deterministic offline invocations"
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    })

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "source": "local",
            "risk": self.risk,
            "replay_safe": self.replay_safe,
        }

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"call_count": len(self.calls), "value": arguments["value"]}


@dataclass
class FileCounterTool(CountingTool):
    """用临时文件模拟跨 runtime 可观察的外部副作用。"""

    counter_path: Path = Path("counter.txt")
    fail_after_increment: bool = False

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        count = (
            int(self.counter_path.read_text(encoding="utf-8"))
            if self.counter_path.exists()
            else 0
        ) + 1
        self.counter_path.write_text(str(count), encoding="utf-8")
        if self.fail_after_increment:
            raise RuntimeError("controlled terminal failure")
        return {"call_count": count, "value": arguments["value"]}


class FakeAnalyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["finish"])


class FakePlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[PlanStep(
            id=1,
            description="run one durable action",
            success_criteria=["observation exists"],
        )])


class TwoStepPlanner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[
            PlanStep(id=1, description="first", success_criteria=["first done"]),
            PlanStep(
                id=2,
                description="second",
                depends_on=[1],
                success_criteria=["second done"],
            ),
        ])


class FakeVerifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=kwargs["step"].id,
            verified=True,
            checks=[CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="offline accepted",
            )],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="offline complete")


class RejectOnceVerifier(FakeVerifier):
    def __init__(self) -> None:
        self.calls = 0

    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        self.calls += 1
        if self.calls == 1:
            return StepVerificationResult(
                step_id=kwargs["step"].id,
                verified=False,
                checks=[CriterionCheck(
                    criterion_index=0,
                    satisfied=False,
                    reason="need durable evidence",
                )],
                feedback="need durable evidence",
            )
        return super().verify_step(**kwargs)


class CrashOnce:
    def __init__(self, point: ExecutionFaultPoint) -> None:
        self.point = point
        self.fired = False

    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if point is self.point and not self.fired:
            self.fired = True
            raise InjectedCrash(f"{point.value}:{action_key}")


class CrashOnStepTwo(CrashOnce):
    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if ":2:" in action_key:
            super().hit(point, action_key)


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "run durable fixture",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "pending_action": None,
        "policy_decisions": [],
        "approval_records": [],
        "current_action_index": 0,
        "pending_executor_action": None,
        "recovery_issue": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def make_executor(tool: CountingTool, responses: Sequence[AIMessage]) -> TaskExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return TaskExecutor(
        registry=registry,
        model=cast(BaseChatModel, FakeFunctionCallingModel(responses)),
        policy=DefaultRiskPolicy(),
    )


def graph_config(task_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": task_id},
        "recursion_limit": 100,
    }


def interrupts(snapshot: Any) -> list[Any]:
    return [item for task in snapshot.tasks for item in task.interrupts]


@pytest.mark.asyncio
async def test_action_level_graph_runs_one_tool_then_one_finish(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool = CountingTool(replay_safe=True)
    model = FakeFunctionCallingModel([tool_call(), finish_call()])
    registry = ToolRegistry()
    registry.register(tool)
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=executor,
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(initial_state("action-loop"), config=graph_config("action-loop"))

    nodes = set(graph.get_graph().nodes)
    assert {"decide_action", "execute_tool_action", "build_step_outcome"} <= nodes
    assert model.invocations == 2
    assert len(tool.calls) == 1
    assert len(result["tool_calls"]) == 1
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_risk_level_and_replay_safe_are_independent() -> None:
    tool = CountingTool(replay_safe=True, risk="high")
    executor = make_executor(tool, [finish_call()])
    decision_result = await executor.decide_action(
        task_spec=TaskSpec(goal="independence"),
        step=PlanStep(id=1, description="check", success_criteria=["checked"]),
        action_index=1,
        task_results={},
        tool_calls=[],
    )
    # 上面的模型返回 finish，因此另造 frozen Tool action 更直接验证两条轴。
    from taskpilot.models import ExecutorAction, ExecutorActionType

    action_model = ExecutorAction(
        step_id=1,
        action_index=1,
        call_id="independent",
        action_type=ExecutorActionType.TOOL,
        tool_name="counter",
        arguments={"value": 7},
    )
    policy_decision = await executor.assess_action(action_model)

    assert decision_result.action is not None
    assert executor.is_replay_safe(action_model) is True
    assert policy_decision is not None
    assert policy_decision.outcome.value == "require_approval"


@pytest.mark.asyncio
async def test_terminal_journal_deduplicates_after_graph_checkpoint_crash(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "terminal-dedup"
    counter_path = tmp_path / "terminal-counter.txt"
    tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    rebuilt_tool = FileCounterTool(replay_safe=False, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None and record.status is JournalStatus.SUCCEEDED
        assert len(tool.calls) == 1

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert len(tool.calls) == 1
    assert rebuilt_tool.calls == []
    assert counter_path.read_text(encoding="utf-8") == "1"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0].result == {"call_count": 1, "value": 7}
    assert result["status"] is TaskStatus.COMPLETED
    print("TERMINAL_JOURNAL_CRASH_SMOKE=" + json.dumps({
        "tool_counter_before": 0,
        "tool_counter_after_execution": 1,
        "injected_crash": True,
        "journal_status": "succeeded",
        "tool_reinvoked": False,
        "tool_counter_final": 1,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_replay_safe_started_is_automatically_retried(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "safe-started"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert tool.calls == []

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")

    assert len(tool.calls) == 1
    assert record is not None and record.attempt_count == 2
    assert result["status"] is TaskStatus.COMPLETED
    print("REPLAY_SAFE_RECOVERY_SMOKE=" + json.dumps({
        "journal_status_before": "started",
        "automatic_retry": True,
        "recovery_interrupt": False,
        "attempt_count": record.attempt_count,
        "tool_counter_final": len(tool.calls),
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_unsafe_started_interrupts_and_abort_does_not_replay(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-abort"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert len(tool.calls) == 1

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        pending_interrupts = interrupts(snapshot)
        assert paused["status"] is TaskStatus.INTERRUPTED
        assert pending_interrupts[0].value["type"] == "ambiguous_tool_recovery"
        assert len(tool.calls) == 1
        print("AMBIGUOUS_UNSAFE_CRASH_SMOKE=" + json.dumps({
            "journal": "started",
            "side_effect_counter": 1,
            "replay_safe": False,
            "automatic_retry": False,
            "recovery_interrupt": True,
        }, sort_keys=True))
        result = await graph.ainvoke(
            Command(resume={"choice": "abort", "reason": "do not duplicate"}),
            config=graph_config(task_id),
        )

    assert len(tool.calls) == 1
    assert result["status"] is TaskStatus.FAILED
    assert result["recovery_issue"] is not None


@pytest.mark.asyncio
async def test_unsafe_started_human_retry_replays_exact_action(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-retry"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TOOL_RETURN_BEFORE_TERMINAL_JOURNAL
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        await graph.ainvoke(None, config=graph_config(task_id))
        result = await graph.ainvoke(
            Command(resume={"choice": "retry", "reason": "duplicate accepted"}),
            config=graph_config(task_id),
        )
        record = await journal.get(f"{task_id}:1:0:1")

    assert len(tool.calls) == 2
    assert tool.calls[0] == tool.calls[1] == {"value": 7}
    assert record is not None and record.attempt_count == 2
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_high_risk_approval_survives_checkpointer_recreation(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "durable-approval"
    first_tool = CountingTool(replay_safe=False, risk="high")
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        paused = await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert paused["status"] is TaskStatus.WAITING_APPROVAL
        assert interrupts(snapshot)[0].value["type"] == "tool_approval"
        assert first_tool.calls == []

    rebuilt_tool = CountingTool(replay_safe=False, risk="high")
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        snapshot = await graph.aget_state(graph_config(task_id))
        assert interrupts(snapshot)[0].value["type"] == "tool_approval"
        result = await graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=graph_config(task_id),
        )

    assert rebuilt_tool.calls == [{"value": 7}]
    assert result["status"] is TaskStatus.COMPLETED
    print("DURABLE_APPROVAL_SMOKE=" + json.dumps({
        "runtime_recreated": True,
        "pending_approval_restored": True,
        "tool_counter_before_approval": 0,
        "tool_counter_after_approval": len(rebuilt_tool.calls),
        "status": result["status"].value,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_terminal_failed_journal_is_materialized_without_reinvoke(
    tmp_path: Path,
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "terminal-failed"
    counter_path = tmp_path / "failed-counter.txt"
    first_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
        fail_after_increment=True,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(first_tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(
                ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        record = await journal.get(f"{task_id}:1:0:1")
        assert record is not None and record.status is JournalStatus.FAILED

    rebuilt_tool = FileCounterTool(
        replay_safe=False,
        counter_path=counter_path,
        fail_after_increment=True,
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert counter_path.read_text(encoding="utf-8") == "1"
    assert rebuilt_tool.calls == []
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0].status.value == "failed"
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_unsafe_crash_before_invoke_is_still_ambiguous(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "unsafe-before-invoke"
    tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        assert tool.calls == []

    rebuilt_tool = CountingTool(replay_safe=False)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(rebuilt_tool, [finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        paused = await graph.ainvoke(None, config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))

    assert rebuilt_tool.calls == []
    assert paused["status"] is TaskStatus.INTERRUPTED
    assert interrupts(snapshot)[0].value["type"] == "ambiguous_tool_recovery"


@pytest.mark.asyncio
async def test_arguments_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "hash-mismatch"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))

    async with aiosqlite.connect(config.action_journal_db_path) as connection:
        await connection.execute(
            "UPDATE action_journal SET arguments_hash = ? WHERE action_key = ?",
            ("tampered", f"{task_id}:1:0:1"),
        )
        await connection.commit()

    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [finish_call()]), verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert tool.calls == []
    assert result["status"] is TaskStatus.FAILED
    assert "identity/hash" in result["error"]


@pytest.mark.asyncio
async def test_completed_step_is_not_rerun_after_step_two_crash(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "two-step-restart"
    counter_path = tmp_path / "two-step-counter.txt"
    first_runtime_tool = FileCounterTool(
        replay_safe=True, counter_path=counter_path
    )
    first_model = FakeFunctionCallingModel([
        tool_call("step-1-tool", value=1),
        finish_call("step-1-finish"),
        tool_call("step-2-tool", value=2),
    ])
    registry = ToolRegistry()
    registry.register(first_runtime_tool)
    first_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, first_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=TwoStepPlanner(),
            executor=first_executor, verifier=FakeVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnStepTwo(
                ExecutionFaultPoint.AFTER_JOURNAL_STARTED
            ),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert snapshot.values["plan"][0].status.value == "completed"
        assert counter_path.read_text(encoding="utf-8") == "1"

    rebuilt_tool = FileCounterTool(replay_safe=True, counter_path=counter_path)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=TwoStepPlanner(),
            executor=make_executor(rebuilt_tool, [finish_call("step-2-finish")]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert counter_path.read_text(encoding="utf-8") == "2"
    assert rebuilt_tool.calls == [{"value": 2}]
    assert [step.status.value for step in result["plan"]] == ["completed", "completed"]
    assert result["status"] is TaskStatus.COMPLETED
    print("FULL_RESTART_SMOKE=" + json.dumps({
        "step_1_tool_count": 1,
        "step_1_status": result["plan"][0].status.value,
        "step_2_tool_count": len(rebuilt_tool.calls),
        "step_2_status": result["plan"][1].status.value,
        "task_status": result["status"].value,
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_verifier_retry_count_and_feedback_survive_restart(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "verifier-retry"
    tool = CountingTool(replay_safe=True)
    first_model = FakeFunctionCallingModel([
        finish_call("first-claim"),
        tool_call("retry-tool"),
    ])
    registry = ToolRegistry()
    registry.register(tool)
    first_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, first_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=first_executor, verifier=RejectOnceVerifier(),
            checkpointer=checkpoint.checkpointer, action_journal=journal,
            fault_injector=CrashOnce(ExecutionFaultPoint.AFTER_JOURNAL_STARTED),
        )
        with pytest.raises(InjectedCrash):
            await graph.ainvoke(initial_state(task_id), config=graph_config(task_id))
        snapshot = await graph.aget_state(graph_config(task_id))
        assert snapshot.values["plan"][0].retry_count == 1
        assert snapshot.values["step_verification"].feedback == "need durable evidence"

    second_model = FakeFunctionCallingModel([finish_call("retry-finish")])
    registry = ToolRegistry()
    registry.register(tool)
    second_executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, second_model),
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=second_executor,
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        result = await graph.ainvoke(None, config=graph_config(task_id))

    assert result["plan"][0].retry_count == 1
    assert "need durable evidence" in str(second_model.messages[0][-1].content)
    assert result["status"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_action_level_max_actions_remains_bounded(tmp_path: Path) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    tool = CountingTool(replay_safe=True)
    registry = ToolRegistry()
    registry.register(tool)
    model = FakeFunctionCallingModel([tool_call()])
    executor = TaskExecutor(
        registry,
        model=cast(BaseChatModel, model),
        max_actions_per_step=1,
        policy=DefaultRiskPolicy(),
    )
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(), executor=executor,
            verifier=FakeVerifier(), max_step_attempts=1,
            checkpointer=checkpoint.checkpointer, action_journal=journal,
        )
        result = await graph.ainvoke(
            initial_state("bounded-actions"), config=graph_config("bounded-actions")
        )

    assert model.invocations == 1
    assert len(tool.calls) == 1
    assert result["step_outcome"].error == "Executor action limit reached: 1"
    assert result["status"] is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_cli_resume_completed_task_does_not_run_graph_again(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "completed-cli"
    tool = CountingTool(replay_safe=True)
    async with SQLiteCheckpointProvider(config) as checkpoint, SQLiteActionJournal(
        config.action_journal_db_path
    ) as journal:
        graph = build_graph(
            analyzer=FakeAnalyzer(), planner=FakePlanner(),
            executor=make_executor(tool, [tool_call(), finish_call()]),
            verifier=FakeVerifier(), checkpointer=checkpoint.checkpointer,
            action_journal=journal,
        )
        completed = await graph.ainvoke(
            initial_state(task_id), config=graph_config(task_id)
        )
        assert completed["status"] is TaskStatus.COMPLETED

    exit_code = await async_main(
        ["--resume", task_id, "--state-dir", str(tmp_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task already completed" in output
    assert "status=completed" in output
    assert f"task_id={task_id}" in output
    assert len(tool.calls) == 1
```

### tests/test_phase9_mcp_resume.py

```python
"""真实 MCP STDIO provider 重建后的 durable Graph 恢复测试。"""

import sys
from pathlib import Path
from typing import Any, Sequence, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.graph import build_graph
from taskpilot.mcp import MCPServerConfig, MCPToolProvider
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.persistence import (
    ExecutionFaultPoint,
    InjectedCrash,
    PersistenceConfig,
    SQLiteActionJournal,
    SQLiteCheckpointProvider,
)
from taskpilot.policy import DefaultRiskPolicy
from taskpilot.state import TaskState
from taskpilot.tools import ToolRegistry


SERVER_PATH = Path(__file__).parent / "fixtures" / "mcp_server.py"


class FakeModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any):
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self.responses.pop(0)


def action(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }])


class Analyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(goal=user_input, completion_criteria=["echo observed"])


class Planner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(steps=[PlanStep(
            id=1,
            description="echo through rebuilt MCP provider",
            success_criteria=["echo result exists"],
        )])


class Verifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        return StepVerificationResult(
            step_id=1,
            verified=True,
            checks=[CriterionCheck(
                criterion_index=0,
                satisfied=True,
                reason="durable echo observation",
            )],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(completed=True, reason="MCP resume complete")


class CrashAfterTerminal:
    def hit(self, point: ExecutionFaultPoint, action_key: str) -> None:
        if point is ExecutionFaultPoint.AFTER_TERMINAL_JOURNAL_BEFORE_GRAPH_RETURN:
            raise InjectedCrash(action_key)


def initial_state(task_id: str) -> TaskState:
    return {
        "task_id": task_id,
        "user_input": "durable MCP echo",
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "step_outcome": None,
        "step_results": [],
        "step_verification": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "pending_action": None,
        "policy_decisions": [],
        "approval_records": [],
        "current_action_index": 0,
        "pending_executor_action": None,
        "recovery_issue": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def server_config() -> MCPServerConfig:
    return MCPServerConfig(
        server_id="demo",
        transport="stdio",
        command=sys.executable,
        args=["-u", str(SERVER_PATH)],
        trust_tool_annotations=True,
    )


@pytest.mark.asyncio
async def test_real_mcp_provider_reconnects_for_same_durable_thread(
    tmp_path: Path,
) -> None:
    persistence = PersistenceConfig.from_state_dir(tmp_path)
    task_id = "mcp-provider-rebuild"
    graph_config = {
        "configurable": {"thread_id": task_id},
        "recursion_limit": 100,
    }

    first_registry = ToolRegistry()
    async with MCPToolProvider([server_config()]) as mcp_provider:
        await mcp_provider.register_tools(first_registry)
        assert first_registry.get("demo__echo").metadata["replay_safe"] is True
        first_executor = TaskExecutor(
            first_registry,
            model=cast(BaseChatModel, FakeModel([
                action("demo__echo", {"text": "phase-9"}, "mcp-echo-1")
            ])),
            policy=DefaultRiskPolicy(),
        )
        async with SQLiteCheckpointProvider(persistence) as checkpoint, SQLiteActionJournal(
            persistence.action_journal_db_path
        ) as journal:
            graph = build_graph(
                analyzer=Analyzer(), planner=Planner(), executor=first_executor,
                verifier=Verifier(), checkpointer=checkpoint.checkpointer,
                action_journal=journal, fault_injector=CrashAfterTerminal(),
            )
            with pytest.raises(InjectedCrash):
                await graph.ainvoke(initial_state(task_id), config=graph_config)

    rebuilt_registry = ToolRegistry()
    reinvocations: list[dict[str, Any]] = []
    async with MCPToolProvider([server_config()]) as rebuilt_provider:
        adapters = await rebuilt_provider.register_tools(rebuilt_registry)
        echo_adapter = next(item for item in adapters if item.name == "demo__echo")
        original_invoke = echo_adapter.invoke

        async def counted_invoke(arguments: dict[str, Any]) -> Any:
            reinvocations.append(dict(arguments))
            return await original_invoke(arguments)

        echo_adapter.invoke = counted_invoke  # type: ignore[method-assign]
        rebuilt_executor = TaskExecutor(
            rebuilt_registry,
            model=cast(BaseChatModel, FakeModel([
                action(
                    FINISH_STEP_NAME,
                    {
                        "summary": "MCP echo is durable",
                        "evidence": ["journaled structured_content"],
                        "output": {"echoed": "phase-9"},
                    },
                    "mcp-finish-2",
                )
            ])),
            policy=DefaultRiskPolicy(),
        )
        async with SQLiteCheckpointProvider(persistence) as checkpoint, SQLiteActionJournal(
            persistence.action_journal_db_path
        ) as journal:
            graph = build_graph(
                analyzer=Analyzer(), planner=Planner(), executor=rebuilt_executor,
                verifier=Verifier(), checkpointer=checkpoint.checkpointer,
                action_journal=journal,
            )
            result = await graph.ainvoke(None, config=graph_config)

    assert reinvocations == []
    assert result["tool_calls"][0].result["structured_content"] == {
        "echoed": "phase-9"
    }
    assert result["status"] is TaskStatus.COMPLETED
```


以上为 Phase 9 新增测试文件的完整内容。Browser/MCP 既有测试中的 Phase 9
assertions 已在完整 pytest 回归中执行。

## 20. Git Status

命令：

```powershell
git status --short
```

真实输出：

```text
 M .gitignore
 M README.md
 M docs/review/phase_03.md
 M pyproject.toml
 M src/taskpilot/cli.py
 M src/taskpilot/graph.py
 M src/taskpilot/models.py
 M src/taskpilot/state.py
 M tests/test_analyzer.py
 M tests/test_graph_smoke.py
 M tests/test_models.py
 M tests/test_planner.py
?? docs/review/phase_04.md
?? docs/review/phase_05.md
?? docs/review/phase_06.md
?? docs/review/phase_07.md
?? docs/review/phase_08.md
?? docs/review/phase_09.md
?? src/taskpilot/browser/
?? src/taskpilot/executor.py
?? src/taskpilot/mcp/
?? src/taskpilot/persistence/
?? src/taskpilot/policy/
?? src/taskpilot/tools/
?? src/taskpilot/verifier.py
?? tests/conftest.py
?? tests/fixtures/
?? tests/test_browser.py
?? tests/test_browser_executor.py
?? tests/test_browser_policy.py
?? tests/test_executor.py
?? tests/test_hitl.py
?? tests/test_mcp.py
?? tests/test_mcp_integration.py
?? tests/test_phase5_graph.py
?? tests/test_phase9_mcp_resume.py
?? tests/test_phase9_persistence.py
?? tests/test_phase9_recovery.py
?? tests/test_policy.py
?? tests/test_tools.py
?? tests/test_verifier.py
```

Git 同时输出两条无法读取用户级 global ignore 的权限 warning；不影响仓库内
`.gitignore`或上述 status。Phase 9 以外的既有未提交修改未被删除或覆盖。
