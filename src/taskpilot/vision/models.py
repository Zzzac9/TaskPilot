"""Vision fallback 的确定性请求、响应与 viewport 校验。"""

from pydantic import BaseModel, Field, model_validator


class VisionTargetRequest(BaseModel):
    """从语义 Locator 提炼的视觉目标描述，不持有 Playwright 对象。"""

    target_description: str = Field(min_length=1)
    role: str | None = None
    accessible_name: str | None = None
    viewport_width: int = Field(gt=0)
    viewport_height: int = Field(gt=0)


class VisionTarget(BaseModel):
    """当前 viewport 中一个目标中心点，坐标单位为 CSS pixel。"""

    found: bool
    x: float | None = Field(default=None, ge=0)
    y: float | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    @model_validator(mode="after")
    def validate_coordinate_presence(self) -> "VisionTarget":
        """found 与坐标必须一致，避免模糊的半有效结果。"""

        if self.found and (self.x is None or self.y is None):
            raise ValueError("found=true 时 x 和 y 必须同时存在")
        if not self.found and (self.x is not None or self.y is not None):
            raise ValueError("found=false 时 x 和 y 必须为 null")
        return self


def validate_vision_target(
    target: VisionTarget,
    request: VisionTargetRequest,
) -> VisionTarget:
    """结合本次 screenshot 尺寸确定性验证坐标是否位于 viewport 内。"""

    if not target.found:
        return target
    assert target.x is not None and target.y is not None
    if not 0 <= target.x < request.viewport_width:
        raise ValueError(
            f"Vision x 坐标超出 viewport: x={target.x}, width={request.viewport_width}"
        )
    if not 0 <= target.y < request.viewport_height:
        raise ValueError(
            f"Vision y 坐标超出 viewport: y={target.y}, height={request.viewport_height}"
        )
    return target
