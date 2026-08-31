"""Lightweight configuration model for TaskPilot."""

from pydantic import BaseModel


class Settings(BaseModel):
    """Runtime settings reserved for local configuration."""

    environment: str = "development"
    log_level: str = "INFO"

