#!/bin/bash
# MMS media backup script.
#
# Usage:
#   ./scripts/backup_media.sh [/path/to/backup/dir]
#
# Default backup dir: /opt/mms/backups/media (override with first arg).
#
# Behaviour:
#   - tars the mms_media Docker volume to a timestamped .tgz file
#   - Deletes local backups older than 14 days
#   - Optional off-host shipping via AWS S3 (if AWS CLI + S3_BUCKET env vars set)

set -euo pipefail

BACKUP_DIR="${1:-/opt/mms/backups/media}"
TS=$(date +%F_%H%M%S)
FILE="${BACKUP_DIR}/media_${TS}.tgz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting media backup → $FILE"
docker run --rm \
    -v mms_mms_media:/media:ro \
    -v "${BACKUP_DIR}:/backup" \
    alpine \
    tar czf "/backup/$(basename "$FILE")" -C /media .
echo "[$(date -Iseconds)] Backup written: $(du -h "$FILE" | cut -f1)"

# Retention: 14 days
find "$BACKUP_DIR" -name "media_*.tgz" -mtime +14 -delete
echo "[$(date -Iseconds)] Local retention applied (14 days)"

# Optional off-host shipping
if [[ -n "${S3_BUCKET:-}" ]] && command -v aws >/dev/null 2>&1; then
    S3_KEY="s3://${S3_BUCKET}/mms/media/$(basename "$FILE")"
    aws s3 cp "$FILE" "$S3_KEY" --storage-class STANDARD_IA
    echo "[$(date -Iseconds)] Shipped to ${S3_KEY}"
fi

echo "[$(date -Iseconds)] Done."