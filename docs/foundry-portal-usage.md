# Foundry ポータル（Web）から概念地図を作る

CLI（`cc_orchestrator.chat`）と同じことを Foundry ポータルのチャットから実行する手順と、
その背後にある制約の説明。

## 使い方（3 ステップ）

1. Foundry ポータル → プロジェクト **qst-cartographer-poc** → **エージェント**
2. **`cc-cartographer-portal`** を開く（Playground が開く）
3. 「**今週の研究を概念地図として整理して**」と入力

返信に `concept_map.excalidraw` が添付されるので、ダウンロードして
[excalidraw.com](https://excalidraw.com) か手元の Excalidraw（`http://127.0.0.1:3000`）で開く。

実測（2026-08-05）: OneDrive/SharePoint/Teams から資料 5 件を収集 → 概念 20 / 関係 23 /
島 5（うち 1 島がギャップ候補）→ 96 要素の `.excalidraw` を添付。ダウンロードして
Excalidraw に読み込めることを確認済み。

## なぜ既存の 4 エージェントはポータルで完結しないのか

`cc-layout` / `cc-projection` / `cc-verification` は **function tool** を持つ。
function tool は「エージェントが呼び出しを発行 → **クライアント側が実行して結果を返す**」
という往復が前提で、その実行役は手元の `cc_orchestrator` である。

ポータルの Playground には実行役がいないため、`function_call` が発行された時点で
停止する（実測済み）。

```
Playground で cc-layout に KG を渡した結果:
  → function_call 発生: compute_layout
  → 応答する相手が居ないためここで終了
```

対して `cc-cartographer-portal` は **Foundry 内で完結するツールだけ**で構成した:

| | ツール | 実行場所 |
|---|---|---|
| 資料収集 | Work IQ MCP（OneDrive / SharePoint / M365 Copilot） | Foundry |
| 座標計算・作図 | `code_interpreter`（決定的スクリプトを貼付実行） | Foundry |
| 出力 | `.excalidraw` ファイル添付 | Foundry |

## 2 つのモードの使い分け

| | ポータル版 `cc-cartographer-portal` | CLI 版 `cc_orchestrator.chat` |
|---|---|---|
| 起動 | ブラウザのみ | Mac のターミナル |
| 出力 | `.excalidraw` を**ダウンロード** | **VM-Excalidraw-MCP へライブ描画** + ミラー |
| エージェント数 | 1（自己完結） | 4（抽出/配置/描画/検証の分離） |
| 独立検証 | なし（同一エージェント内で完結） | あり（cc-verification が別モデルで突合） |
| 所要 | 約 2〜4 分 | 約 3.5 分 |
| 前提 | なし | Mac の venv + VM 稼働 + az ログイン |

描画規則（glyph 配色・ギャップの破線＋半透明・島グリッド配置）は両者で同一。
ポータル版のスクリプトは `src/cc_orchestrator/portal_agent.py` の `BUILDER_SCRIPT`、
CLI 版は `cc_core.layout` + `cc_core.excalidraw_file` にあり、**変更時は両方を合わせる**こと。

## ポータルから VM へライブ描画したい場合（未実施）

VM-Excalidraw-MCP は private IP のみ（<VM の private IP>）で、Foundry の実行基盤から到達できない。
ポータルから VM へ直接描かせるには、描画ツールを **Foundry が到達できる Remote MCP** として
公開し、プロジェクト接続に登録する必要がある。

現状の権限で確認した結果:

| 方式 | 状態 |
|---|---|
| Azure Container Apps（内部/外部イングレス） | ❌ `Microsoft.App` 未登録。登録権限なし（`AuthorizationFailed`） |
| Container Instances / Relay | ❌ 同上（未登録） |
| App Service（`Microsoft.Web`） | ⚠️ プロバイダは登録済み。リソース作成権限は未確認 |
| VM に Public IP 付与 | ❌ 引き継ぎメモ §4 のセキュリティ制約に抵触 |
| Foundry の VNet 注入（Standard Agent Setup） | ❌ 現アカウントは `publicNetworkAccess: Enabled` の基本構成 |

**IT 部門への依頼事項**（いずれか）:
- `Microsoft.App` プロバイダ登録 + `prj-qst-ai` への Contributor → `infra/main.bicep` で
  内部イングレスの ACA を構築し、そこに `cc_tools` を載せる
- または Foundry を Standard Agent Setup（VNet 注入）へ移行し、ACA/VM へ private 到達

それまでは「ポータル版でファイルを得る」「ライブ描画は CLI 版」の使い分けで運用できる。

## メンテナンス

```bash
# ポータル版エージェントの登録・更新（instructions を変えたら実行）
./.venv/bin/python -m cc_orchestrator.portal_agent

# CLI 版 4 エージェントの登録・更新
./.venv/bin/python -m cc_orchestrator.chat --setup-agents
```

どちらも既存エージェントがあれば**新しいバージョン**として登録される
（ポータルの「バージョン」列が増える）。
