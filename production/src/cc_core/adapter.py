"""layout_plan -> Excalidraw MCP tool-call adapter (引き継ぎメモ §10-2〜§10-4, §10-7〜§10-9).

描画順は island -> node -> edge。element ID は layout_plan の ID から決定的に
生成し (isl-*/node-*/edge-*)、plan と canvas の対応表を返す。部分失敗時は
作成済み要素を逆順で削除してロールバックする。

Glyph mapping (メモ §9 の変換ルール / 色は v4 §8.3 の UI 記号色):
  arrow  (因果)          -> 赤 実線 矢印
  wave   (相関)          -> 青 点線 矢印なし + ラベル前置 "〜"
  zigzag (矛盾)          -> 橙 実線 + ラベル前置 "⚡"
  double (補強)          -> 緑 太線 + ラベル前置 "⇒"
  hole   (ギャップ候補)   -> 灰 破線 + 半透明 (確定事項として描画しない) + "?"
  tension(対立候補)      -> 灰 破線 + 半透明 (R1: 矛盾を断定しない, 裁定7) + "?"
  isa    (分類)          -> 青 実線 細 + 白抜き三角 + "◇"      (R2a 追加)
  partof (構成)          -> 菫 実線 細 + 矢頭なし + "◧"        (R2a 追加)
  precedes(時系列先行)   -> 灰 実線 細 + 矢印 + "▷"            (R2a 追加)
  question(疑問)         -> 橙 点線 細 + 矢頭なし + "?"        (R2a 追加)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cc_core.logging_util import get_logger, label_digest
from cc_core.mcp_client import ExcalidrawClient, ToolCallError
from cc_core.overlap import plan_label_layout
from cc_core.validate import validate_layout_plan

logger = get_logger("cc_core.adapter")

GLYPH_STYLES: dict[str, dict[str, Any]] = {
    "arrow": {
        "strokeColor": "#c92a2a", "strokeStyle": "solid", "strokeWidth": 2,
        "endArrowhead": "arrow", "opacity": 100, "label_prefix": "",
    },
    "wave": {
        "strokeColor": "#1971c2", "strokeStyle": "dotted", "strokeWidth": 2,
        "endArrowhead": None, "opacity": 100, "label_prefix": "〜 ",
    },
    "zigzag": {
        "strokeColor": "#e8590c", "strokeStyle": "solid", "strokeWidth": 2,
        "endArrowhead": "bar", "opacity": 100, "label_prefix": "⚡ ",
    },
    "double": {
        "strokeColor": "#2f9e44", "strokeStyle": "solid", "strokeWidth": 3,
        "endArrowhead": "triangle", "opacity": 100, "label_prefix": "⇒ ",
    },
    "hole": {
        "strokeColor": "#868e96", "strokeStyle": "dashed", "strokeWidth": 2,
        "endArrowhead": "dot", "opacity": 40, "label_prefix": "? ",
    },
    # tension = 対立候補。矛盾判定は L8 (R2) の役割なので R1 では断定しない
    # (実運用計画 裁定7)。灰・破線・半透明で「候補」であることを示す。
    "tension": {
        "strokeColor": "#868e96", "strokeStyle": "dashed", "strokeWidth": 2,
        "endArrowhead": None, "opacity": 55, "label_prefix": "? ",
    },
    # --- R2a で追加した 4 種 (設計書 §2)。層 A/C/D の主要な関係を、
    # 既存パレットの流儀 (色 = 意味、破線 = 非断定) のまま拡張する。
    # 因果 (赤) より弱い線幅にしてあるのは、地図を見たときに → が最も強い
    # 主張として立ち上がる序列を崩さないため。
    "isa": {
        "strokeColor": "#1971c2", "strokeStyle": "solid", "strokeWidth": 1.5,
        "endArrowhead": "triangle", "opacity": 100, "label_prefix": "◇ ",
    },
    "partof": {
        "strokeColor": "#6741d9", "strokeStyle": "solid", "strokeWidth": 1.5,
        "endArrowhead": None, "opacity": 100, "label_prefix": "◧ ",
    },
    "precedes": {
        "strokeColor": "#495057", "strokeStyle": "solid", "strokeWidth": 1.5,
        "endArrowhead": "arrow", "opacity": 100, "label_prefix": "▷ ",
    },
    "question": {
        "strokeColor": "#e8590c", "strokeStyle": "dotted", "strokeWidth": 1.5,
        "endArrowhead": None, "opacity": 90, "label_prefix": "? ",
    },
}

NODE_HEIGHT_RATIO = 0.55
GAP_OPACITY = 40
NODE_FONT_SIZE = 14   # cc_core.layout.NODE_FONT と一致させること
EDGE_FONT_SIZE = 12   # cc_core.layout.EDGE_FONT と一致させること


def island_element_id(community_id: str) -> str:
    return f"isl-{community_id}"


def island_label_id(community_id: str) -> str:
    return f"isl-{community_id}-label"


def node_element_id(node_id: str) -> str:
    return f"node-{node_id}"


def edge_element_id(edge_id: str) -> str:
    return f"edge-{edge_id}"


@dataclass
class RenderResult:
    success: bool
    element_map: dict[str, str] = field(default_factory=dict)  # plan id -> canvas element id
    created: list[str] = field(default_factory=list)           # canvas element ids in draw order
    errors: list[str] = field(default_factory=list)
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "element_map": self.element_map,
            "created": self.created,
            "errors": self.errors,
            "rolled_back": self.rolled_back,
        }


async def render_layout_plan(
    plan: dict[str, Any],
    client: ExcalidrawClient,
    *,
    clear_before: bool = False,
    rollback_on_error: bool = True,
) -> RenderResult:
    """Validate then draw a layout_plan onto the Excalidraw canvas."""
    result = RenderResult(success=False)

    validation = validate_layout_plan(plan)
    if not validation.valid:
        result.errors = [f"validation: {e}" for e in validation.errors]
        return result

    if clear_before:
        await client.call("clear_canvas")
        logger.info("canvas cleared before render")

    async def create(plan_key: str, args: dict[str, Any]) -> None:
        text = await client.call("create_element", args)
        # The server echoes the created element as JSON; trust our custom id.
        canvas_id = args["id"]
        result.element_map[plan_key] = canvas_id
        result.created.append(canvas_id)
        logger.info(
            "created kind=%s id=%s label#=%s",
            args["type"], canvas_id, label_digest(args.get("text")),
        )
        del text  # response body intentionally not logged (sanitize §10-9)

    try:
        # --- 1) islands ---
        for island in plan.get("islands", []):
            x0, y0, x1, y1 = island["bbox"]
            is_gap = bool(island.get("is_gap", False))
            await create(
                island_element_id(island["community_id"]),
                {
                    "id": island_element_id(island["community_id"]),
                    "type": "rectangle",
                    "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0,
                    "strokeColor": "#868e96" if is_gap else "#495057",
                    # レイアウト v3 §5: 島の淡い地色。ギャップ島 (と grid
                    # エンジンの plan) には tint が無いので従来どおり透明。
                    # fillStyle は Excalidraw 既定が hachure (斜線) なので、
                    # 淡色がベタ塗りになるよう solid を明示 (excalidraw_file と
                    # 三面一致。tint 無しの transparent には効かず無害)。
                    "backgroundColor": island.get("tint") or "transparent",
                    "fillStyle": "solid",
                    "strokeStyle": "dashed" if is_gap else "solid",
                    "strokeWidth": 1,
                    "roughness": 2,
                    "opacity": GAP_OPACITY if is_gap else 100,
                },
            )
            await create(
                island_label_id(island["community_id"]),
                {
                    "id": island_label_id(island["community_id"]),
                    "type": "text",
                    "x": x0 + 10, "y": y0 + 8,
                    "text": ("❓ " if is_gap else "") + island["name"],
                    "fontSize": 16,
                    "fontFamily": "hand",
                    "strokeColor": "#868e96" if is_gap else "#495057",
                    "opacity": GAP_OPACITY if is_gap else 100,
                },
            )

        # --- 2) nodes ---
        gap_communities = {
            i["community_id"] for i in plan.get("islands", []) if i.get("is_gap")
        }
        for node in plan["nodes"]:
            style = node.get("style", {})
            in_gap = node["community_id"] in gap_communities
            node_h = node.get("height", max(60, node["size"] * NODE_HEIGHT_RATIO))
            await create(
                node_element_id(node["id"]),
                {
                    "id": node_element_id(node["id"]),
                    "type": "ellipse",
                    "x": node["x"], "y": node["y"],
                    "width": node["size"],
                    "height": node_h,
                    "strokeColor": style.get("strokeColor", "#1e1e1e"),
                    "backgroundColor": style.get("backgroundColor", "#fff9db"),
                    "strokeStyle": "dashed" if in_gap else "solid",
                    "strokeWidth": 1,
                    "roughness": 2 if style.get("rough", True) else 0,
                    "opacity": GAP_OPACITY if in_gap else 100,
                    "text": node["label"],
                    "fontSize": NODE_FONT_SIZE,
                    "fontFamily": "hand",
                },
            )

        # --- 3) edges ---
        # ラベル位置はビュー全体の一括プランナーが決める (設計書 裁定 AA)。
        # svg_export も同じ関数を呼ぶので、canvas と SVG の位置が一致する。
        placements = plan_label_layout(plan)
        for edge in plan.get("edges", []):
            glyph = GLYPH_STYLES[edge["glyph"]]
            placement = placements.get(edge["id"])
            # プランナーが短縮したときは短縮後の文字列を描く (裁定 AC)
            label = placement.text if placement is not None else edge.get("label", "")
            args: dict[str, Any] = {
                "id": edge_element_id(edge["id"]),
                "type": "arrow",
                "x": 0, "y": 0,
                "startElementId": node_element_id(edge["from"]),
                "endElementId": node_element_id(edge["to"]),
                "strokeColor": glyph["strokeColor"],
                "strokeStyle": glyph["strokeStyle"],
                "strokeWidth": glyph["strokeWidth"],
                "opacity": glyph["opacity"],
                "roughness": 2,
            }
            if glyph["endArrowhead"]:
                args["endArrowhead"] = glyph["endArrowhead"]

            text = f"{glyph['label_prefix']}{label}".strip()
            # 中点に置くと他ノード・島タイトル・他ラベルへ重なるエッジ
            # (中間ノードを飛び越す線など) は、ラベルを線に紐付けず、
            # プランナーが決めた位置へ独立テキストとして逃がす
            retreated = bool(text and placement is not None and placement.retreated)
            if text and not retreated:
                args["text"] = text
                args["fontSize"] = EDGE_FONT_SIZE
                args["fontFamily"] = "hand"
            await create(edge_element_id(edge["id"]), args)

            if retreated:
                assert placement is not None  # retreated が True なら必ず在る
                # プランナーの x/y はラベル矩形の中心。Excalidraw の text は
                # 左上原点なので、幅・高さの半分だけ戻す
                width, height = placement.width, placement.height
                await create(
                    edge_element_id(edge["id"]) + "-label",
                    {
                        "id": edge_element_id(edge["id"]) + "-label",
                        "type": "text",
                        "x": placement.x - width / 2, "y": placement.y - height / 2,
                        "text": text,
                        "fontSize": EDGE_FONT_SIZE,
                        "fontFamily": "hand",
                        "strokeColor": glyph["strokeColor"],
                        "opacity": glyph["opacity"],
                    },
                )

        result.success = True
        logger.info(
            "render complete plan=%s elements=%d",
            plan.get("provenance", {}).get("graph_version", "?"),
            len(result.created),
        )
        return result

    except ToolCallError as exc:
        result.errors.append(str(exc))
        logger.error("render failed after %d elements: %s", len(result.created), exc)
        if rollback_on_error and result.created:
            result.rolled_back = await _rollback(client, result.created)
        return result


async def _rollback(client: ExcalidrawClient, created: list[str]) -> bool:
    """Delete already-created elements in reverse order (§10-8)."""
    ok = True
    for element_id in reversed(created):
        try:
            await client.call("delete_element", {"id": element_id})
        except ToolCallError:
            ok = False
            logger.error("rollback: failed to delete %s", element_id)
    logger.info("rollback finished ok=%s count=%d", ok, len(created))
    return ok
