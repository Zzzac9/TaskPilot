"""无凭证泄漏的 benchmark environment snapshot。"""

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SECRET_KEY_NAMES = {
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
}
BENCHMARK_ENV_NAMES = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


def _is_secret_key(name: object) -> bool:
    normalized = str(name).casefold().replace("-", "_")
    return normalized in SECRET_KEY_NAMES or normalized.endswith(
        ("_api_key", "_password", "_secret", "_access_token", "_refresh_token")
    )


def load_benchmark_dotenv(path: Path) -> None:
    """只加载 Benchmark 所需三项；不返回、记录或打印 credential。"""

    if not path.is_file():
        return
    allowed = set(BENCHMARK_ENV_NAMES)
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if name not in allowed:
            continue
        if not separator:
            raise ValueError(f".env 中 {name} 缺少等号")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not value:
            raise ValueError(f".env 中 {name} 为空")
        os.environ.setdefault(name, value)


def redact_secrets(value: Any) -> Any:
    """递归清除 key 名和当前环境已知 secret value。"""

    known = [
        item for name in ("LLM_API_KEY", "VISION_API_KEY") if (item := os.getenv(name))
    ]
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _is_secret_key(key) else redact_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        result = value
        for secret in known:
            result = result.replace(secret, "[REDACTED]")
        return result
    return value


def _run_text(command: list[str], cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    # git status --short 的 leading column 有语义，只移除结尾换行。
    return completed.returncode, completed.stdout.rstrip()


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def detect_chromium_version() -> str:
    """隔离 subprocess 探测版本，避免 Sync API 进入 async runner loop。"""

    script = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); "
        "b=p.chromium.launch(headless=True); "
        "print(b.version); b.close(); p.stop()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("无法启动 Playwright Chromium 记录实际版本")
    return completed.stdout.strip()


def capture_environment(repo_root: Path) -> tuple[dict[str, Any], str, str]:
    """返回 environment.json 内容、pip freeze 和 pip check 原始输出。"""

    commit_code, commit = _run_text(["git", "rev-parse", "HEAD"], repo_root)
    status_code, status = _run_text(["git", "status", "--short"], repo_root)
    freeze_code, freeze = _run_text([sys.executable, "-m", "pip", "freeze"], repo_root)
    check_code, pip_check = _run_text([sys.executable, "-m", "pip", "check"], repo_root)
    if commit_code or status_code or freeze_code:
        raise RuntimeError("无法记录 git/pip environment metadata")
    base_url = os.getenv("LLM_BASE_URL")
    base_host = urlsplit(base_url).netloc if base_url else "default-provider-endpoint"
    metadata = {
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "os": platform.platform(),
        "taskpilot_git_commit": commit,
        "git_dirty": bool(status),
        "git_status_short": status.splitlines(),
        "dependencies": {
            name: package_version(name)
            for name in (
                "taskpilot",
                "playwright",
                "langgraph",
                "langchain-core",
                "mcp",
                "fastapi",
                "starlette",
                "pydantic",
            )
        },
        "playwright_version": package_version("playwright"),
        "chromium_version": detect_chromium_version(),
        "model_provider": base_host,
        "model_name": os.getenv("LLM_MODEL") or "unavailable",
        "model_temperature": 0,
        "model_seed": "unavailable",
        "vision_enabled": os.getenv("TASKPILOT_VISION_ENABLED", "false").casefold()
        == "true",
        "vision_provider": (urlsplit(os.getenv("VISION_BASE_URL", "")).netloc or None),
        "vision_model": os.getenv("VISION_MODEL"),
        "pip_check_exit_code": check_code,
    }
    return redact_secrets(metadata), freeze, redact_secrets(pip_check)


def write_environment_files(run_dir: Path, repo_root: Path) -> dict[str, Any]:
    metadata, freeze, pip_check = capture_environment(repo_root)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "environment.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "pip-freeze.txt").write_text(freeze + "\n", encoding="utf-8")
    (run_dir / "pip-check.txt").write_text(str(pip_check) + "\n", encoding="utf-8")
    return metadata
