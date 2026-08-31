"""TaskPilot 的最小命令行 smoke test。"""

import argparse
import json
from typing import Sequence
from uuid import uuid4

from taskpilot.graph import build_graph
from taskpilot.models import TaskStatus
from taskpilot.state import TaskState


def create_initial_state(user_input: str) -> TaskState:
    """根据用户输入创建结构化初始任务状态。"""

    return {
        "task_id": str(uuid4()),
        "user_input": user_input,
        "task_spec": None,
        "plan": [],
        "current_step_id": None,
        "tool_calls": [],
        "task_results": {},
        "verification": None,
        "status": TaskStatus.CREATED,
        "error": None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """运行一次 Phase 3 图，并输出 TaskSpec 与初始计划。"""

    parser = argparse.ArgumentParser(description="Run the TaskPilot Initial Planner.")
    parser.add_argument("task", help="Task description")
    args = parser.parse_args(argv)

    # CLI 使用真实 Analyzer 与 Planner；离线测试通过 build_graph 注入 Fake。
    result = build_graph().invoke(create_initial_state(args.task))
    status = result["status"]
    status_value = status.value if isinstance(status, TaskStatus) else str(status)
    print(f"task_id={result['task_id']}")
    print(f"status={status_value}")
    task_spec = result["task_spec"]
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
    for step in result["plan"]:
        print(f"  step_id={step.id}")
        print(f"    description={step.description}")
        print(f"    depends_on={json.dumps(step.depends_on)}")
        print(
            "    success_criteria="
            + json.dumps(step.success_criteria, ensure_ascii=False)
        )
        print(f"    status={step.status.value}")
    print(f"current_step_id={result['current_step_id']}")
    if result["error"]:
        print(f"error={result['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
