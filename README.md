# TaskPilot

TaskPilot 是一个面向 Web 与本地环境的通用任务执行 Agent。

核心规划架构为：

```text
Planner → Executor → Verifier
```

当前已完成：

### Phase 1

- Task models
- Task State
- LangGraph skeleton

### Phase 2

- LLM-based Task Analyzer
- Structured TaskSpec generation
- Completion Criteria extraction

### Phase 3

- Initial Planner
- Structured TaskPlan
- Step dependencies
- Step success criteria
- Basic plan validation

当前状态图为：

```text
START → initialize_task → analyze_task → plan_task → END
```

仍未实现：

- Executor
- Tools
- Verifier
- Browser
- MCP
- HITL
- Dynamic Replanning
- Checkpoint / Recovery
- Vision
- Benchmark implementation

## 本地运行

项目要求 Python 3.11 或更高版本。

```powershell
python -m pip install -e .
pytest
$env:LLM_API_KEY = "your-api-key"
$env:LLM_BASE_URL = "https://your-openai-compatible-endpoint/v1"
$env:LLM_MODEL = "your-model"
python -m taskpilot.cli "找3家香港岛月费低于500港币的健身房并生成CSV"
```

CLI 目前只调用 Task Analyzer 与 Initial Planner，并打印结构化 `TaskSpec` 和 `TaskPlan`。它不会执行计划或调用工具，任务状态仍为 `running`。
