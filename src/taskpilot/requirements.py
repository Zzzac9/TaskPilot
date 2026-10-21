"""TaskSpec 的不可变完成契约与可审计的要求引用。"""

from enum import Enum

from pydantic import BaseModel, ConfigDict

from taskpilot.models import TaskSpec


class RequirementPriority(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class ContractRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    statement: str
    priority: RequirementPriority
    origin: str


class RequirementContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    required: tuple[ContractRequirement, ...]
    optional: tuple[ContractRequirement, ...]

    @property
    def required_ids(self) -> set[str]:
        return {item.id for item in self.required}

    @property
    def all_ids(self) -> set[str]:
        return self.required_ids | {item.id for item in self.optional}


def build_requirement_contract(task_spec: TaskSpec) -> RequirementContract:
    """仅忠实冻结 Analyzer 的要求；不把 Planner 的附加条件升级为硬要求。

    注意：Analyzer 仍可能误解原始请求。来源标记使其可以审计，并不代表
    我们已经用确定性程序证明自然语言在语义上完全等价。
    """
    required = tuple(
        ContractRequirement(
            id=f"R{index + 1}",
            statement=statement,
            priority=RequirementPriority.REQUIRED,
            origin="task_spec.completion_criteria",
        )
        for index, statement in enumerate(task_spec.completion_criteria)
    )
    optional = tuple(
        ContractRequirement(
            id=f"O{index + 1}",
            statement=statement,
            priority=RequirementPriority.OPTIONAL,
            origin="task_spec.optional_criteria",
        )
        for index, statement in enumerate(task_spec.optional_criteria)
    )
    return RequirementContract(required=required, optional=optional)


def validate_step_references(
    requirement_ids: list[str], contract: RequirementContract
) -> None:
    """没有引用或引用了凭空创建的要求时，拒绝生产 Planner 的计划。"""
    if not requirement_ids:
        raise ValueError("PlanStep 缺少 requirement_ids：必须溯源到 TaskSpec")
    unknown = set(requirement_ids) - contract.all_ids
    if unknown:
        raise ValueError(f"PlanStep 引用了不存在的 Requirement ID: {sorted(unknown)}")
