"""`python -m taskpilot` 的便捷默认入口。"""

import sys
import warnings
from collections.abc import Sequence

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

warnings.filterwarnings(
    "ignore",
    category=LangChainPendingDeprecationWarning,
    message=r"The default value of `allowed_objects` will change.*",
)

from taskpilot.cli import main


DEFAULT_INTERACTIVE_ARGS = ("--interactive", "--browser", "--headed")


def resolve_argv(argv: Sequence[str]) -> list[str]:
    """零参数默认启动带可见浏览器的交互 Shell；有参数时原样转交。"""

    supplied = list(argv)
    return supplied if supplied else list(DEFAULT_INTERACTIVE_ARGS)


def entrypoint(argv: Sequence[str] | None = None) -> int:
    return main(resolve_argv(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(entrypoint())
