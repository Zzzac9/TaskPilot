"""Browser observation identity and deterministic reuse helpers."""

import hashlib
import json
from typing import Any, Sequence

from taskpilot.models import ToolCallRecord, ToolCallStatus


def browser_observation_fingerprint(record: ToolCallRecord) -> str | None:
    """Return a stable fingerprint for a successful browser_observe result."""

    if (
        record.tool_name != "browser_observe"
        or record.status is not ToolCallStatus.SUCCESS
        or not isinstance(record.result, dict)
    ):
        return None
    result = record.result
    stable = {
        key: result.get(key)
        for key in (
            "page_id",
            "url",
            "title",
            "aria_snapshot",
            "snapshot_mode",
            "depth",
            "truncated",
            "original_length",
            "pages",
        )
    }
    rendered = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def browser_observation_page(record: ToolCallRecord) -> str | None:
    """Resolve the durable page identity used by an observation."""

    if isinstance(record.result, dict):
        page_id = record.result.get("page_id")
        if page_id:
            return str(page_id)
        url = record.result.get("url")
        if url:
            return str(url)
    page_id = record.arguments.get("page_id")
    return str(page_id) if page_id else None


def repeated_observation_streak(
    records: Sequence[ToolCallRecord],
    *,
    step_id: int,
    proposed_arguments: dict[str, Any],
) -> tuple[int, str | None, str | None]:
    """Describe an unchanged consecutive observation streak for the same page.

    At least two already executed observations are required. This avoids treating
    a single refresh of a dynamically changing page as a proven duplicate.
    """

    current = [record for record in records if record.step_id == step_id]
    streak: list[ToolCallRecord] = []
    for record in reversed(current):
        fingerprint = browser_observation_fingerprint(record)
        if fingerprint is None:
            break
        if not streak:
            streak.append(record)
            continue
        if (
            fingerprint != browser_observation_fingerprint(streak[0])
            or browser_observation_page(record) != browser_observation_page(streak[0])
        ):
            break
        streak.append(record)
    if len(streak) < 2:
        return 0, None, None
    page = browser_observation_page(streak[0])
    proposed_page = proposed_arguments.get("page_id")
    if proposed_page is not None and page is not None and str(proposed_page) != page:
        return 0, None, None
    proposed_depth = proposed_arguments.get("depth")
    latest_depth = (
        streak[0].result.get("depth")
        if isinstance(streak[0].result, dict)
        else streak[0].arguments.get("depth")
    )
    if proposed_depth is not None and latest_depth is not None:
        if proposed_depth != latest_depth:
            return 0, None, None
    return len(streak), page, browser_observation_fingerprint(streak[0])


def collapse_repeated_observations(
    records: Sequence[ToolCallRecord],
) -> tuple[list[ToolCallRecord], list[dict[str, Any]]]:
    """Collapse consecutive identical observations while preserving newest data."""

    collapsed: list[ToolCallRecord] = []
    reuse: list[dict[str, Any]] = []
    for record in records:
        fingerprint = browser_observation_fingerprint(record)
        previous = collapsed[-1] if collapsed else None
        if (
            fingerprint is not None
            and previous is not None
            and fingerprint == browser_observation_fingerprint(previous)
            and browser_observation_page(record) == browser_observation_page(previous)
        ):
            if reuse and reuse[-1]["fingerprint"] == fingerprint[:12]:
                reuse[-1]["duplicate_count"] += 1
                reuse[-1]["latest_call_id"] = record.call_id
            else:
                reuse.append(
                    {
                        "tool_name": "browser_observe",
                        "page": browser_observation_page(record),
                        "fingerprint": fingerprint[:12],
                        "duplicate_count": 1,
                        "latest_call_id": record.call_id,
                    }
                )
            collapsed[-1] = record
            continue
        collapsed.append(record)
    return collapsed, reuse
