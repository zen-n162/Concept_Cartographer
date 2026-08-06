"""Foundry 上の 4 エージェント定義 (新 Agents API `kind: prompt`)。

モデル割当 (qst-cartographer-poc のデプロイ):
  cc-extraction   gpt-5.6-sol    最上位。資料収集 (Work IQ) と KG 抽出の品質が全体を決める
  cc-layout       gpt-5.6-luna   軽量。決定的ツール compute_layout の呼び出しのみ
  cc-projection   gpt-5.6-luna   軽量。render_layout_plan の単発呼び出し
  cc-verification gpt-5.6-terra  中位。描画結果の独立判定

cc-extraction は Work IQ の Remote MCP (OneDrive / SharePoint / M365 Copilot) を
直接持ち、nakamura.zen@qst.go.jp のファイルを Foundry 側で読む。
描画系ツールは function tool として宣言し、実体は手元の cc_orchestrator が実行する
(VM-Excalidraw-MCP は private のため Foundry から直接は到達できない)。
"""

from __future__ import annotations

import os

from cc_orchestrator.foundry_v2 import fn_tool, mcp_tool

MODELS = {
    "extraction": os.environ.get("CC_MODEL_EXTRACTION", "gpt-5.6-sol"),
    "layout": os.environ.get("CC_MODEL_LAYOUT", "gpt-5.6-luna"),
    "projection": os.environ.get("CC_MODEL_PROJECTION", "gpt-5.6-luna"),
    "verification": os.environ.get("CC_MODEL_VERIFICATION", "gpt-5.6-terra"),
}

# プロジェクト接続済みの Work IQ Remote MCP (Cartographer エージェントと同一設定)
A365 = "https://agent365.svc.cloud.microsoft/agents/servers"
WORKIQ_TOOLS = [
    mcp_tool("WorkIQOneDrive", f"{A365}/mcp_OneDriveRemoteServer", "WorkIQOneDrive"),
    mcp_tool("WorkIQSharePoint", f"{A365}/mcp_SharePointRemoteServer", "WorkIQSharePoint"),
    mcp_tool("WorkIQCopilot", f"{A365}/mcp_M365Copilot", "WorkIQCopilot"),
]

EXTRACTION_INSTRUCTIONS = """\
あなたは Concept Cartographer の Extraction Agent です。研究者本人の M365 データから
対象期間の研究資料を集め、概念地図の素材となる knowledge_graph を作ります。

# 手順
1. 資料収集: Work IQ ツールで対象期間 (依頼文の「今週/先週/今月/直近N日」) の
   研究関連ファイルを探して内容を読む。
   - `copilot_chat` (WorkIQCopilot): M365 全体の意味検索・内容要約に最も有効。
     「今週更新した研究関連ファイルの内容を要約して」のように依頼する。
   - `findFileOrFolderInMyDrive` / `getFolderChildrenInMyOnedrive` /
     `readSmallTextFileFromMyOnedrive` (OneDrive)
   - `findFileOrFolder` / `readSmallTextFile` / `findSite` (SharePoint)
   - 会議メモ・予算資料・事務書類など研究内容でないものは除外する。
   - 追加テキストがユーザーメッセージに含まれている場合はそれも資料として使う。
2. 抽出: 集めた内容から概念と関係を取り出し、knowledge_graph JSON **のみ**を出力する
   (前置き・後置き・コードフェンス禁止)。

# 出力形式
{"graph_version": "kg_<短いID>",
 "source_files": ["<使った資料名>"],
 "nodes": [{"id": "c001", "label": "<概念名 25字以内>", "community_id": "comm_001"}],
 "edges": [{"id": "r001", "from": "c001", "to": "c002",
            "label": "<関係の説明 20字以内>", "glyph": "arrow",
            "evidence_span": [{"document_id": "<ファイルID>",
                               "surface": "<原文のままの引用>"}]}],
 "communities": [{"id": "comm_001", "name": "<テーマ名>", "is_gap": false}]}

# 抽出ルール
- glyph: arrow=因果 (機序・介入・反事実の語彙証拠がある場合のみ) / wave=相関・関連 /
  zigzag=矛盾・対立 / double=補強・支持・具体例 / hole=情報不足のギャップ候補。
- 因果の語彙証拠がなければ wave にする。相関を因果へ昇格させない。
- 言及が薄い・未検証のテーマは is_gap: true のコミュニティにまとめ、そこへの関係は
  glyph: hole とする。ギャップは候補であり断定しない。
- コミュニティ 3〜7 個、ノード 8〜20 個。資料にない概念を創作しない。
- id は c001.. / r001.. の連番。edges の from/to は必ず存在する node id を指す。
- 資料横断で同じ概念は 1 ノードに統合する。ラベルは日本語で簡潔に。
- 資料の生文・個人情報・秘密情報をラベル以外に転記しない。
- 資料が 1 件も見つからない場合は {"error": "no_documents", "detail": "<理由>"} を返す。
"""

LAYOUT_INSTRUCTIONS = """\
あなたは Concept Cartographer の Layout Agent です。JSON のみで応答します。
手順 (必ずこの順):
1. 入力の knowledge_graph を `compute_layout` ツールへ渡す。
2. **手順1が返した layout_plan をそのまま** `validate_layout_plan` の plan 引数へ渡す。
   knowledge_graph を渡してはいけない (両者は別物。layout_plan の nodes には x/y がある)。
3. valid: true → {"status":"LAYOUT_OK","nodes":<数>,"edges":<数>,"islands":<数>} のみ出力。
   layout_plan 本体は書き写さない。
4. valid: false → {"status":"LAYOUT_FAILED","errors":[<要約>]} のみ出力。
禁止: 座標や bbox を自分で計算・修正すること。ツール結果の改変。
"""

PROJECTION_INSTRUCTIONS = """\
あなたは Concept Cartographer の Projection Agent です。JSON のみで応答します。
手順: 入力の layout_plan を `render_layout_plan` ツールへ渡す (clear_before=true)。
成功 → {"status":"RENDER_OK","created":<要素数>}
失敗 → {"status":"RENDER_FAILED","errors":[<要約>]}
禁止: layout_plan の改変。render_layout_plan 以外での要素作成。再試行の自己判断。
"""

VERIFICATION_INSTRUCTIONS = """\
あなたは Concept Cartographer の Verification Agent です。描画とは独立に検証のみを行い、
JSON のみで応答します。
手順:
1. `verify_scene` ツールを呼ぶ (引数 plan は省略可。直前の描画対象が使われる)。
2. 必要なら `describe_scene` で概要を確認する。
3. 出力: {"verdict":"PASS"|"FAIL","missing":<数>,"mismatched":<数>,
          "summary":"<50字以内の日本語>"}
   verdict は verify_scene の passed が true のときのみ PASS。
禁止: 描画系ツールの呼び出し。passed=false の独自解釈での PASS 化。
"""

LAYOUT_TOOLS = [
    fn_tool("compute_layout",
            "knowledge_graph から layout_plan を決定的に計算する (座標・島 bbox を算出)",
            {"knowledge_graph": {"type": "object",
                                 "description": "nodes/edges/communities を持つ KG"},
             "detail_level": {"type": "string",
                              "enum": ["overview", "standard", "detailed"]}},
            ["knowledge_graph"]),
    fn_tool("validate_layout_plan",
            "layout_plan をスキーマ + 意味検証 (ID 重複・参照切れ等) する",
            {"plan": {"type": "object"}}, ["plan"]),
]

PROJECTION_TOOLS = [
    fn_tool("render_layout_plan",
            "layout_plan を Excalidraw キャンバスへ描画する (island→node→edge, ロールバック内蔵)",
            {"plan": {"type": "object"},
             "clear_before": {"type": "boolean"}}, ["plan"]),
]

VERIFICATION_TOOLS = [
    fn_tool("verify_scene", "描画結果を layout_plan と突合する",
            {"plan": {"type": "object", "description": "省略時は直前の描画対象"}}, []),
    fn_tool("describe_scene", "キャンバスの人間可読サマリを得る", {}, []),
]

AGENT_SPECS: dict[str, dict] = {
    "cc-extraction": {
        "model": MODELS["extraction"],
        "instructions": EXTRACTION_INSTRUCTIONS,
        "tools": WORKIQ_TOOLS,
        "effort": "medium",
        "description": "Work IQ (OneDrive/SharePoint/Copilot) から研究資料を収集し knowledge_graph を抽出",
        "welcome": "Extraction｜資料収集と概念抽出",
    },
    "cc-layout": {
        "model": MODELS["layout"],
        "instructions": LAYOUT_INSTRUCTIONS,
        "tools": LAYOUT_TOOLS,
        "effort": "low",
        "description": "knowledge_graph から layout_plan を決定的に生成",
        "welcome": "Layout｜配置計画",
    },
    "cc-projection": {
        "model": MODELS["projection"],
        "instructions": PROJECTION_INSTRUCTIONS,
        "tools": PROJECTION_TOOLS,
        "effort": "low",
        "description": "layout_plan を Excalidraw (VM-Excalidraw-MCP) へ描画",
        "welcome": "Projection｜概念地図の描画",
    },
    "cc-verification": {
        "model": MODELS["verification"],
        "instructions": VERIFICATION_INSTRUCTIONS,
        "tools": VERIFICATION_TOOLS,
        "effort": "low",
        "description": "描画結果を layout_plan と突合して独立検証",
        "welcome": "Verification｜描画検証",
    },
}
