"""Browser click preflight metadata 的确定性风险规则。"""

import re
from typing import Any
from urllib.parse import urlsplit

from taskpilot.policy.models import RiskLevel


_ENGLISH_HIGH_RISK = re.compile(
    r"\b(delete|remove|send|submit|purchase|buy|pay|order|"
    r"confirm\s+order|book|publish|post|upload)\b",
    re.IGNORECASE,
)
_CHINESE_HIGH_RISK = (
    "删除",
    "移除",
    "发送",
    "提交",
    "购买",
    "支付",
    "下单",
    "确认订单",
    "预订",
    "发布",
    "上传",
)
_SEARCH_SEMANTICS = re.compile(r"\bsearch\b|搜索|检索|查询", re.IGNORECASE)


def classify_browser_press(metadata: dict[str, Any]) -> tuple[RiskLevel, str]:
    """搜索型 GET 表单的 Enter 自动放行，其他提交保持保守。"""

    key = str(metadata.get("pressed_key") or "")
    if key.lower() != "enter":
        return classify_browser_click(metadata)

    if metadata.get("is_submit"):
        target_text = " ".join(
            str(metadata.get(name) or "")
            for name in ("accessible_name", "aria_label", "text", "value")
        )
        is_search = (
            str(metadata.get("input_type") or "").lower() == "search"
            or _SEARCH_SEMANTICS.search(target_text) is not None
        )
        form_method = str(metadata.get("form_method") or "").lower()
        if form_method == "get" and is_search:
            return RiskLevel.MEDIUM, "搜索型 GET form 的 Enter 导航"
        return RiskLevel.HIGH, "Enter 可能提交非搜索或非 GET form"

    return RiskLevel.MEDIUM, "普通控件 Enter 键盘交互"


def classify_browser_click(metadata: dict[str, Any]) -> tuple[RiskLevel, str]:
    """优先使用 HTML submit 语义，再检查名称，最后按元素类型分类。"""

    if metadata.get("vision_fallback_required") is True:
        return RiskLevel.HIGH, "DOM 无法可靠定位，Vision coordinate click 必须人工审批"

    if metadata.get("is_submit"):
        return RiskLevel.HIGH, "目标具有 HTML form submit 语义"

    target_text = " ".join(
        str(metadata.get(key) or "")
        for key in ("accessible_name", "aria_label", "text", "value")
    ).strip()
    if _ENGLISH_HIGH_RISK.search(target_text) or any(
        term in target_text for term in _CHINESE_HIGH_RISK
    ):
        return RiskLevel.HIGH, "目标名称包含外部写入或破坏性动作语义"

    tag_name = str(metadata.get("tag_name") or "").lower()
    href = str(metadata.get("href") or "")
    if tag_name == "a" and urlsplit(href).scheme.lower() in {"http", "https"}:
        return RiskLevel.LOW, "普通 HTTP(S) link navigation"
    if tag_name in {"button", "input"}:
        return RiskLevel.MEDIUM, "普通非提交 DOM control interaction"

    # 无法可靠识别的 dynamic click 采用最保守等级，绝不默认 LOW。
    return RiskLevel.HIGH, "无法可靠判断 dynamic click 的副作用"
