"""Post-render verification against the live canvas (引き継ぎメモ §10-5).

`query_elements` (JSON) で機械照合し、`describe_scene` の人間可読サマリも
併せて返す。照合内容:
- 期待した element ID (isl-*/node-*/edge-*) が全て存在するか
- ノード/エッジ/島の個数一致
- ノードラベル・エッジラベルの一致 (ラベル本文はレポートに digest のみ)
"""

from __future__ import annotations

from typing import Any

from cc_core.adapter import (
    GLYPH_STYLES,
    edge_element_id,
    island_element_id,
    island_label_id,
    node_element_id,
)
from cc_core.logging_util import get_logger, label_digest
from cc_core.mcp_client import ExcalidrawClient

logger = get_logger("cc_core.verify")


def _element_label(el: dict[str, Any],
                   texts_by_container: dict[str, str] | None = None) -> str | None:
    """要素のラベルを取り出す。

    ブラウザがキャンバスに接続していると、フロントエンドが label を独立した
    bound text 要素 (containerId=親要素) に変換して同期し返すため、親要素の
    label/text フィールドは消える。その場合は containerId 経由で探す。
    """
    if isinstance(el.get("label"), dict) and el["label"].get("text"):
        return el["label"]["text"]
    if el.get("text"):
        return el["text"]
    if texts_by_container:
        return texts_by_container.get(el["id"])
    return None


def _bound_texts(elements: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for el in elements:
        if el.get("type") == "text" and el.get("containerId") and el.get("text"):
            out.setdefault(el["containerId"], el["text"])
    return out


async def verify_scene(plan: dict[str, Any], client: ExcalidrawClient) -> dict[str, Any]:
    elements = await client.call_json("query_elements", {})
    by_id: dict[str, dict[str, Any]] = {el["id"]: el for el in elements}
    texts_by_container = _bound_texts(elements)

    missing: list[str] = []
    label_mismatches: list[dict[str, str]] = []

    expected_ids: list[str] = []
    for island in plan.get("islands", []):
        expected_ids += [
            island_element_id(island["community_id"]),
            island_label_id(island["community_id"]),
        ]
    for node in plan["nodes"]:
        expected_ids.append(node_element_id(node["id"]))
    for edge in plan.get("edges", []):
        expected_ids.append(edge_element_id(edge["id"]))

    for eid in expected_ids:
        if eid not in by_id:
            missing.append(eid)

    # label checks (digest only in the report — sanitize §10-9)
    for node in plan["nodes"]:
        el = by_id.get(node_element_id(node["id"]))
        if el is not None:
            actual = _element_label(el, texts_by_container)
            if actual != node["label"]:
                label_mismatches.append(
                    {
                        "element": node_element_id(node["id"]),
                        "expected_digest": label_digest(node["label"]),
                        "actual_digest": label_digest(actual),
                    }
                )
    for edge in plan.get("edges", []):
        el = by_id.get(edge_element_id(edge["id"]))
        if el is not None:
            prefix = GLYPH_STYLES[edge["glyph"]]["label_prefix"]
            expected = f"{prefix}{edge.get('label', '')}".strip()
            actual = _element_label(el, texts_by_container)
            if expected and actual != expected:
                label_mismatches.append(
                    {
                        "element": edge_element_id(edge["id"]),
                        "expected_digest": label_digest(expected),
                        "actual_digest": label_digest(actual),
                    }
                )

    # gap rendering check: gap islands must be dashed + translucent (memo §9)
    gap_style_violations: list[str] = []
    for island in plan.get("islands", []):
        if island.get("is_gap"):
            el = by_id.get(island_element_id(island["community_id"]))
            if el is not None and not (
                el.get("strokeStyle") == "dashed" and (el.get("opacity", 100) or 100) < 100
            ):
                gap_style_violations.append(island_element_id(island["community_id"]))

    describe = await client.call("describe_scene")

    passed = not missing and not label_mismatches and not gap_style_violations
    report = {
        "passed": passed,
        "expected_element_count": len(expected_ids),
        "canvas_element_count": len(elements),
        "missing_elements": missing,
        "label_mismatches": label_mismatches,
        "gap_style_violations": gap_style_violations,
        "describe_scene": describe,
    }
    logger.info(
        "verify passed=%s expected=%d canvas=%d missing=%d mismatched=%d",
        passed, len(expected_ids), len(elements), len(missing), len(label_mismatches),
    )
    return report
