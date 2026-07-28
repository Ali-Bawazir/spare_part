#!/bin/bash
# MMS DB backup script.
#
# Usage:
#   ./scripts/backup_db.sh [/path/to/backup/dir]
#
# Default backup dir: /opt/mms/backups/db (override with first arg).
#
# Behaviour:
#   - Dumps the running Postgres container to a timestamped .sql.gz file
#   - Deletes local backups older than 14 days
#   - Optional off-host shipping via AWS S3 (if AWS CLI + S3_BUCKET env vars set)
#
# Cron example:
#   0 2 * * *  cd /opt/mms && /opt/mms/scripts/backup_db.sh >> /var/log/mms-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${1:-/opt/mms/backups/db}"
TS=$(date +%F_%H%M%S)
FILE="${BACKUP_DIR}/db_${TS}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting DB backup → $FILE"
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$FILE"
echo "[$(date -Iseconds)] Backup written: $(du -h "$FILE" | cut -f1)"

# Retention: 14 days
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +14 -delete
echo "[$(date -Iseconds)] Local retention applied (14 days)"

# Optional off-host shipping
if [[ -n "${S3_BUCKET:-}" ]] && command -v aws >/dev/null 2>&1; then
    S3_KEY="s3://${S3_BUCKET}/mms/db/$(basename "$FILE")"
    aws s3 cp "$FILE" "$S3_KEY" --storage-class STANDARD_IA
    echo "[$(date -Iseconds)] Shipped to ${S3_KEY}"
fi

echo "[$(date -Iseconds)] Done."