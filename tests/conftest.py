"""测试期共享的纯本地 HTTP fixture。"""

from collections.abc import Generator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    """提供静态测试页面，同时关闭不必要的访问日志。"""

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture(scope="session")
def browser_site_url() -> Generator[str, None, None]:
    """在随机本地端口启动标准库 HTTP server。"""

    site_dir = Path(__file__).parent / "fixtures" / "browser_site"
    handler = partial(_QuietStaticHandler, directory=str(site_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
