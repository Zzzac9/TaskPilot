# TaskPilot

TaskPilot 是一个面向 Web 与本地环境的通用任务执行 Agent。

核心规划架构为：

```text
Planner → Executor → Verifier
```

当前处于 Phase 1，仅完成：

- 数据模型
- Task State
- LangGraph 基础骨架

## 本地运行

项目要求 Python 3.11 或更高版本。

```powershell
python -m pip install -e .
pytest
python -m taskpilot.cli "找3家香港岛月费低于500港币的健身房并生成CSV"
```

