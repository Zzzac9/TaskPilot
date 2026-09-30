"""跨 Tool Calls 保持状态的 Playwright BrowserSession。"""

from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from taskpilot.browser.config import BrowserConfig


class BrowserError(RuntimeError):
    """面向 Executor 的 Browser 环境错误基类。"""


class BrowserSessionError(BrowserError):
    """Browser lifecycle 未启动或启动/关闭失败。"""


class BrowserPageError(BrowserError):
    """page_id 未知或页面已关闭。"""


class BrowserLocatorError(BrowserError):
    """Locator 不存在、歧义或 actionability 失败。"""


class BrowserVisionError(BrowserLocatorError):
    """Vision fallback 已请求但目标被拒绝；携带的只有审计元数据。"""

    def __init__(self, message: str, *, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.vision_metadata = metadata


class BrowserNavigationError(BrowserError):
    """URL scheme、navigation 或 navigation timeout 失败。"""


class BrowserDownloadError(BrowserError):
    """下载事件、文件名或受控目录写入失败。"""


def concise_playwright_error(exc: Exception, limit: int = 600) -> str:
    """压缩 Playwright 消息，避免把巨型 traceback 作为 Tool observation。"""

    message = " ".join(str(exc).split())
    return message if len(message) <= limit else message[:limit] + "…"


class BrowserSession:
    """一个 Task/Graph 生命周期内复用 BrowserContext、cookies 和 pages。"""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, Page] = {}
        self._page_ids: dict[Page, str] = {}
        self._next_page_number = 1
        self.active_page_id: str | None = None

    @property
    def is_started(self) -> bool:
        return self._context is not None

    @property
    def browser_version(self) -> str | None:
        return self._browser.version if self._browser is not None else None

    async def __aenter__(self) -> "BrowserSession":
        if self.is_started:
            raise BrowserSessionError("BrowserSession 不能重复进入 context")
        self.config.ensure_directories()
        # 每次进入都代表一个新的 Session 生命周期，page ID 从 1 重新开始。
        self._next_page_number = 1
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless
            )
            self._context = await self._browser.new_context(
                accept_downloads=True,
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
            )
            self._context.set_default_timeout(self.config.action_timeout_ms)
            self._context.set_default_navigation_timeout(
                self.config.navigation_timeout_ms
            )
            self._context.on("page", self.register_page)
            self.register_page(await self._context.new_page())
        except Exception as exc:
            await self._close_resources()
            raise BrowserSessionError(
                f"BrowserSession 启动失败: {concise_playwright_error(exc)}"
            ) from exc
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        await self._close_resources()

    def register_page(self, page: Page) -> str:
        """为新 Page 分配稳定 ID；同一对象重复注册保持原 ID。"""

        existing = self._page_ids.get(page)
        if existing is not None:
            self.active_page_id = existing
            return existing
        page_id = f"page-{self._next_page_number}"
        self._next_page_number += 1
        self._pages[page_id] = page
        self._page_ids[page] = page_id
        self.active_page_id = page_id
        page.on("close", lambda _page: self._remove_page(page))
        return page_id

    def _remove_page(self, page: Page) -> None:
        page_id = self._page_ids.pop(page, None)
        if page_id is None:
            return
        self._pages.pop(page_id, None)
        if self.active_page_id == page_id:
            self.active_page_id = next(iter(self._pages), None)

    def get_page(self, page_id: str | None = None) -> tuple[str, Page]:
        """返回指定或 active Page；未知 ID 清晰失败。"""

        selected_id = page_id or self.active_page_id
        if selected_id is None:
            raise BrowserPageError("BrowserSession 当前没有可用 Page")
        page = self._pages.get(selected_id)
        if page is None or page.is_closed():
            raise BrowserPageError(f"未知或已关闭的 page_id: {selected_id}")
        return selected_id, page

    async def list_pages(self) -> list[dict[str, Any]]:
        """只返回 JSON-serializable 页面元数据。"""

        result: list[dict[str, Any]] = []
        for page_id, page in self._pages.items():
            if page.is_closed():
                continue
            result.append(
                {
                    "page_id": page_id,
                    "active": page_id == self.active_page_id,
                    "title": await page.title(),
                    "url": page.url,
                }
            )
        return result

    def switch_page(self, page_id: str) -> Page:
        """切换 active Page，不依赖 context.pages 数组位置。"""

        _, page = self.get_page(page_id)
        self.active_page_id = page_id
        return page

    async def _close_resources(self) -> None:
        context, browser, playwright = (
            self._context,
            self._browser,
            self._playwright,
        )
        self._context = None
        self._browser = None
        self._playwright = None
        self._pages.clear()
        self._page_ids.clear()
        self.active_page_id = None
        # 即使前一层关闭异常，也继续释放后续进程资源。
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if playwright is not None:
                    await playwright.stop()
