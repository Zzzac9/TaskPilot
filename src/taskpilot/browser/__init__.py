"""基于 Playwright async API 的 DOM-first Browser Tool infrastructure。"""

from taskpilot.browser.config import BrowserConfig
from taskpilot.browser.locators import LocatorSpec, LocatorStrategy
from taskpilot.browser.session import (
    BrowserError,
    BrowserLocatorError,
    BrowserDownloadError,
    BrowserNavigationError,
    BrowserPageError,
    BrowserSession,
    BrowserSessionError,
    BrowserVisionError,
)
from taskpilot.browser.tools import BrowserToolProvider, is_vision_fallback_eligible

__all__ = [
    "BrowserConfig",
    "BrowserError",
    "BrowserLocatorError",
    "BrowserDownloadError",
    "BrowserNavigationError",
    "BrowserPageError",
    "BrowserSession",
    "BrowserSessionError",
    "BrowserVisionError",
    "BrowserToolProvider",
    "is_vision_fallback_eligible",
    "LocatorSpec",
    "LocatorStrategy",
]
