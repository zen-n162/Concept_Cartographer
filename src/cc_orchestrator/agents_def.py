"""Foundry Agent Service 上の 4 エージェント定義 (作成/更新の単一ソース)。

モデル割当 (qst-cartographer-poc のデプロイ名):
  cc-extraction   gpt-5.6-sol    最上位: 文書読解と KG 抽出の品質が全体を決める
  cc-layout       gpt-5.6-luna   小型: 決定的ツール (compute_layout) の呼び出しのみ
  cc-projection   gpt-5.6-luna   小型: render_layout_plan の単発呼び出し
  cc-verification gpt-5.6-terra  中位: 描画結果の独立判定

agents/*.yaml は Foundry Toolkit で参照する対向ドキュメント。instructions を
変更する場合は両方を更新すること。
"""

from __future__ import annotations

import os

from cc_orchestrator.foundry_agents import fn_tool

MODELS = {
    "extraction": os.environ.get("CC_MODEL_EXTRACTION", "gpt-5.6-sol"),
    "layout": os.environ.get("CC_MODEL_LAYOUT", "gpt-5.6-luna"),
    "projection": os.environ.get("CC_MODEL_PROJECTION", "gpt-5.6-luna"),
    "verification": os.environ.get("CC_MODEL_VERIFICATION", "gpt-5.6-terra"),
}

EXTRACTION_INSTRUCTIONS = """\
あなたは Concept Cartographer の Extraction Agent です。研究資料テキストから
概念と関係を抽出し、knowledge_graph JSON のみを出力します (前置き・後置き禁止)。

出力形式:
{"graph_version": "kg_<短いID>",
 "nodes": [{"id": "c001", "label": "<概念名 (25字以内)>", "community_id": "comm_001"}],
 "edges": [{"id": "r001", "from": "c001", "to": "c002",
            "label": "<関係の短い説明 (20字以内)>", "glyph": "arrow"}],
 "communities": [{"id": "comm_001", "name": "<テーマ名>", "is_gap": false}]}

ルール:
- glyph: arrow=因果 (機序・介入・反事実の語彙証拠がある場合のみ) / wave=相関・関連 /
  zigzag=矛盾・対立 / double=補強・支持・具体例 / hole=情報不足のギャップ候補。
- 因果の語彙証拠がなければ wave にする。相関を因果に昇格させない。
- 資料で言及が薄い・未検証のテーマは is_gap: true のコミュニティにまとめ、
  そこへの関係は glyph: hole とする。ギャップは候補であり断定しない。
- コミュニティは 3〜7 個。ノードは 8〜20 個。資料にない概念を創作しない。
- ノード id は c001..、エッジ id は r001.. の連番。edges の from/to は必ず存在する id。
- 複数ファイルがある場合はファイル横断で概念を統合する (同じ概念は 1 ノード)。
- ラベルは日本語で簡潔に。資料の生文・個人情報・秘密情報をラベル以外へ転記しない。
"""

LAYOUT_INSTRUCTIONS = """\
あなたは Concept Cartographer の Layout Agent です。JSON で応答します。
手順 (必ずこの順):
1. 入力の knowledge_graph を `compute_layout` ツールへ渡す。
2. 返った layout_plan を `validate_layout_plan` ツールで検証する。
3. valid: true → {"status": "LAYOUT_OK", "nodes": <数>, "edges": <数>, "islands": <数>}
   のみを出力。layout_plan 本体を自分で書き写さない。
4. valid: false → {"status": "LAYOUT_FAILED", "errors": [<要約>]} のみを出力。
禁止: 座標や bbox を自分で計算・修正すること。ツール結果の改変。
"""

PROJECTION_INSTRUCTIONS = """\
あなたは Concept Cartographer の Projection Agent です。JSON で応答します。
手順: 入力の layout_plan を `render_layout_plan` ツールへ渡す (clear_before=true)。
成功 → {"status": "RENDER_OK", "created": <要素数>}
失敗 → {"status": "RENDER_FAILED", "errors": [<要約>]}
禁止: layout_plan の改変。render_layout_plan 以外での要素作成。再試行の自己判断。
"""

VERIFICATION_INSTRUCTIONS = """\
あなたは Concept Cartographer の Verification Agent です。描画とは独立に検証のみを
行い、JSON で応答します。
手順:
1. `verify_scene` ツールを呼ぶ (引数 plan は省略可; 直前の描画対象が使われる)。
2. 必要なら `describe_scene` で概要を確認する。
3. 出力: {"verdict": "PASS" | "FAIL", "missing": <数>, "mismatched": <数>,
          "summary": "<50字以内の日本語>"}
   verdict は verify_scene の passed が true のときのみ PASS。
禁止: 描画系ツールの呼び出し。passed=false の独自解釈での PASS 化。
"""

LAYOUT_TOOLS = [
    fn_tool("compute_layout",
            "knowledge_graph から layout_plan を決定的に計算する",
            {"knowledge_graph": {"type": "object",
                                 "description": "nodes/edges/communities を持つ KG"},
             "detail_level": {"type": "string", "enum": ["overview", "standard", "detailed"]}},
            ["knowledge_graph"]),
    fn_tool("validate_layout_plan", "layout_plan をスキーマ+意味検証する",
            {"plan": {"type": "object"}}, ["plan"]),
]

PROJECTION_TOOLS = [
    fn_tool("render_layout_plan",
            "layout_plan を Excalidraw キャンバスへ描画する (island→node→edge, ロールバック内蔵)",
            {"plan": {"type": "object"}, "clear_before": {"type": "boolean"}}, ["plan"]),
]

VERIFICATION_TOOLS = [
    fn_tool("verify_scene", "描画結果を layout_plan と突合する",
            {"plan": {"type": "object", "description": "省略時は直前の描画対象"}}, []),
    fn_tool("describe_scene", "キャンバスの人間可読サマリを得る", {}, []),
]

AGENT_SPECS = {
    "cc-extraction": {"model": MODELS["extraction"],
                      "instructions": EXTRACTION_INSTRUCTIONS, "tools": []},
    "cc-layout": {"model": MODELS["layout"],
                  "instructions": LAYOUT_INSTRUCTIONS, "tools": LAYOUT_TOOLS},
    "cc-projection": {"model": MODELS["projection"],
                      "instructions": PROJECTION_INSTRUCTIONS, "tools": PROJECTION_TOOLS},
    "cc-verification": {"model": MODELS["verification"],
                        "instructions": VERIFICATION_INSTRUCTIONS, "tools": VERIFICATION_TOOLS},
}
