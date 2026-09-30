"""Browser DOM 定位失败时使用的最小视觉目标解析组件。"""

from taskpilot.vision.config import VisionConfig
from taskpilot.vision.models import (
    VisionTarget,
    VisionTargetRequest,
    validate_vision_target,
)
from taskpilot.vision.targeter import (
    MultimodalVisionTargeter,
    VisionTargeterLike,
)

__all__ = [
    "MultimodalVisionTargeter",
    "VisionConfig",
    "VisionTarget",
    "VisionTargeterLike",
    "VisionTargetRequest",
    "validate_vision_target",
]
