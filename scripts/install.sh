#!/usr/bin/env bash
# install.sh — bring a jupyter-rmcp instance up on a (possibly fresh) machine.
# Idempotent: safe to re-run. Handles the Docker side only — exposing the server
# beyond this host is your reverse proxy's / tunnel's job (see docs/AUTH.md).
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root

echo "▶ prereqs"
command -v docker >/dev/null || { echo "  ✗ docker not installed"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "  ✗ 'docker compose' v2 not available"; exit 1; }
echo "  ✓ docker $(docker --version | awk '{print $3}' | tr -d ,)"

if [ ! -f .env ]; then
  echo "▶ .env not found — generating (fresh JUPYTER_TOKEN + MCP_BEARER)"
  gen() { python3 -c 'import secrets;print(secrets.token_hex(32))'; }
  cat > .env <<EOF
JUPYTER_TOKEN=$(gen)
# Bearer token your MCP client must send. Keep it unless something in front of the
# server authenticates callers for you (docs/AUTH.md explains when to blank it).
MCP_BEARER=$(gen)
MCP_HOST_PORT=7130
JUPYTER_LAB_HOST_PORT=7131
KERNEL_IDLE_TIMEOUT_SEC=3600
KERNEL_MAX_AGE_SEC=28800
EXEC_TIMEOUT_SEC=120
MAX_KERNELS=8
# Uncomment for a Colab-only instance (no local code execution). Needs GCLOUD_ADC.
# COLAB_ONLY=1
# GCLOUD_ADC=/absolute/path/to/application_default_credentials.json
EOF
  chmod 600 .env
  echo "  ✓ wrote .env"
else
  echo "▶ .env exists — keeping it"
fi

PORT="$(grep '^MCP_HOST_PORT=' .env | cut -d= -f2)"

echo "▶ build + up"
docker compose up -d --build

echo "▶ waiting for health"
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "  ✓ healthy: $(curl -s "http://127.0.0.1:${PORT}/health")"
    break
  fi
  sleep 2
done

cat <<NEXT

▶ up on http://127.0.0.1:${PORT}/mcp (loopback only).
  1) Smoke test:        python scripts/smoke_test.py
  2) Connect Claude Code:
       claude mcp add --transport http jupyter-rmcp http://127.0.0.1:${PORT}/mcp \\
         --header "Authorization: Bearer \$(grep '^MCP_BEARER=' .env | cut -d= -f2)"
  3) JupyterLab (watch/edit the same notebooks): http://127.0.0.1:$(grep '^JUPYTER_LAB_HOST_PORT=' .env | cut -d= -f2 || echo 7131)
  4) Colab GPU offload + Kaggle setup:           docs/GUIDE.md
  5) Reaching it from the Claude app / another machine: docs/AUTH.md
NEXT
