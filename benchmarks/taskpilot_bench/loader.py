"""固定 manifest 的加载与完整性校验。"""

import json
from collections import Counter
from pathlib import Path

from pydantic import TypeAdapter

from benchmarks.taskpilot_bench.models import BenchmarkCategory, BenchmarkTask


PRIMARY_DISTRIBUTION = {
    BenchmarkCategory.MCP: 6,
    BenchmarkCategory.BROWSER_DOM: 8,
    BenchmarkCategory.FORM_INTERACTION: 4,
    BenchmarkCategory.CONSTRAINT_AGGREGATION: 4,
    BenchmarkCategory.REPLANNING: 4,
    BenchmarkCategory.HITL: 4,
}


def load_manifest(
    path: Path, *, enforce_primary_shape: bool = True
) -> list[BenchmarkTask]:
    """加载 JSON manifest；主 manifest 必须正好是固定 30 题分布。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    tasks = TypeAdapter(list[BenchmarkTask]).validate_python(raw)
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Benchmark task id 必须唯一")
    if enforce_primary_shape:
        if len(tasks) != 30:
            raise ValueError("Primary manifest 必须正好包含 30 个 task")
        observed = Counter(task.category for task in tasks)
        if observed != Counter(PRIMARY_DISTRIBUTION):
            raise ValueError(
                f"Primary category 分布错误: observed={dict(observed)}, "
                f"expected={PRIMARY_DISTRIBUTION}"
            )
        vision = [task.id for task in tasks if task.requires_vision]
        if vision:
            raise ValueError(f"Primary 30 不得包含真实 Vision denominator: {vision}")
    return tasks


def render_task(task: BenchmarkTask, variables: dict[str, str]) -> BenchmarkTask:
    """只替换 Host 提供的 fixture 地址，不在运行中改变 task 语义。"""

    def render_value(value: object) -> object:
        if isinstance(value, str):
            return value.format_map(variables)
        if isinstance(value, list):
            return [render_value(item) for item in value]
        if isinstance(value, dict):
            return {key: render_value(item) for key, item in value.items()}
        return value

    try:
        prompt = task.prompt.format_map(variables)
        oracle_config = render_value(task.oracle_config)
    except KeyError as exc:
        raise ValueError(f"manifest 缺少模板变量: {exc.args[0]}") from exc
    if not isinstance(oracle_config, dict):  # pragma: no cover - 字段类型已封闭
        raise TypeError("rendered oracle_config 必须是 dict")
    return task.model_copy(update={"prompt": prompt, "oracle_config": oracle_config})
