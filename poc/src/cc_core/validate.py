"""layout_plan validation: JSON Schema + semantic checks (引き継ぎメモ §10-1, §10-6).

Semantic checks on top of the schema:
- duplicate node IDs / duplicate edge IDs / duplicate island community_ids
- edges referencing non-existent node IDs (from/to)
- self-loop edges (from == to)
- nodes referencing a community_id with no island entry
- nodes positioned outside their island bbox (warning, not error)
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "layout_plan.schema.json"


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def _load_schema() -> dict[str, Any]:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate_layout_plan(plan: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    # 1) JSON Schema
    validator = jsonschema.Draft202012Validator(_load_schema())
    for err in sorted(validator.iter_errors(plan), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"schema: {path}: {err.message}")
    if errors:
        # Semantic checks assume the basic shape holds; stop here if it doesn't.
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    nodes = plan["nodes"]
    edges = plan.get("edges", [])
    islands = plan.get("islands", [])

    # 2) duplicate IDs
    for kind, ids in (
        ("node", [n["id"] for n in nodes]),
        ("edge", [e["id"] for e in edges]),
        ("island", [i["community_id"] for i in islands]),
    ):
        for dup, count in Counter(ids).items():
            if count > 1:
                errors.append(f"duplicate {kind} id: {dup} (x{count})")

    # 3) edge references
    node_ids = {n["id"] for n in nodes}
    for e in edges:
        for endpoint in ("from", "to"):
            if e[endpoint] not in node_ids:
                errors.append(
                    f"edge {e['id']}: {endpoint}={e[endpoint]} does not exist in nodes"
                )
        if e["from"] == e["to"]:
            errors.append(f"edge {e['id']}: self-loop (from == to)")

    # 4) community / island consistency
    island_ids = {i["community_id"] for i in islands}
    for n in nodes:
        if n["community_id"] not in island_ids:
            errors.append(
                f"node {n['id']}: community_id={n['community_id']} has no island entry"
            )

    # 5) island bbox sanity + node containment (warning only)
    island_by_id = {i["community_id"]: i for i in islands}
    for i in islands:
        x0, y0, x1, y1 = i["bbox"]
        if x1 <= x0 or y1 <= y0:
            errors.append(f"island {i['community_id']}: degenerate bbox {i['bbox']}")
    for n in nodes:
        island = island_by_id.get(n["community_id"])
        if island is None:
            continue
        x0, y0, x1, y1 = island["bbox"]
        if not (x0 <= n["x"] <= x1 and y0 <= n["y"] <= y1):
            warnings.append(
                f"node {n['id']}: position ({n['x']}, {n['y']}) outside island "
                f"{n['community_id']} bbox {island['bbox']}"
            )

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)
