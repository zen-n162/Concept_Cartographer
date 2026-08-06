#!/usr/bin/env bash
# cc_core + vmexec を VM-Excalidraw-MCP へ配布する (az vm run-command 経由 / 経路不要)
#   ./scripts/vm_deploy_app.sh
set -euo pipefail
cd "$(dirname "$0")/.."

RG=prj-qst-ai
VM=VM-Excalidraw-MCP

echo "== packaging (src/cc_core, schemas, vmexec) =="
TAR_B64=$(tar czf - src/cc_core schemas scripts/vmexec.py pyproject.toml | base64 | tr -d '\n')
echo "payload: $(( ${#TAR_B64} / 1024 )) KB"

echo "== deploying via run-command =="
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript --scripts '
set -e
mkdir -p /opt/cartographer/app
cd /opt/cartographer/app
echo "'"$TAR_B64"'" | base64 -d | tar xzf -
mv -f scripts/vmexec.py vmexec.py 2>/dev/null || true
if [ ! -d /opt/cartographer/venv ]; then
  apt-get install -y -q python3-venv >/dev/null 2>&1 || true
  python3 -m venv /opt/cartographer/venv
fi
/opt/cartographer/venv/bin/pip install -q "mcp>=1.9,<2" "jsonschema>=4.21" "anyio>=4.0"
chown -R azureuser:azureuser /opt/cartographer/app /opt/cartographer/venv
sudo -u azureuser /opt/cartographer/venv/bin/python /opt/cartographer/app/vmexec.py status
' --query "value[0].message" -o tsv | grep -v '^\[' | grep -v '^$' || true
echo "== deploy done =="
