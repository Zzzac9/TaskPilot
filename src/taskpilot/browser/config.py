"""Playwright Chromium 执行环境的轻量配置。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class BrowserConfig(BaseModel):
    """Phase 7 实际支持的 Chromium Browser 配置。"""

    browser_type: Literal["chromium"] = "chromium"
    headless: bool = True
    action_timeout_ms: int = Field(default=5_000, ge=1)
    navigation_timeout_ms: int = Field(default=10_000, ge=1)
    viewport_width: int = Field(default=1280, ge=320)
    viewport_height: int = Field(default=720, ge=240)
    download_dir: Path = Path("artifacts/browser/downloads")
    artifact_dir: Path = Path("artifacts/browser/screenshots")
    default_snapshot_depth: int = Field(default=8, ge=1, le=30)
    max_snapshot_chars: int = Field(default=20_000, ge=100)
    max_extract_chars: int = Field(default=20_000, ge=100)

    @field_validator("download_dir", "artifact_dir")
    @classmethod
    def resolve_controlled_directory(cls, value: Path) -> Path:
        """在任何文件写入前固定为绝对目录。"""

        return value.expanduser().resolve()

    def ensure_directories(self) -> None:
        """Browser lifecycle 启动时创建唯一允许写入的目录。"""

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
