"""语义优先的 Playwright LocatorSpec 与公开 API 映射。"""

from enum import Enum
from typing import Any

from playwright.async_api import FrameLocator, Locator, Page
from pydantic import BaseModel, Field, model_validator


class LocatorStrategy(str, Enum):
    """从语义定位到 CSS fallback 的有限策略集合。"""

    ROLE = "role"
    LABEL = "label"
    TEST_ID = "test_id"
    TEXT = "text"
    PLACEHOLDER = "placeholder"
    CSS = "css"


class LocatorSpec(BaseModel):
    """LLM 可提供的结构化定位信息；不支持任意 XPath/JavaScript。"""

    strategy: LocatorStrategy
    role: str | None = None
    name: str | None = None
    value: str | None = None
    exact: bool = False
    # 当语义定位命中多个同名元素时，显式选择观察结果中的第几个（从 0 开始）。
    # 省略时继续保持 Playwright strict mode，避免悄悄点错目标。
    index: int | None = Field(default=None, ge=0)
    # frame_chain 只描述 iframe 边界；每一项交给公开 FrameLocator API。
    frame_chain: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_strategy_fields(self) -> "LocatorSpec":
        if self.strategy is LocatorStrategy.ROLE:
            if not self.role or not self.name:
                raise ValueError("role locator 必须提供 role 和 name")
        elif not self.value:
            raise ValueError(f"{self.strategy.value} locator 必须提供 value")
        if any(not selector.strip() for selector in self.frame_chain):
            raise ValueError("frame_chain 不能包含空 selector")
        return self


LOCATOR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy": {
            "type": "string",
            "enum": [strategy.value for strategy in LocatorStrategy],
        },
        "role": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "exact": {"type": "boolean", "default": False},
        "index": {"type": ["integer", "null"], "minimum": 0},
        "frame_chain": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "default": [],
        },
    },
    "required": ["strategy"],
    "additionalProperties": False,
}


def resolve_locator(page: Page, spec: LocatorSpec) -> Locator:
    """按公开 Locator/FrameLocator API 构造严格 Locator。"""

    target: Page | FrameLocator = page
    for frame_selector in spec.frame_chain:
        target = target.frame_locator(frame_selector)

    if spec.strategy is LocatorStrategy.ROLE:
        locator = target.get_by_role(
            spec.role,  # type: ignore[arg-type]
            name=spec.name,
            exact=spec.exact,
        )
    elif spec.strategy is LocatorStrategy.LABEL:
        locator = target.get_by_label(spec.value or "", exact=spec.exact)
    elif spec.strategy is LocatorStrategy.TEST_ID:
        locator = target.get_by_test_id(spec.value or "")
    elif spec.strategy is LocatorStrategy.TEXT:
        locator = target.get_by_text(spec.value or "", exact=spec.exact)
    elif spec.strategy is LocatorStrategy.PLACEHOLDER:
        locator = target.get_by_placeholder(spec.value or "", exact=spec.exact)
    else:
        locator = target.locator(spec.value or "")
    return locator.nth(spec.index) if spec.index is not None else locator
