"""独立于 TaskPilot Verifier 的 deterministic external oracle。"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from benchmarks.taskpilot_bench.models import (
    BenchmarkTask,
    OracleCheck,
    OracleResult,
    OracleType,
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _path_values(value: Any, path: str) -> list[Any]:
    """点路径支持 list 展开，供 oracle 检查 step_results.*.output。"""

    values = [_jsonable(value)]
    for part in path.split(".") if path else []:
        next_values: list[Any] = []
        for current in values:
            if part == "*" and isinstance(current, list):
                next_values.extend(current)
            elif isinstance(current, dict) and part in current:
                next_values.append(current[part])
        values = next_values
    return values


def _contains(observed: Any, expected: Any) -> bool:
    """Expected 是 ground-truth 子结构；list 采用逐项包含而非顺序相等。"""

    observed = _jsonable(observed)
    expected = _jsonable(expected)
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and _contains(observed[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(observed, list) and all(
            any(_contains(candidate, item) for candidate in observed)
            for item in expected
        )
    if isinstance(observed, str) and isinstance(expected, str):
        return expected.casefold() in observed.casefold()
    return observed == expected


def _checks_from_mapping(observed: Any, config: dict[str, Any]) -> list[OracleCheck]:
    checks: list[OracleCheck] = []
    for index, rule in enumerate(config.get("checks", []), start=1):
        name = str(rule.get("name") or f"check_{index}")
        path = str(rule.get("path") or "")
        expected = rule.get("contains")
        values = _path_values(observed, path)
        passed = any(_contains(value, expected) for value in values)
        checks.append(
            OracleCheck(
                name=name,
                passed=passed,
                expected=expected,
                observed=values,
                reason=("ground truth matched" if passed else "ground truth not found"),
            )
        )
    return checks


def evaluate_oracle(
    task: BenchmarkTask,
    state: dict[str, Any],
    *,
    allowed_file_root: Path | None = None,
) -> OracleResult:
    """从 TaskState 外部读取 ground truth；绝不采信内部 VerificationResult。"""

    if task.oracle_type is OracleType.STATE_CONTAINS:
        observed = _jsonable(state)
        checks = _checks_from_mapping(observed, task.oracle_config)
    elif task.oracle_type is OracleType.FIXTURE_STATE:
        url = str(task.oracle_config["url"])
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("fixture_state oracle 只允许 localhost HTTP URL")
        with urlopen(url, timeout=5) as response:  # noqa: S310 - manifest 固定 localhost
            observed = json.loads(response.read().decode("utf-8"))
        checks = _checks_from_mapping(observed, task.oracle_config)
    elif task.oracle_type is OracleType.FILE_CONTENT:
        if allowed_file_root is None:
            raise ValueError("file oracle 必须配置 allowed_file_root")
        root = allowed_file_root.resolve()
        path = (root / str(task.oracle_config["path"])).resolve()
        if root != path and root not in path.parents:
            raise ValueError("file oracle path 越出 allowed root")
        observed = path.read_text(encoding="utf-8") if path.exists() else None
        checks = _checks_from_mapping(
            {"exists": path.exists(), "content": observed}, task.oracle_config
        )
    else:  # pragma: no cover - Enum 已封闭
        raise ValueError(f"unsupported oracle: {task.oracle_type}")
    passed = bool(checks) and all(check.passed for check in checks)
    return OracleResult(
        passed=passed,
        reason=(
            "all independent checks passed"
            if passed
            else "one or more independent checks failed"
        ),
        checks=checks,
        observed_output=observed,
    )
