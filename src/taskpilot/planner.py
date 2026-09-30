"""把 TaskSpec 拆解为结构化、与具体工具无关的任务计划。"""

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from taskpilot.llm import create_chat_model, invoke_structured_with_retry
from taskpilot.models import TaskPlan, TaskSpec


PLANNER_SYSTEM_PROMPT = """你是 Initial Planner，只负责根据输入的 TaskSpec 创建计划，不重新解释或修改用户目标，也不执行任务。
使用完成任务所需的最少合理步骤；每个 PlanStep 描述任务级子目标，不得包含 search、browser、filesystem 等具体工具调用、函数参数或选择器，除非某个工具名称本身就是用户明确要求处理的任务对象。
每一步都必须提供只针对该步骤的、可验证的 success_criteria；整个计划最终应覆盖 TaskSpec.completion_criteria。
合理填写 depends_on，步骤 ID 必须唯一、稳定，并从 1 顺序递增；初始步骤保持 pending 状态。
不得添加 TaskSpec 中不存在的硬约束，不得虚构尚未执行获得的数据，也不得声称已经搜索、打开、读取或生成任何内容。
Planner 只做规划，不做执行。输出必须符合 TaskPlan schema。"""


class TaskPlannerLike(Protocol):
    """真实 Planner 与离线 Fake 共同遵循的最小接口。"""

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        """根据结构化任务描述生成经过校验的计划。"""

        ...


def validate_plan(task_plan: TaskPlan) -> None:
    """对 Initial Planner 输出执行轻量、确定性的结构校验。"""

    if not task_plan.steps:
        raise ValueError("TaskPlan.steps 不能为空")

    step_ids = [step.id for step in task_plan.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("PlanStep.id 必须唯一")
    if any(step_id < 1 for step_id in step_ids):
        raise ValueError("Initial PlanStep.id 必须是正整数")
    if step_ids != sorted(step_ids) or any(
        right <= left for left, right in zip(step_ids, step_ids[1:])
    ):
        raise ValueError("Initial PlanStep.id 必须严格递增")
    step_positions = {step_id: index for index, step_id in enumerate(step_ids)}
    for index, step in enumerate(task_plan.steps):
        if not step.success_criteria:
            raise ValueError(f"PlanStep {step.id} 缺少 success_criteria")
        for dependency_id in step.depends_on:
            if dependency_id == step.id:
                raise ValueError(f"PlanStep {step.id} 不能依赖自身")
            if dependency_id not in step_positions:
                raise ValueError(
                    f"PlanStep {step.id} 引用了不存在的依赖 {dependency_id}"
                )
            if step_positions[dependency_id] >= index:
                raise ValueError(
                    f"PlanStep {step.id} 的依赖 {dependency_id} 必须出现在当前步骤之前"
                )


class TaskPlanner:
    """使用 Chat Model Structured Output 创建并校验初始计划。"""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        """从 TaskSpec 生成有效的结构化 TaskPlan。"""

        if self._model is None:
            # 与 Analyzer 一样延迟创建模型，保证默认测试无需 API Key。
            self._model = create_chat_model()
        task_plan = invoke_structured_with_retry(
            self._model,
            TaskPlan,
            [
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", "TaskSpec:\n" + task_spec.model_dump_json(indent=2)),
            ],
        )
        validate_plan(task_plan)
        # revision 属于 Graph 控制信息，不信任模型生成值。
        return TaskPlan(
            steps=[step.model_copy(update={"revision": 0}) for step in task_plan.steps]
        )
