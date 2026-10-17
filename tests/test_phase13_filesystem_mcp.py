"""Phase 13 Browser -> local MCP -> real DOCX end-to-end tests."""

import json
from pathlib import Path
from typing import Any, Sequence, cast
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from taskpilot.browser import BrowserConfig, BrowserToolProvider
from taskpilot.executor import FINISH_STEP_NAME, TaskExecutor
from taskpilot.filesystem_mcp import (
    FileAlreadyExistsError,
    PathOutsideSandboxError,
    SandboxedFilesystem,
    create_filesystem_mcp_config,
)
from taskpilot.graph import build_graph
from taskpilot.mcp import MCPToolExecutionError, MCPToolProvider
from taskpilot.models import (
    CriterionCheck,
    PlanStep,
    StepVerificationResult,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from taskpilot.policy import DefaultRiskPolicy, PolicyOutcome, RiskLevel
from taskpilot.service import create_initial_state
from taskpilot.tools import ToolRegistry


def _docx_text(path: Path) -> list[str]:
    """Extract paragraph text independently from the generated OOXML package."""

    with ZipFile(path) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    return [
        "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        for paragraph in document.findall(".//w:p", namespace)
    ]


def test_sandbox_blocks_escape_and_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    filesystem = SandboxedFilesystem(root)

    first = filesystem.write_file("notes/report.txt", "first")

    assert first["path"] == "notes/report.txt"
    assert (root / "notes" / "report.txt").read_text(encoding="utf-8") == "first"
    with pytest.raises(FileAlreadyExistsError, match="overwrite"):
        filesystem.write_file("notes/report.txt", "second")
    assert (root / "notes" / "report.txt").read_text(encoding="utf-8") == "first"
    with pytest.raises(PathOutsideSandboxError, match="outside"):
        filesystem.write_file("../escaped.txt", "blocked")
    with pytest.raises(PathOutsideSandboxError, match="absolute"):
        filesystem.read_file(str((tmp_path / "outside.txt").resolve()))
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_real_filesystem_mcp_read_write_docx_and_policy(tmp_path: Path) -> None:
    root = tmp_path / "mcp-root"
    registry = ToolRegistry()

    async with MCPToolProvider([create_filesystem_mcp_config(root)]) as provider:
        adapters = await provider.register_tools(registry)
        assert {item.name for item in adapters} == {
            "filesystem__read_file",
            "filesystem__write_file",
            "filesystem__generate_docx",
        }

        write_result = await registry.invoke(
            "filesystem__write_file",
            {"path": "research.txt", "content": "Harbor Gym: HKD 420"},
        )
        read_result = await registry.invoke(
            "filesystem__read_file",
            {"path": "research.txt"},
        )
        docx_result = await registry.invoke(
            "filesystem__generate_docx",
            {
                "path": "reports/gyms.docx",
                "title": "Gym Research",
                "paragraphs": ["Harbor Gym costs HKD 420 per month."],
                "sources": ["http://127.0.0.1/example"],
            },
        )

        with pytest.raises(MCPToolExecutionError, match="outside"):
            await registry.invoke(
                "filesystem__write_file",
                {"path": "../escape.txt", "content": "blocked"},
            )
        with pytest.raises(MCPToolExecutionError, match="overwrite"):
            await registry.invoke(
                "filesystem__write_file",
                {"path": "research.txt", "content": "replacement"},
            )
        with pytest.raises(MCPToolExecutionError, match="overwrite"):
            await registry.invoke(
                "filesystem__generate_docx",
                {
                    "path": "reports/gyms.docx",
                    "title": "Replacement",
                    "paragraphs": ["This must not replace the original."],
                },
            )

        tools = {item.name: item for item in adapters}
        read_policy = await DefaultRiskPolicy().assess(
            call_id="read-policy",
            step_id=1,
            tool=tools["filesystem__read_file"],
            arguments={"path": "research.txt"},
        )
        write_policy = await DefaultRiskPolicy().assess(
            call_id="write-policy",
            step_id=1,
            tool=tools["filesystem__generate_docx"],
            arguments={"path": "reports/gyms.docx"},
        )

    assert write_result["structured_content"]["path"] == "research.txt"
    assert read_result["structured_content"]["content"] == "Harbor Gym: HKD 420"
    assert docx_result["structured_content"]["path"] == "reports/gyms.docx"
    assert _docx_text(root / "reports" / "gyms.docx") == [
        "Gym Research",
        "Harbor Gym costs HKD 420 per month.",
        "Sources",
        "http://127.0.0.1/example",
    ]
    assert read_policy.risk_level is RiskLevel.LOW
    assert read_policy.outcome is PolicyOutcome.ALLOW
    assert write_policy.risk_level is RiskLevel.MEDIUM
    assert write_policy.outcome is PolicyOutcome.ALLOW


class _ScriptedModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.bound_tools: list[dict[str, Any]] = []
        self.invocations: list[list[BaseMessage]] = []

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> "_ScriptedModel":
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return self.responses.pop(0)


def _call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}
        ],
    )


class _Analyzer:
    def analyze(self, user_input: str) -> TaskSpec:
        return TaskSpec(
            goal=user_input,
            constraints={"source": "localhost", "output": "gyms.docx"},
            completion_criteria=["Create a Word report from rendered web facts"],
        )


class _Planner:
    def plan(self, task_spec: TaskSpec) -> TaskPlan:
        return TaskPlan(
            steps=[
                PlanStep(
                    id=1,
                    description="Research the page and create the local Word report",
                    success_criteria=[
                        "Rendered gym prices are collected",
                        "A Word document is written through Filesystem MCP",
                    ],
                )
            ]
        )


class _Verifier:
    def verify_step(self, **kwargs: Any) -> StepVerificationResult:
        step = kwargs["step"]
        return StepVerificationResult(
            step_id=step.id,
            verified=True,
            checks=[
                CriterionCheck(
                    criterion_index=0,
                    satisfied=True,
                    reason="Browser and MCP tool records provide the chain evidence.",
                ),
                CriterionCheck(
                    criterion_index=1,
                    satisfied=True,
                    reason="The Filesystem MCP returned a concrete relative DOCX path.",
                ),
            ],
        )

    def verify_task(self, **kwargs: Any) -> VerificationResult:
        return VerificationResult(
            completed=True,
            reason="The verified step reports the generated Word document.",
        )


@pytest.mark.asyncio
async def test_full_graph_browser_to_local_mcp_creates_verified_word_content(
    browser_site_url: str,
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    source_url = f"{browser_site_url}/details.html"
    model = _ScriptedModel(
        [
            _call("browser_navigate", {"url": source_url}, "phase13-nav"),
            _call("browser_extract_text", {}, "phase13-extract"),
            _call(
                "filesystem__generate_docx",
                {
                    "path": "gyms.docx",
                    "title": "Hong Kong Gym Price Research",
                    "paragraphs": [
                        "Harbor Gym costs HKD 420 per month.",
                        "Peak Fitness costs HKD 480 per month.",
                    ],
                    "sources": [source_url],
                },
                "phase13-docx",
            ),
            _call(
                FINISH_STEP_NAME,
                {
                    "summary": "Browsed the source and generated the Word report.",
                    "evidence": [
                        "browser_extract_text returned both rendered prices",
                        "filesystem__generate_docx returned gyms.docx",
                    ],
                    "output": {"document": "gyms.docx"},
                },
                "phase13-finish",
            ),
        ]
    )
    registry = ToolRegistry()
    browser_config = BrowserConfig(
        headless=True,
        action_timeout_ms=3_000,
        navigation_timeout_ms=5_000,
        download_dir=tmp_path / "downloads",
        artifact_dir=tmp_path / "screenshots",
    )

    async with MCPToolProvider(
        [create_filesystem_mcp_config(root)]
    ) as filesystem_provider, BrowserToolProvider(browser_config) as browser_provider:
        await filesystem_provider.register_tools(registry)
        browser_provider.register_tools(registry)
        executor = TaskExecutor(
            registry=registry,
            model=cast(BaseChatModel, model),
            max_actions_per_step=4,
            policy=DefaultRiskPolicy(),
        )
        result = await build_graph(
            analyzer=_Analyzer(),
            planner=_Planner(),
            executor=executor,
            verifier=_Verifier(),
        ).ainvoke(
            create_initial_state(
                "Browse the local source and create a Word gym report",
                task_id="phase-13-browser-mcp-word",
                browser_enabled=True,
            )
        )

    document = root / "gyms.docx"
    paragraphs = _docx_text(document)
    assert result["status"] is TaskStatus.COMPLETED
    assert result["error"] is None
    assert [record.tool_name for record in result["tool_calls"]] == [
        "browser_navigate",
        "browser_extract_text",
        "filesystem__generate_docx",
    ]
    assert [decision.risk_level for decision in result["policy_decisions"]] == [
        RiskLevel.LOW,
        RiskLevel.LOW,
        RiskLevel.MEDIUM,
    ]
    assert document.is_file()
    assert paragraphs == [
        "Hong Kong Gym Price Research",
        "Harbor Gym costs HKD 420 per month.",
        "Peak Fitness costs HKD 480 per month.",
        "Sources",
        source_url,
    ]
    assert "Harbor Gym costs HKD 420" in "\n".join(paragraphs)
    assert "Peak Fitness costs HKD 480" in "\n".join(paragraphs)
    print(
        "PHASE13_BROWSER_MCP_WORD="
        + json.dumps(
            {
                "status": result["status"].value,
                "tool_chain": [
                    record.tool_name for record in result["tool_calls"]
                ],
                "document": str(document),
                "paragraphs": paragraphs,
            },
            ensure_ascii=False,
        )
    )
