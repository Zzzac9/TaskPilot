"""Primary benchmark 使用的真实本地 MCP STDIO subprocess。"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


server = MCPServer("taskpilot-benchmark")

GYMS = [
    {
        "id": "g1",
        "name": "Harbor Gym",
        "district": "Central",
        "monthly_hkd": 420,
        "rating": 4.4,
    },
    {
        "id": "g2",
        "name": "Peak Fitness",
        "district": "Wan Chai",
        "monthly_hkd": 480,
        "rating": 4.7,
    },
    {
        "id": "g3",
        "name": "Island Strength",
        "district": "North Point",
        "monthly_hkd": 390,
        "rating": 4.2,
    },
    {
        "id": "g4",
        "name": "Luxury Club",
        "district": "Admiralty",
        "monthly_hkd": 880,
        "rating": 4.9,
    },
]

PRODUCTS = {
    "p100": {"name": "Atlas Keyboard", "price": 699, "stock": 8},
    "p200": {"name": "Beacon Mouse", "price": 299, "stock": 0},
    "p300": {"name": "Cedar Stand", "price": 459, "stock": 12},
}


@server.tool(
    description="List deterministic Hong Kong Island gym candidates with prices and ratings.",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
    structured_output=True,
)
def lookup_candidates(max_monthly_hkd: int = 10000) -> dict[str, object]:
    return {
        "candidates": [item for item in GYMS if item["monthly_hkd"] <= max_monthly_hkd]
    }


@server.tool(
    description="Look up one product record by product_id.",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
    structured_output=True,
)
def lookup_record(product_id: str) -> dict[str, object]:
    if product_id not in PRODUCTS:
        raise ToolError(f"unknown product: {product_id}")
    return {"product_id": product_id, **PRODUCTS[product_id]}


@server.tool(
    description="Calculate a deterministic arithmetic expression with an allowlisted operator.",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
    structured_output=True,
)
def calculate(a: float, b: float, operation: str) -> dict[str, float | str]:
    operations = {
        "add": a + b,
        "subtract": a - b,
        "multiply": a * b,
        "divide": a / b,
    }
    if operation not in operations:
        raise ToolError("operation must be add/subtract/multiply/divide")
    return {"operation": operation, "result": operations[operation]}


@server.tool(
    description="Controlled unavailable source used by dead-end replanning tasks."
)
def source_a(query: str) -> str:
    raise ToolError(f"source A unavailable for {query}")


@server.tool(
    description="Alternate deterministic source for recovery after source A fails.",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
    structured_output=True,
)
def source_b(query: str) -> dict[str, str]:
    answers = {
        "support_email": "support@taskpilot.local",
        "warehouse": "Kowloon Bay",
        "policy_version": "2026.09",
        "fallback_code": "ALT-204",
    }
    if query not in answers:
        raise ToolError(f"alternate source has no record for {query}")
    return {"query": query, "value": answers[query], "source": "B"}


@server.tool(
    description="Controlled high-risk write fixture; benchmark driver decides approval.",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
    structured_output=True,
)
def untrusted_write(record_id: str, value: str) -> dict[str, str]:
    return {"record_id": record_id, "written": value}


if __name__ == "__main__":
    server.run(transport="stdio")
