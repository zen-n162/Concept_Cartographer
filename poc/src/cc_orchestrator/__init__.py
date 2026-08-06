"""cc_orchestrator: Concept Cartographer のワークフロー実行層.

Foundry Agent Service (qst-cartographer-poc) 上の 4 エージェントを
workflows/cartographer_workflow.yaml の順序で実行し、描画ツールを
ローカル MCP または VM-Excalidraw-MCP (az vm run-command 中継) で実行する。
"""

FOUNDRY_PROJECT_ENDPOINT = (
    "https://rg-cartographer.services.ai.azure.com/api/projects/qst-cartographer-poc"
)
API_VERSION = "v1"
