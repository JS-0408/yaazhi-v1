#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Yaazhi VPS Setup Script — Ubuntu 22.04 LTS
# Run this ONCE on a fresh DigitalOcean Droplet:
#   bash scripts/setup.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ─── Colors ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
NC='\033[0m' # No Color

# ─── Helpers ───────────────────────────────────────────────────
log_step() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${PURPLE}▶ $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

log_ok() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warn() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_err() {
    echo -e "${RED}❌ $1${NC}"
}

command_exists() {
    command -v "$1" &>/dev/null
}

SERVER_IP=$(curl -s https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
YAAZHI_DIR="${HOME}/yaazhi"
VENV_DIR="${YAAZHI_DIR}/.venv"
PYTHON_BIN="python3.11"

echo -e "\n${PURPLE}"
echo "╔══════════════════════════════════════════════╗"
echo "║          YAAZHI VPS SETUP STARTING           ║"
echo "║     Personal AI System — VIT-AP ECE          ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ─── Step 1: System Update ─────────────────────────────────────
log_step "Step 1: Updating system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    build-essential \
    curl \
    wget \
    git \
    software-properties-common \
    ca-certificates \
    gnupg \
    lsb-release \
    unzip \
    jq \
    htop \
    tmux \
    ffmpeg \
    libsndfile1 \
    portaudio19-dev \
    postgresql-client \
    redis-tools
log_ok "System packages updated"

# ─── Step 2: Python 3.11 ───────────────────────────────────────
log_step "Step 2: Installing Python 3.11"
if ! command_exists python3.11; then
    add-apt-repository ppa:deadsnakes/ppa -y
    apt-get update -qq
    apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
else
    log_warn "Python 3.11 already installed"
fi
python3.11 --version
log_ok "Python 3.11 ready"

# ─── Step 3: Docker ────────────────────────────────────────────
log_step "Step 3: Installing Docker + Compose Plugin"
if ! command_exists docker; then
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm /tmp/get-docker.sh
    usermod -aG docker "${USER:-root}"
    systemctl enable docker
    systemctl start docker
    log_ok "Docker installed"
else
    log_warn "Docker already installed: $(docker --version)"
fi

if ! docker compose version &>/dev/null; then
    apt-get install -y docker-compose-plugin
fi
docker compose version
log_ok "Docker Compose ready"

# ─── Step 4: Ollama ────────────────────────────────────────────
log_step "Step 4: Installing Ollama and pulling models"
if ! command_exists ollama; then
    curl -fsSL https://ollama.ai/install.sh | sh
    systemctl enable ollama 2>/dev/null || true
    systemctl start ollama 2>/dev/null || true
    sleep 5
    log_ok "Ollama installed"
else
    log_warn "Ollama already installed"
fi

# Pull required models
log_step "Pulling llama3.2 (this may take 5-10 minutes on first run)"
ollama pull llama3.2 && log_ok "llama3.2 ready" || log_err "llama3.2 pull failed — retry manually: ollama pull llama3.2"

log_step "Pulling nomic-embed-text (embedding model)"
ollama pull nomic-embed-text && log_ok "nomic-embed-text ready" || log_err "nomic-embed-text pull failed"

# ─── Step 5: Cloudflared ───────────────────────────────────────
log_step "Step 5: Installing Cloudflare Tunnel (cloudflared)"
if ! command_exists cloudflared; then
    wget -q "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb" -O /tmp/cloudflared.deb
    dpkg -i /tmp/cloudflared.deb
    rm /tmp/cloudflared.deb
    log_ok "cloudflared installed: $(cloudflared --version)"
else
    log_warn "cloudflared already installed"
fi

# ─── Step 6: Clone Repository ──────────────────────────────────
log_step "Step 6: Cloning Yaazhi repository"
if [ ! -d "${YAAZHI_DIR}/.git" ]; then
    git clone https://github.com/santhosh-vitap/yaazhi.git "${YAAZHI_DIR}" || {
        log_warn "Git clone failed — assuming code already exists at ${YAAZHI_DIR}"
    }
else
    log_warn "Repository already exists at ${YAAZHI_DIR}"
    cd "${YAAZHI_DIR}" && git pull origin main 2>/dev/null || true
fi
log_ok "Repository ready at ${YAAZHI_DIR}"

# ─── Step 7: Python Virtual Environment ────────────────────────
log_step "Step 7: Creating Python virtual environment and installing dependencies"
cd "${YAAZHI_DIR}"

if [ ! -d "${VENV_DIR}" ]; then
    ${PYTHON_BIN} -m venv "${VENV_DIR}"
    log_ok "Virtual environment created at ${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"
pip install --upgrade pip wheel setuptools -q
pip install -r requirements.txt -q && log_ok "Python dependencies installed" || {
    log_err "pip install failed — check requirements.txt and your internet connection"
    exit 1
}

# ─── Step 8: Playwright Chromium ───────────────────────────────
log_step "Step 8: Installing Playwright + Chromium browser"
playwright install chromium --with-deps && log_ok "Playwright Chromium ready" || {
    log_warn "Playwright install had issues — browser features may not work"
}

# ─── Step 9: Environment File ──────────────────────────────────
log_step "Step 9: Setting up environment configuration"
if [ ! -f "${YAAZHI_DIR}/config/.env" ]; then
    cp "${YAAZHI_DIR}/config/.env.example" "${YAAZHI_DIR}/config/.env"
    log_warn "config/.env created from template — YOU MUST FILL IN YOUR API KEYS before starting!"
else
    log_warn "config/.env already exists — not overwriting"
fi

# Create required directories
mkdir -p "${YAAZHI_DIR}/logs"
mkdir -p "${YAAZHI_DIR}/knowledge/btech_notes"
mkdir -p "${YAAZHI_DIR}/knowledge/ieee_papers"
mkdir -p "${YAAZHI_DIR}/knowledge/projects"
mkdir -p "/tmp/yaazhi_sandbox"
mkdir -p "/tmp/yaazhi_screenshots"
mkdir -p "/backups"
log_ok "Directories created"

# ─── Step 10: Docker Services ──────────────────────────────────
log_step "Step 10: Starting Docker services (ChromaDB, PostgreSQL, Redis, n8n, Grafana)"
cd "${YAAZHI_DIR}"
docker compose -f infra/docker-compose.yml up -d && log_ok "Docker services starting" || {
    log_err "Docker Compose failed — check infra/docker-compose.yml and config/.env"
}

echo -e "\n${YELLOW}Waiting 15 seconds for services to initialise...${NC}"
sleep 15

# ─── Step 11: Database Migration ───────────────────────────────
log_step "Step 11: Running database migrations"
source "${VENV_DIR}/bin/activate"
cd "${YAAZHI_DIR}"
${PYTHON_BIN} scripts/migrate.py && log_ok "Database migrations complete" || {
    log_warn "Migration had issues — check PostgreSQL is running and POSTGRES_URL is correct"
}

# ─── Step 12: Health Check ─────────────────────────────────────
log_step "Step 12: Running system health check"
${PYTHON_BIN} scripts/health_check.py || log_warn "Some services are not yet healthy — check logs"

# ─── Setup Backup Cron ─────────────────────────────────────────
log_step "Setting up automatic daily backup cron job"
CRON_JOB="0 2 * * * /bin/bash ${YAAZHI_DIR}/scripts/backup.sh >> /var/log/yaazhi_backup.log 2>&1"
(crontab -l 2>/dev/null | grep -v "backup.sh"; echo "${CRON_JOB}") | crontab -
log_ok "Backup cron job added (runs at 2AM daily)"

# ─── Final Banner ──────────────────────────────────────────────
echo ""
echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║                                                      ║"
echo "║          ✅  YAAZHI IS READY!  ✅                    ║"
echo "║                                                      ║"
echo "║  Your personal AI system is running on:              ║"
echo -e "║  VPS IP: ${SERVER_IP}                                ║"
echo "║                                                      ║"
echo "║  NEXT STEPS:                                         ║"
echo "║  1. Fill in config/.env with your API keys           ║"
echo "║  2. Restart: docker compose -f infra/docker-compose.yml restart"
echo "║  3. Visit: http://${SERVER_IP}:8501 (Dashboard)       ║"
echo "║  4. API:   http://${SERVER_IP}:8000/health            ║"
echo "║  5. n8n:   http://${SERVER_IP}:5678                   ║"
echo "║                                                      ║"
echo "║  Add your notes: knowledge/btech_notes/              ║"
echo "║  Then run: python scripts/ingest_docs.py             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
