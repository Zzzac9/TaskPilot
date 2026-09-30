"""DOM-first Playwright Browser Tools 与长生命周期 Provider。"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from taskpilot.browser.config import BrowserConfig
from taskpilot.browser.locators import LOCATOR_JSON_SCHEMA, LocatorSpec, resolve_locator
from taskpilot.browser.session import (
    BrowserDownloadError,
    BrowserError,
    BrowserLocatorError,
    BrowserNavigationError,
    BrowserSession,
    BrowserVisionError,
    concise_playwright_error,
)
from taskpilot.tools import ToolRegistry
from taskpilot.vision import (
    MultimodalVisionTargeter,
    VisionConfig,
    VisionTargetRequest,
    VisionTargeterLike,
    validate_vision_target,
)


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_LOCATOR_PRIORITY = (
    "Prefer role + accessible name, then label, test_id, text/placeholder, "
    "and use CSS only as a fallback. Strict matching is preserved. If an "
    "observed semantic locator has duplicate matches, set locator.index to "
    "the intended zero-based occurrence; never repeat the same ambiguous locator."
)

_NON_VISION_ERRORS = (
    "page closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser disconnected",
    "connection closed",
    "navigation failed",
    "net::err_",
)
_VISION_LOCATION_ERRORS = (
    "timeout",
    "strict mode violation",
    "waiting for",
    "not attached",
    "detached",
    "resolved to",
    "no element",
    "not found",
    "canvas",
)


def is_vision_fallback_eligible(error: BaseException) -> bool:
    """只允许目标定位/attachment 类失败进入 Vision，不吞环境故障。"""

    if isinstance(error, (BrowserNavigationError, BrowserVisionError)):
        return False
    message = " ".join(str(error).lower().split())
    if any(marker in message for marker in _NON_VISION_ERRORS):
        return False
    if isinstance(error, BrowserLocatorError):
        return any(marker in message for marker in _VISION_LOCATION_ERRORS)
    if isinstance(error, PlaywrightError):
        return any(marker in message for marker in _VISION_LOCATION_ERRORS)
    return False


@dataclass
class BrowserTool:
    """把一个 Browser handler 暴露为统一 async Tool。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    _handler: Callable[[dict[str, Any]], Awaitable[Any]]
    metadata: dict[str, Any] = field(default_factory=dict)
    _preflight: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return await self._handler(arguments)

    async def preflight(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """只读取 dynamic action metadata，不执行目标动作。"""

        if self._preflight is None:
            raise BrowserError(f"Browser Tool {self.name} 没有 dynamic preflight")
        return await self._preflight(arguments)


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


class BrowserTools:
    """BrowserSession 上的 JSON-serializable Tool handlers。"""

    def __init__(
        self,
        session: BrowserSession,
        *,
        vision_config: VisionConfig | None = None,
        vision_targeter: VisionTargeterLike | None = None,
    ) -> None:
        self.session = session
        self.vision_config = vision_config or VisionConfig()
        self.vision_targeter = vision_targeter
        self._screenshot_number = 1

    @property
    def vision_available(self) -> bool:
        """只有 Host 同时启用并提供 targeter 时才允许 fallback。"""

        return self.vision_config.enabled and self.vision_targeter is not None

    def build(self) -> list[BrowserTool]:
        # 可选 page_id 由模型留成 "" 时等价于省略并使用 active page。
        # 只有 browser_switch_page 的必填目标仍禁止空字符串。
        page_id = {
            "type": "string",
            "description": "Stable page ID. Omit or use an empty string for the active page.",
        }
        required_page_id = {"type": "string", "minLength": 1}
        locator = LOCATOR_JSON_SCHEMA
        return [
            BrowserTool(
                "browser_navigate",
                "Navigate an existing page to an HTTP(S) URL.",
                _object_schema(
                    {"url": {"type": "string", "minLength": 1}, "page_id": page_id},
                    ["url"],
                ),
                self.navigate,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_observe",
                "Return an AI-mode ARIA snapshot, URL, title and stable page IDs.",
                _object_schema(
                    {
                        "page_id": page_id,
                        "depth": {"type": "integer", "minimum": 1, "maximum": 30},
                    }
                ),
                self.observe,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_click",
                f"Click one strict semantic locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "page_id": page_id,
                        "expect_popup": {"type": "boolean", "default": False},
                    },
                    ["locator"],
                ),
                self.click,
                metadata={"source": "browser", "risk": "dynamic", "replay_safe": False},
                _preflight=self.inspect_click,
            ),
            BrowserTool(
                "browser_fill",
                f"Fill one strict textbox locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "value": {"type": "string"},
                        "page_id": page_id,
                    },
                    ["locator", "value"],
                ),
                self.fill,
                metadata={"source": "browser", "risk": "medium", "replay_safe": True},
            ),
            BrowserTool(
                "browser_press",
                (
                    "Press one keyboard key on a strict semantic locator. "
                    "Use Enter to submit a search or form after filling it. "
                    f"{_LOCATOR_PRIORITY}"
                ),
                _object_schema(
                    {
                        "locator": locator,
                        "key": {"type": "string", "minLength": 1, "maxLength": 100},
                        "page_id": page_id,
                    },
                    ["locator", "key"],
                ),
                self.press,
                metadata={"source": "browser", "risk": "dynamic", "replay_safe": False},
                _preflight=self.inspect_press,
            ),
            BrowserTool(
                "browser_select",
                f"Select one option by value on a strict locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "value": {"type": "string"},
                        "page_id": page_id,
                    },
                    ["locator", "value"],
                ),
                self.select,
                metadata={"source": "browser", "risk": "medium", "replay_safe": True},
            ),
            BrowserTool(
                "browser_check",
                f"Check one checkbox/radio locator. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.check,
                metadata={"source": "browser", "risk": "medium", "replay_safe": True},
            ),
            BrowserTool(
                "browser_extract_text",
                f"Extract bounded text from one locator or body. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {
                        "locator": locator,
                        "page_id": page_id,
                        "max_chars": {"type": "integer", "minimum": 1},
                    }
                ),
                self.extract_text,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_list_pages",
                "List stable page IDs with active state, title and URL.",
                _object_schema({}),
                self.list_pages,
                metadata={"source": "browser", "risk": "low", "replay_safe": True},
            ),
            BrowserTool(
                "browser_switch_page",
                "Switch the active page using a stable page_id.",
                _object_schema({"page_id": required_page_id}, ["page_id"]),
                self.switch_page,
                metadata={"source": "browser", "risk": "low", "replay_safe": False},
            ),
            BrowserTool(
                "browser_download",
                f"Click one locator and save its download only in the controlled directory. {_LOCATOR_PRIORITY}",
                _object_schema(
                    {"locator": locator, "page_id": page_id},
                    ["locator"],
                ),
                self.download,
                metadata={"source": "browser", "risk": "medium", "replay_safe": False},
            ),
            BrowserTool(
                "browser_screenshot",
                "Save a system-named PNG in the controlled artifact directory; no Vision call.",
                _object_schema(
                    {
                        "page_id": page_id,
                        "full_page": {"type": "boolean", "default": False},
                    }
                ),
                self.screenshot,
                metadata={"source": "browser", "risk": "medium", "replay_safe": False},
            ),
        ]

    async def navigate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        url = arguments["url"]
        if urlsplit(url).scheme.lower() not in {"http", "https"}:
            raise BrowserNavigationError(
                f"browser_navigate 拒绝非 HTTP(S) URL: {urlsplit(url).scheme or 'missing'}"
            )
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.session.config.navigation_timeout_ms,
            )
            return {
                "page_id": page_id,
                "final_url": page.url,
                "title": await page.title(),
                "status": response.status if response is not None else None,
            }
        except PlaywrightError as exc:
            raise BrowserNavigationError(
                f"browser_navigate failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc

    async def observe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        depth = arguments.get("depth", self.session.config.default_snapshot_depth)
        try:
            snapshot = await page.aria_snapshot(mode="ai", depth=depth)
        except PlaywrightError as exc:
            raise BrowserError(
                f"browser_observe failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        original_length = len(snapshot)
        truncated = original_length > self.session.config.max_snapshot_chars
        if truncated:
            snapshot = snapshot[: self.session.config.max_snapshot_chars]
        return {
            "page_id": page_id,
            "url": page.url,
            "title": await page.title(),
            "aria_snapshot": snapshot,
            "snapshot_mode": "ai",
            "depth": depth,
            "truncated": truncated,
            "original_length": original_length,
            "pages": await self.session.list_pages(),
        }

    async def click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "click")
        try:
            if arguments.get("expect_popup", False):
                async with page.expect_popup() as popup_info:
                    await locator.click()
                popup = await popup_info.value
                await popup.wait_for_load_state("domcontentloaded")
                new_page_id = self.session.register_page(popup)
            else:
                await locator.click()
                new_page_id = None
            pages = await self.session.list_pages()
            return {
                "clicked": True,
                "interaction_mode": "dom",
                "page_id": page_id,
                "current_url": page.url,
                "new_page_id": new_page_id,
                "active_page_id": self.session.active_page_id,
                "pages": pages,
            }
        except PlaywrightError as exc:
            dom_error = self._locator_action_error("browser_click", page.url, spec, exc)
            if not self.vision_available or not is_vision_fallback_eligible(exc):
                raise dom_error from exc
            return await self._vision_click(
                page_id=page_id,
                page=page,
                spec=spec,
                fallback_reason=str(dom_error),
                expect_popup=arguments.get("expect_popup", False),
            )

    async def fill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "fill")
        try:
            await locator.fill(arguments["value"])
            return {"filled": True, "page_id": page_id, "value": arguments["value"]}
        except PlaywrightError as exc:
            raise self._locator_action_error(
                "browser_fill", page.url, spec, exc
            ) from exc

    async def press(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "press")
        key = arguments["key"]
        try:
            await locator.press(key)
            return {
                "pressed": True,
                "key": key,
                "page_id": page_id,
                "current_url": page.url,
            }
        except PlaywrightError as exc:
            raise self._locator_action_error(
                "browser_press", page.url, spec, exc
            ) from exc

    async def select(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "select")
        try:
            selected = await locator.select_option(value=arguments["value"])
            return {"selected": selected, "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error(
                "browser_select", page.url, spec, exc
            ) from exc

    async def check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "check")
        try:
            await locator.check()
            return {"checked": await locator.is_checked(), "page_id": page_id}
        except PlaywrightError as exc:
            raise self._locator_action_error(
                "browser_check", page.url, spec, exc
            ) from exc

    async def extract_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        try:
            if "locator" in arguments:
                _, locator = self._action_locator(
                    page, arguments["locator"], "extract_text"
                )
                text = await locator.inner_text()
            else:
                text = await page.locator("body").inner_text()
        except PlaywrightError as exc:
            raise BrowserLocatorError(
                f"browser_extract_text failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        limit = min(
            arguments.get("max_chars", self.session.config.max_extract_chars),
            self.session.config.max_extract_chars,
        )
        original_length = len(text)
        return {
            "page_id": page_id,
            "text": text[:limit],
            "truncated": original_length > limit,
            "original_length": original_length,
        }

    async def list_pages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"pages": await self.session.list_pages()}

    async def switch_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page = self.session.switch_page(arguments["page_id"])
        await page.bring_to_front()
        return {
            "page_id": arguments["page_id"],
            "active": True,
            "url": page.url,
            "title": await page.title(),
        }

    async def download(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(page, arguments["locator"], "download")
        try:
            async with page.expect_download() as download_info:
                await locator.click()
            download = await download_info.value
            suggested = download.suggested_filename
            filename = self._sanitize_filename(suggested)
            destination = self._unique_path(self.session.config.download_dir, filename)
            await download.save_as(destination)
            return {
                "filename": destination.name,
                "saved_path": str(destination),
                "suggested_filename": suggested,
                "page_id": page_id,
            }
        except BrowserDownloadError:
            raise
        except PlaywrightError as exc:
            raise BrowserDownloadError(
                f"browser_download failed; page_url={page.url}; "
                f"strategy={spec.strategy.value}; detail={concise_playwright_error(exc)}"
            ) from exc

    async def screenshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id, page = self.session.get_page(arguments.get("page_id"))
        filename = f"screenshot_{self._screenshot_number:04d}.png"
        self._screenshot_number += 1
        destination = self._unique_path(self.session.config.artifact_dir, filename)
        try:
            await page.screenshot(
                path=str(destination),
                full_page=arguments.get("full_page", False),
            )
        except PlaywrightError as exc:
            raise BrowserError(
                f"browser_screenshot failed; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        return {"saved_path": str(destination), "page_id": page_id, "url": page.url}

    async def inspect_click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """等待目标出现后只读取 click 风险 metadata，不触发 click。"""

        _, page = self.session.get_page(arguments.get("page_id"))
        spec, locator = self._action_locator(
            page,
            arguments["locator"],
            "click_preflight",
        )
        try:
            # preflight 可以等待 attached；普通 action 不提前 count，保留 auto-wait。
            await locator.wait_for(state="attached")
            count = await locator.count()
            if count != 1:
                raise BrowserLocatorError(
                    "browser_click preflight 必须唯一定位目标; "
                    f"page_url={page.url}; strategy={spec.strategy.value}; count={count}"
                )
            metadata = await locator.evaluate(
                """element => {
                    const tag = element.tagName.toLowerCase();
                    const form = element.closest("form");
                    const typeValue = "type" in element
                        ? String(element.type || "").toLowerCase()
                        : String(element.getAttribute("type") || "").toLowerCase();
                    const isSubmit =
                        (tag === "button" && typeValue === "submit") ||
                        (tag === "input" && ["submit", "image"].includes(typeValue));
                    return {
                        tag_name: tag,
                        input_type: typeValue,
                        text: String(element.innerText || element.textContent || "").trim().slice(0, 500),
                        value: String(element.value || "").slice(0, 500),
                        aria_label: element.getAttribute("aria-label"),
                        href: tag === "a" ? String(element.href || "") : null,
                        in_form: form !== null,
                        form_method: form ? String(form.method || "get").toLowerCase() : null,
                        is_submit: isSubmit,
                    };
                }"""
            )
        except BrowserLocatorError as exc:
            if self.vision_available and is_vision_fallback_eligible(exc):
                return self._vision_preflight_metadata(page, spec, exc)
            raise
        except PlaywrightError as exc:
            error = BrowserLocatorError(
                "browser_click preflight failed; "
                f"page_url={page.url}; strategy={spec.strategy.value}; "
                f"detail={concise_playwright_error(exc)}"
            )
            if self.vision_available and is_vision_fallback_eligible(exc):
                return self._vision_preflight_metadata(page, spec, error)
            raise error from exc
        metadata["accessible_name"] = spec.name
        metadata["page_url"] = page.url
        metadata["strategy"] = spec.strategy.value
        return metadata

    async def inspect_press(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """只读取按键目标；Enter 在 form 中按 submit 语义保守分类。"""

        metadata = await self.inspect_click(arguments)
        key = arguments["key"]
        metadata["pressed_key"] = key
        metadata["is_submit"] = bool(metadata.get("is_submit")) or (
            key.lower() == "enter" and bool(metadata.get("in_form"))
        )
        return metadata

    async def _vision_click(
        self,
        *,
        page_id: str,
        page: Page,
        spec: LocatorSpec,
        fallback_reason: str,
        expect_popup: bool,
    ) -> dict[str, Any]:
        """在一次获准执行内截图、定位、校验并点击；图片只存在于内存。"""

        assert self.vision_targeter is not None
        viewport = page.viewport_size
        if viewport is None:
            viewport = await page.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
        width = int(viewport["width"])
        height = int(viewport["height"])
        try:
            screenshot = await page.screenshot(type="png", full_page=False)
        except PlaywrightError as exc:
            raise BrowserVisionError(
                "Vision fallback screenshot 失败: " + concise_playwright_error(exc),
                metadata={
                    "status": "rejected",
                    "reason": "screenshot_failed",
                    "fallback_reason": fallback_reason,
                    "viewport_width": width,
                    "viewport_height": height,
                    "screenshot_bytes": 0,
                    "confidence": None,
                },
            ) from exc
        base_metadata: dict[str, Any] = {
            "fallback_reason": fallback_reason,
            "viewport_width": width,
            "viewport_height": height,
            "screenshot_bytes": len(screenshot),
        }
        if len(screenshot) > self.vision_config.max_screenshot_bytes:
            raise BrowserVisionError(
                "Vision fallback screenshot 超出 Host 大小上限",
                metadata={
                    **base_metadata,
                    "status": "rejected",
                    "reason": "screenshot_too_large",
                    "confidence": None,
                },
            )
        request = self._vision_request(spec, width=width, height=height)
        try:
            target = await self.vision_targeter.locate(
                screenshot=screenshot,
                request=request,
            )
            target = validate_vision_target(target, request)
        except Exception as exc:
            raise BrowserVisionError(
                "Vision targeter 返回无效结果: " + concise_playwright_error(exc),
                metadata={
                    **base_metadata,
                    "status": "rejected",
                    "reason": "invalid_target",
                    "confidence": None,
                },
            ) from exc
        if not target.found:
            raise BrowserVisionError(
                f"Vision target 未找到: {target.reason}",
                metadata={
                    **base_metadata,
                    "status": "rejected",
                    "reason": target.reason,
                    "confidence": target.confidence,
                },
            )
        if target.confidence < self.vision_config.min_confidence:
            raise BrowserVisionError(
                "Vision target confidence 低于 Host 阈值: "
                f"{target.confidence} < {self.vision_config.min_confidence}",
                metadata={
                    **base_metadata,
                    "status": "rejected",
                    "reason": "confidence_below_threshold",
                    "confidence": target.confidence,
                },
            )
        assert target.x is not None and target.y is not None
        found_metadata = {
            **base_metadata,
            "status": "found",
            "reason": target.reason,
            "confidence": target.confidence,
        }
        try:
            if expect_popup:
                async with page.expect_popup() as popup_info:
                    await page.mouse.click(target.x, target.y)
                popup = await popup_info.value
                await popup.wait_for_load_state("domcontentloaded")
                new_page_id = self.session.register_page(popup)
            else:
                await page.mouse.click(target.x, target.y)
                new_page_id = None
        except PlaywrightError as exc:
            raise BrowserVisionError(
                "Vision target 已定位但 coordinate click 失败: "
                + concise_playwright_error(exc),
                metadata=found_metadata,
            ) from exc
        return {
            "clicked": True,
            "interaction_mode": "vision",
            "page_id": page_id,
            "current_url": page.url,
            "new_page_id": new_page_id,
            "x": target.x,
            "y": target.y,
            "confidence": target.confidence,
            "vision_reason": target.reason,
            "fallback_reason": fallback_reason,
            "viewport_width": width,
            "viewport_height": height,
            "screenshot_bytes": len(screenshot),
        }

    @staticmethod
    def _vision_preflight_metadata(
        page: Page,
        spec: LocatorSpec,
        error: BrowserLocatorError,
    ) -> dict[str, Any]:
        return {
            "vision_fallback_required": True,
            "fallback_reason": str(error),
            "accessible_name": spec.name,
            "page_url": page.url,
            "strategy": spec.strategy.value,
        }

    @staticmethod
    def _vision_request(
        spec: LocatorSpec,
        *,
        width: int,
        height: int,
    ) -> VisionTargetRequest:
        value = spec.name if spec.strategy.value == "role" else spec.value
        description = f"{spec.role or spec.strategy.value} target named {value!r}"
        return VisionTargetRequest(
            target_description=description,
            role=spec.role,
            accessible_name=value,
            viewport_width=width,
            viewport_height=height,
        )

    def _action_locator(
        self,
        page: Page,
        raw_spec: dict[str, Any],
        action: str,
    ) -> tuple[LocatorSpec, Locator]:
        try:
            spec = LocatorSpec.model_validate(raw_spec)
            locator = resolve_locator(page, spec)
        except Exception as exc:
            if isinstance(exc, BrowserError):
                raise
            raise BrowserLocatorError(
                f"browser_{action} locator invalid; page_url={page.url}; "
                f"detail={concise_playwright_error(exc)}"
            ) from exc
        return spec, locator

    @staticmethod
    def _locator_action_error(
        action: str,
        url: str,
        spec: LocatorSpec,
        exc: Exception,
    ) -> BrowserLocatorError:
        return BrowserLocatorError(
            f"{action} failed; page_url={url}; strategy={spec.strategy.value}; "
            f"detail={concise_playwright_error(exc)}"
        )

    @staticmethod
    def _sanitize_filename(suggested: str) -> str:
        basename = Path(suggested).name
        sanitized = _SAFE_FILENAME.sub("_", basename).strip("._")
        if not sanitized or sanitized in {".", ".."}:
            raise BrowserDownloadError("下载 suggested_filename 无法安全保存")
        return sanitized

    @staticmethod
    def _unique_path(directory: Path, filename: str) -> Path:
        directory = directory.resolve()
        candidate = (directory / filename).resolve()
        if candidate.parent != directory:
            raise BrowserDownloadError("Browser artifact path 超出受控目录")
        counter = 1
        while candidate.exists():
            candidate = (
                directory / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
            )
            counter += 1
        return candidate


class BrowserToolProvider:
    """Host 侧 BrowserSession lifecycle 与 Tool 注册，不参与推理。"""

    def __init__(
        self,
        config: BrowserConfig | None = None,
        *,
        vision_config: VisionConfig | None = None,
        vision_targeter: VisionTargeterLike | None = None,
    ) -> None:
        self.config = config or BrowserConfig()
        self.vision_config = vision_config or VisionConfig()
        self.vision_targeter = vision_targeter
        if self.vision_config.enabled and self.vision_targeter is None:
            self.vision_targeter = MultimodalVisionTargeter(self.vision_config)
        self.session = BrowserSession(self.config)
        self._tools: list[BrowserTool] = []

    async def __aenter__(self) -> "BrowserToolProvider":
        await self.session.__aenter__()
        self._tools = BrowserTools(
            self.session,
            vision_config=self.vision_config,
            vision_targeter=self.vision_targeter,
        ).build()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self._tools = []
        await self.session.__aexit__(exc_type, exc, traceback)

    def register_tools(self, registry: ToolRegistry) -> list[BrowserTool]:
        if not self.session.is_started:
            raise BrowserError("BrowserToolProvider 必须先进入 async context")
        existing = {tool.name for tool in registry.list_tools()}
        conflicts = sorted(tool.name for tool in self._tools if tool.name in existing)
        if conflicts:
            raise BrowserError(f"Browser Tool 与 Registry 名称冲突: {conflicts}")
        for tool in self._tools:
            registry.register(tool)
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]
