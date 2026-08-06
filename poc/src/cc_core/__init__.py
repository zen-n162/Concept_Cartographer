"""Concept Cartographer core: layout_plan validation, deterministic layout,
Excalidraw MCP adapter, and scene verification.

layout_plan.json (schemas/layout_plan.schema.json) is the single contract
between the knowledge-graph side and the drawing side. Nothing else crosses
that boundary (引き継ぎメモ §9).
"""

from cc_core.validate import validate_layout_plan
from cc_core.layout import compute_layout
from cc_core.adapter import render_layout_plan, RenderResult
from cc_core.verify import verify_scene

__all__ = [
    "validate_layout_plan",
    "compute_layout",
    "render_layout_plan",
    "RenderResult",
    "verify_scene",
]
