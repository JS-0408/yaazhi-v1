#!/usr/bin/env bash
# backup.sh — Yaazhi Automated Backup Script
# Backs up PostgreSQL DB, ChromaDB vectors, knowledge vault, and config to a local
# timestamped archive. Can optionally sync to Supabase Storage or a remote SSH server.
#
# Usage:
#   bash scripts/backup.sh [--full | --incremental] [--remote]
#
# Schedule (add to crontab on your VPS):
#   0 3 * * * /app/scripts/backup.sh --incremental >> /var/log/yaazhi-backup.log 2>&1
#   0 0 * * 0 /app/scripts/backup.sh --full --remote >> /var/log/yaazhi-backup.log 2>&1

set -euo pipefail

# ─────────────────────────────────────────────────────────
# Configuration (inherit from environment or set defaults)
# ─────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"          # Days to retain local backups
POSTGRES_URL="${DATABASE_URL:-postgresql://yaazhi:yaazhi@localhost:5432/yaazhi}"
CHROMA_DATA="${CHROMA_PERSIST_DIR:-/app/data/chroma}"
KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-/app/knowledge}"
CONFIG_DIR="${CONFIG_DIR:-/app/config}"
REMOTE_SSH="${BACKUP_REMOTE_SSH:-}"         # e.g. user@backup-server.example.com:/backups
SUPABASE_URL="${SUPABASE_URL:-}"
SUPABASE_KEY="${SUPABASE_SERVICE_KEY:-}"
SUPABASE_BUCKET="${BACKUP_SUPABASE_BUCKET:-yaazhi-backups}"

MODE="incremental"
REMOTE=false

# ─────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --full)        MODE="full" ;;
    --incremental) MODE="incremental" ;;
    --remote)      REMOTE=true ;;
    --help)
      echo "Usage: backup.sh [--full | --incremental] [--remote]"
      exit 0 ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="yaazhi_${MODE}_${TIMESTAMP}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] [backup.sh]"

log()  { echo "${LOG_PREFIX} $*"; }
warn() { echo "${LOG_PREFIX} ⚠️  WARNING: $*" >&2; }
fail() { echo "${LOG_PREFIX} ❌ ERROR: $*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

# ─────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────
log "=== Yaazhi Backup — ${MODE} mode ==="
require_cmd pg_dump
require_cmd tar
mkdir -p "${BACKUP_PATH}"

# ─────────────────────────────────────────────────────────
# 1. PostgreSQL database
# ─────────────────────────────────────────────────────────
log "📦 Dumping PostgreSQL database…"
if pg_dump "${POSTGRES_URL}" \
     --format=custom \
     --compress=9 \
     --file="${BACKUP_PATH}/postgres.dump" 2>/dev/null; then
  log "   ✅ Postgres dump: $(du -sh "${BACKUP_PATH}/postgres.dump" | cut -f1)"
else
  warn "Postgres dump failed — database may be offline. Skipping."
fi

# ─────────────────────────────────────────────────────────
# 2. ChromaDB vector store
# ─────────────────────────────────────────────────────────
log "🗄  Archiving ChromaDB vector store…"
if [[ -d "${CHROMA_DATA}" ]]; then
  tar -czf "${BACKUP_PATH}/chroma.tar.gz" -C "$(dirname "${CHROMA_DATA}")" \
      "$(basename "${CHROMA_DATA}")"
  log "   ✅ ChromaDB: $(du -sh "${BACKUP_PATH}/chroma.tar.gz" | cut -f1)"
else
  warn "ChromaDB directory not found: ${CHROMA_DATA}. Skipping."
fi

# ─────────────────────────────────────────────────────────
# 3. Knowledge vault (PDFs, notes, papers)
# ─────────────────────────────────────────────────────────
log "📚 Archiving knowledge vault…"
if [[ -d "${KNOWLEDGE_DIR}" ]]; then
  if [[ "${MODE}" == "full" ]]; then
    tar -czf "${BACKUP_PATH}/knowledge.tar.gz" -C "$(dirname "${KNOWLEDGE_DIR}")" \
        "$(basename "${KNOWLEDGE_DIR}")"
    log "   ✅ Knowledge vault (full): $(du -sh "${BACKUP_PATH}/knowledge.tar.gz" | cut -f1)"
  else
    # Incremental: only files modified in the last 24 hours
    find "${KNOWLEDGE_DIR}" -mtime -1 -type f | tar -czf "${BACKUP_PATH}/knowledge_incremental.tar.gz" \
        --files-from=-
    COUNT=$(tar -tzf "${BACKUP_PATH}/knowledge_incremental.tar.gz" 2>/dev/null | wc -l || echo 0)
    log "   ✅ Knowledge vault (incremental, ${COUNT} files)"
  fi
else
  warn "Knowledge directory not found: ${KNOWLEDGE_DIR}. Skipping."
fi

# ─────────────────────────────────────────────────────────
# 4. Config (exclude .env to protect secrets)
# ─────────────────────────────────────────────────────────
log "⚙️  Archiving config (secrets excluded)…"
if [[ -d "${CONFIG_DIR}" ]]; then
  tar -czf "${BACKUP_PATH}/config.tar.gz" -C "$(dirname "${CONFIG_DIR}")" \
      "$(basename "${CONFIG_DIR}")" \
      --exclude="*.env" --exclude=".env"
  log "   ✅ Config archived (secrets excluded)"
fi

# ─────────────────────────────────────────────────────────
# 5. n8n workflows
# ─────────────────────────────────────────────────────────
log "🔄 Archiving n8n workflows…"
WORKFLOWS_DIR="${WORKFLOWS_DIR:-/app/workflows}"
if [[ -d "${WORKFLOWS_DIR}" ]]; then
  tar -czf "${BACKUP_PATH}/workflows.tar.gz" -C "$(dirname "${WORKFLOWS_DIR}")" \
      "$(basename "${WORKFLOWS_DIR}")"
  log "   ✅ n8n workflows archived"
fi

# ─────────────────────────────────────────────────────────
# 6. Bundle everything into a single archive
# ─────────────────────────────────────────────────────────
log "🗜  Creating final bundle: ${BACKUP_NAME}.tar.gz"
BUNDLE="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
tar -czf "${BUNDLE}" -C "${BACKUP_DIR}" "${BACKUP_NAME}/"
rm -rf "${BACKUP_PATH}"          # Clean up staging directory
BUNDLE_SIZE=$(du -sh "${BUNDLE}" | cut -f1)
log "   ✅ Bundle ready: ${BUNDLE_SIZE}"

# ─────────────────────────────────────────────────────────
# 7. Remote sync (optional)
# ─────────────────────────────────────────────────────────
if [[ "${REMOTE}" == "true" ]]; then
  # Option A: SSH remote
  if [[ -n "${REMOTE_SSH}" ]]; then
    log "🚀 Syncing to SSH remote: ${REMOTE_SSH}"
    if rsync -az --progress "${BUNDLE}" "${REMOTE_SSH}/"; then
      log "   ✅ Remote SSH sync complete"
    else
      warn "Remote SSH sync failed. Backup is still stored locally."
    fi
  fi

  # Option B: Supabase Storage
  if [[ -n "${SUPABASE_URL}" && -n "${SUPABASE_KEY}" ]]; then
    log "☁️  Uploading to Supabase Storage bucket '${SUPABASE_BUCKET}'…"
    UPLOAD_URL="${SUPABASE_URL}/storage/v1/object/${SUPABASE_BUCKET}/${BACKUP_NAME}.tar.gz"
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "${UPLOAD_URL}" \
      -H "Authorization: Bearer ${SUPABASE_KEY}" \
      -H "Content-Type: application/gzip" \
      --data-binary "@${BUNDLE}" || echo "000")

    if [[ "${HTTP_STATUS}" =~ ^2 ]]; then
      log "   ✅ Supabase upload complete (HTTP ${HTTP_STATUS})"
    else
      warn "Supabase upload returned HTTP ${HTTP_STATUS}. Check storage quota and key."
    fi
  fi
fi

# ─────────────────────────────────────────────────────────
# 8. Prune old local backups
# ─────────────────────────────────────────────────────────
log "🧹 Pruning backups older than ${KEEP_DAYS} days…"
find "${BACKUP_DIR}" -name "yaazhi_*.tar.gz" -mtime "+${KEEP_DAYS}" -delete
REMAINING=$(find "${BACKUP_DIR}" -name "yaazhi_*.tar.gz" | wc -l)
log "   ✅ ${REMAINING} backup(s) retained"

# ─────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────
log "=== Backup complete: ${BUNDLE} ==="
