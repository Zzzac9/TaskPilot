"""独立于文本模型的 Vision provider 配置。"""

import os

from pydantic import BaseModel, Field, SecretStr


class VisionConfig(BaseModel):
    """视觉 click fallback 的 Host 侧配置；默认完全关闭。"""

    enabled: bool = False
    model: str | None = None
    min_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    max_screenshot_bytes: int = Field(default=5_000_000, ge=1)
    api_key: SecretStr | None = None
    base_url: str | None = None

    @classmethod
    def from_env(cls) -> "VisionConfig":
        """只读取独立 VISION_* 环境变量，不借用默认文本模型配置。"""

        enabled = os.getenv("TASKPILOT_VISION_ENABLED", "").strip().lower()
        api_key = os.getenv("VISION_API_KEY")
        return cls(
            enabled=enabled in {"1", "true", "yes", "on"},
            model=os.getenv("VISION_MODEL"),
            api_key=SecretStr(api_key) if api_key else None,
            base_url=os.getenv("VISION_BASE_URL"),
        )
