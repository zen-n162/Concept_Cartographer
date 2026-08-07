# Concept Cartographer — PoC ブランチ

このブランチは **PoC (概念実証) の保存・改良ライン**です。完成版 (実運用版 R2 + Web アプリ) は **main ブランチ**にあります。

- 使い方・構成・実測結果: [poc/README.md](poc/README.md)
- タグ `poc-v1` = PoC 完成時点 (テスト 32 件 pass・E2E 検証済み)
- PoC の構成: Foundry 4 エージェント (extraction / layout / projection / verification) + layout_plan.json 中間契約 + Excalidraw 描画

main と並行して手元に置くには:

```bash
git worktree add ../ConceptCartographer-poc poc
```
