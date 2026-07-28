# MMS — Backup & Restore

Critical for disaster recovery. Test restores monthly.

## What to back up

1. **PostgreSQL database** — all operational data (work orders, inventory, procurement, notifications, audit).
2. **Media volume** (`mms_media`) — uploaded photos, attachments, QR codes, vendor invoices.

## PostgreSQL backup

### Manual backup

```bash
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" \
  | gzip > /opt/mms/backups/db_$(date +%F_%H%M%S).sql.gz
```

### Automated daily backup (cron)

```bash
# /etc/cron.d/mms-backup
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Daily DB dump, 14-day retention
0 2 * * *   mms  cd /opt/mms && /opt/mms/scripts/backup_db.sh >> /var/log/mms-backup.log 2>&1
```

`/opt/mms/scripts/backup_db.sh`:
```bash
#!/bin/bash
set -euo pipefail
BACKUP_DIR=/opt/mms/backups/db
mkdir -p "$BACKUP_DIR"
TS=$(date +%F_%H%M%S)
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" \
  | gzip > "$BACKUP_DIR/db_${TS}.sql.gz"
# Retention: 14 days
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +14 -delete
```

### Off-host shipping

After local backup, ship to S3 / NFS / managed backup:

```bash
# Add to backup_db.sh after the local dump:
aws s3 cp "$BACKUP_DIR/db_${TS}.sql.gz" \
  s3://your-bucket/mms/db/db_${TS}.sql.gz
```

Or use your existing backup tool (Bacula, Borg, restic, etc.).

## Media backup

```bash
docker run --rm \
  -v mms_mms_media:/media:ro \
  -v /opt/mms/backups:/backup \
  alpine \
  tar czf /backup/media_$(date +%F_%H%M%S).tgz -C /media .
```

Schedule daily, same retention as DB.

## Restore

### Restoring PostgreSQL from a backup file

```bash
# 1. Stop the web container to prevent new writes
docker compose stop web

# 2. Drop + recreate the DB (or restore into a fresh DB)
docker compose exec -T db dropdb -U "$DB_USER" --if-exists "$DB_NAME"
docker compose exec -T db createdb -U "$DB_USER" "$DB_NAME"

# 3. Restore from the dump
gunzip -c /opt/mms/backups/db_2026-07-28_020000.sql.gz \
  | docker compose exec -T db psql -U "$DB_USER" "$DB_NAME"

# 4. Restart web
docker compose start web
docker compose exec web python manage.py check --deploy
```

### Restoring media from a backup file

```bash
# Stop web, replace media volume contents, restart
docker compose stop web
docker run --rm \
  -v mms_mms_media:/media \
  -v /opt/mms/backups:/backup:ro \
  alpine \
  sh -c "rm -rf /media/* && tar xzf /backup/media_2026-07-28_020000.tgz -C /media"
docker compose start web
```

## Verify backup integrity

Run a restore drill monthly into a disposable database and confirm:

```bash
docker compose exec -T db createdb -U "$DB_USER" mms_drill
gunzip -c /opt/mms/backups/db_2026-07-28_020000.sql.gz \
  | docker compose exec -T db psql -U "$DB_USER" mms_drill
docker compose exec -T db psql -U "$DB_USER" mms_drill -c "\dt" | head -30
docker compose exec -T db psql -U "$DB_USER" mms_drill \
  -c "SELECT count(*) FROM maintenance_workorder;"
docker compose exec -T db dropdb -U "$DB_USER" mms_drill
```

## Backup checklist

- [ ] Daily DB cron installed and verified
- [ ] Daily media cron installed and verified
- [ ] Off-host shipping configured
- [ ] 14-day retention (or per your policy)
- [ ] Monthly restore drill performed
- [ ] Backup files encrypted at rest (consider `gpg --encrypt` or storage-level encryption)
- [ ] Backup access restricted to ops team only