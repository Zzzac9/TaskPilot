"""统一适用于 Browser、MCP 与 Local Tool 的 deterministic Risk Policy。"""

import re
from typing import Any, Protocol

from taskpilot.policy.browser import classify_browser_click, classify_browser_press
from taskpilot.policy.models import PolicyDecision, PolicyOutcome, RiskLevel
from taskpilot.tools import Tool


_SENSITIVE_KEY = re.compile(
    r"password|passwd|token|secret|api[_-]?key|authorization|cookie|credential",
    re.IGNORECASE,
)


class ToolPolicyLike(Protocol):
    """真实 Policy 与测试替身共享的异步风险判断接口。"""

    async def assess(
        self,
        *,
        call_id: str,
        step_id: int,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        """只分类与读取 preflight metadata，不执行目标 Tool。"""

        ...


def redact_preview(value: Any) -> Any:
    """递归脱敏 human-facing preview，不修改真实 Tool arguments。"""

    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(str(key))
                else redact_preview(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_preview(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class DefaultRiskPolicy:
    """基于 Host metadata 与无副作用 Browser preflight 的默认策略。"""

    async def assess(
        self,
        *,
        call_id: str,
        step_id: int,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        metadata = getattr(tool, "metadata", {}) or {}
        source = str(metadata.get("source") or "unknown")
        annotations_trusted: bool | None = None

        if metadata.get("policy_outcome") == PolicyOutcome.DENY.value:
            risk = RiskLevel.HIGH
            outcome = PolicyOutcome.DENY
            reason = str(metadata.get("policy_reason") or "Host policy 明确禁止该 Tool")
        elif source == "browser":
            risk, reason = await self._assess_browser(tool, metadata, arguments)
            outcome = self._outcome_for(risk)
        elif source == "mcp":
            annotations_trusted = bool(metadata.get("trust_tool_annotations", False))
            risk, reason = self._assess_mcp(metadata, annotations_trusted)
            outcome = self._outcome_for(risk)
        else:
            risk, reason = self._assess_local_or_unknown(metadata, source)
            outcome = self._outcome_for(risk)

        preview = redact_preview(arguments)
        if not isinstance(preview, dict):  # pragma: no cover - arguments 固定为 dict
            preview = {"value": preview}
        return PolicyDecision(
            call_id=call_id,
            step_id=step_id,
            tool_name=tool.name,
            risk_level=risk,
            outcome=outcome,
            reason=reason,
            tool_source=source,
            preview=preview,
            annotations_trusted=annotations_trusted,
        )

    @staticmethod
    async def _assess_browser(
        tool: Tool,
        metadata: dict[str, Any],
        arguments: dict[str, Any],
    ) -> tuple[RiskLevel, str]:
        declared = str(metadata.get("risk") or "high")
        if declared != "dynamic":
            try:
                return RiskLevel(declared), "TaskPilot first-party Browser Tool metadata"
            except ValueError:
                return RiskLevel.HIGH, "Browser Tool risk metadata 无法识别"

        preflight = getattr(tool, "preflight", None)
        if preflight is None:
            return RiskLevel.HIGH, "Dynamic Browser Tool 缺少可用 preflight"
        inspected = await preflight(arguments)
        if tool.name == "browser_press":
            return classify_browser_press(inspected)
        return classify_browser_click(inspected)

    @staticmethod
    def _assess_mcp(
        metadata: dict[str, Any],
        trusted: bool,
    ) -> tuple[RiskLevel, str]:
        if not trusted:
            return (
                RiskLevel.HIGH,
                "MCP annotations 未被 Host 信任，按未知 write-capable Tool 处理",
            )
        annotations = metadata.get("annotations")
        if not isinstance(annotations, dict):
            return RiskLevel.HIGH, "可信 MCP Server 未提供完整 annotations"
        read_only = annotations.get("read_only_hint")
        destructive = annotations.get("destructive_hint")
        open_world = annotations.get("open_world_hint")
        if read_only is True:
            return RiskLevel.LOW, "可信 MCP annotation 声明 read-only"
        if read_only is False and (destructive is True or open_world is True):
            return RiskLevel.HIGH, "可信 MCP annotation 提示 destructive/open-world write"
        if read_only is False and destructive is False and open_world is False:
            return RiskLevel.MEDIUM, "可信 MCP annotation 表示受限、非破坏性写入"
        return RiskLevel.HIGH, "MCP annotations 不完整，采用保守默认"

    @staticmethod
    def _assess_local_or_unknown(
        metadata: dict[str, Any],
        source: str,
    ) -> tuple[RiskLevel, str]:
        declared = metadata.get("risk")
        if source == "local" and declared in {level.value for level in RiskLevel}:
            return RiskLevel(declared), "Host 显式 Local Tool risk metadata"
        return RiskLevel.HIGH, "未知或无 policy metadata 的 Tool 采用保守默认"

    @staticmethod
    def _outcome_for(risk: RiskLevel) -> PolicyOutcome:
        return (
            PolicyOutcome.REQUIRE_APPROVAL
            if risk is RiskLevel.HIGH
            else PolicyOutcome.ALLOW
        )
