#!/usr/bin/env bash
# ─── Yaazhi Multi-VPS Bootstrap Script for instabox.sh / Ubuntu / Debian ─────
# Usage:
#   ROLE=data   TAILSCALE_AUTHKEY=tskey-auth-... bash infra/deploy_instabox.sh
#   ROLE=ollama TAILSCALE_AUTHKEY=tskey-auth-... bash infra/deploy_instabox.sh
#   ROLE=api    NODE_ID=instabox-api-1 TAILSCALE_AUTHKEY=tskey-auth-... bash infra/deploy_instabox.sh
#   ROLE=all    bash infra/deploy_instabox.sh

set -euo pipefail

ROLE="${ROLE:-api}"
NODE_ID="${NODE_ID:-instabox-$(hostname)}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"

echo "============================================================"
echo " Yaazhi Multi-VPS Bootstrap — Role: ${ROLE} | Node: ${NODE_ID}"
echo "============================================================"

# 1. Ensure 4 GB swap file so 2-4 GB RAM instabox.sh nodes never OOM-kill
if ! swapon --show | grep -q '/swapfile'; then
  echo "[1/5] Configuring ${SWAP_SIZE_GB}G swapfile..."
  sudo fallocate -l "${SWAP_SIZE_GB}G" /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_SIZE_GB * 1024))
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile || true
fi

# 2. Install Docker & Docker Compose v2 if missing
if ! command -v docker >/dev/null 2>&1; then
  echo "[2/5] Installing Docker..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "${USER:-root}" || true
else
  echo "[2/5] Docker already installed."
fi

# 3. Optional Tailscale mesh setup across separate instabox.sh accounts
if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
  echo "[3/5] Connecting node to Tailscale mesh..."
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  sudo tailscale up --authkey="${TAILSCALE_AUTHKEY}" --hostname="${NODE_ID}"
  TS_IP="$(tailscale ip -4 | head -n1)"
  echo "      Tailscale IP: ${TS_IP}"
  export DB_BIND_IP="${TS_IP}"
  export OLLAMA_BIND_IP="${TS_IP}"
else
  echo "[3/5] TAILSCALE_AUTHKEY not set; skipping Tailscale setup."
fi

# 4. Ensure config/.env exists
if [[ ! -f config/.env ]]; then
  echo "[4/5] Creating config/.env from config/.env.example..."
  cp config/.env.example config/.env
fi

# 5. Launch the requested profile
echo "[5/5] Starting Docker Compose profile '${ROLE}'..."
export YAAZHI_NODE_ID="${NODE_ID}"
docker compose -f infra/docker-compose.instabox.yml --profile "${ROLE}" up -d --build

if [[ "${ROLE}" == "ollama" || "${ROLE}" == "all" ]]; then
  echo "Pulling nomic-embed-text embedding model into yaazhi-ollama..."
  docker exec yaazhi-ollama ollama pull nomic-embed-text || true
fi

echo "============================================================"
echo " Deployment complete for role '${ROLE}' on node '${NODE_ID}'!"
echo "============================================================"

