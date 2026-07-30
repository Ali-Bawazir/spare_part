# MMS — Runbook

Operational playbook for MMS v1.0.0 production deployment.

## Health & liveness

- `GET /health/` returns `200 {"status":"ok","db":"ok"}` when the app and DB are reachable.
- `503 {"status":"degraded","db":"error"}` if the DB is unreachable.
- Container healthcheck (`docker-compose.yml`): `curl -fsS http://localhost:8000/health/` every 30s.

External uptime monitor: configure your monitoring tool (Pingdom, UptimeRobot, etc.) to hit `https://<your-fqdn>/health/` every 60s. Alert on non-200 responses.

## Logs

- Application logs: stderr (captured by Docker / CranL log driver).
- Gunicorn access + error logs: stdout/stderr.
- Configure Docker logging driver in compose to cap retention (default: unlimited, fills disk).

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

## Common operations

### Promote a user to super_admin

```bash
docker compose exec web python manage.py create_mms_user <username> --role super_admin
```

Or in Django admin: `/admin/accounts/user/<id>/change/` and edit the role field.

### Reset a user's password

```bash
docker compose exec web python manage.py changepassword <username>
```

### Account locked out by brute-force protection

django-axes locks the account for 15 minutes after 5 failed logins
(tracked per username + IP). Unlocks automatically.

Manual unlock:

```bash
docker compose exec web python manage.py axes_reset
docker compose exec web python manage.py axes_reset_username <username>
```

Or via `/admin/axes/accessattempt/` (delete the offending AccessAttempt rows).

### Password Reset

Users cannot reset or change forgotten passwords themselves.

If a user forgets their password:

1. User contacts the Super Admin.
2. Super Admin opens Users.
3. Super Admin edits the user.
4. Super Admin sets a new password.
5. User signs in with the new password.

### Idle session timeout (4 hours)

Authenticated sessions expire after 4 hours of inactivity. The
session is renewed on any 2xx/3xx response, but not on failed requests
or static asset hits. After expiry, the user is logged out and
redirected to `/accounts/login/?expired=1` with a "Your session has
expired. Please sign in again." banner.

To change the timeout without redeploying code, set
`MMS_SESSION_TIMEOUT_SECONDS=<seconds>` in the CranL app config and
restart the web service. Default: 14400 (4 hours).

### Inspect a work order

```bash
docker compose exec web python manage.py shell -c "
from maintenance.models import WorkOrder
wo = WorkOrder.objects.get(number=<number>)
print('Lifecycle:', wo.lifecycle_status, 'Operational:', wo.operational_status)
print('Open blockers:', list(wo.blockers.filter(status='OPEN').values_list('kind', flat=True)))
print('Active reservations:', wo.reservations.filter(status='active').count())
"
```

### Reconciliation commands

Run periodically (suggest: weekly cron) to detect data drift:

```bash
docker compose exec web python manage.py repair_part_lines --dry-run
docker compose exec web python manage.py repair_paused_blockers --dry-run
docker compose exec web python manage.py reconcile_orphan_vendor_blockers --dry-run
docker compose exec web python manage.py reconcile_legacy_reservations --dry-run
docker compose exec web python manage.py ledger_integrity_check
```

If a `--dry-run` reports issues, re-run without `--dry-run` to apply the fix. Review the changes via git/audit log afterwards.

### Migration

Migrations run automatically on container start (entrypoint.sh). To run manually:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py makemigrations --check --dry-run
```

## Restart services

```bash
docker compose restart web
docker compose restart db
```

For Gunicorn worker recycling without downtime, configure `--max-requests` in compose (default 1000).

## Rollback

If a deploy goes wrong:

1. **Identify the bad image**: CranL stores image tags per deploy. Find the last known-good tag.
2. **Roll back via CranL**: redeploy the previous image tag.
3. **Roll back DB migrations** (only if migrations are irreversible):
   ```bash
   docker compose exec web python manage.py migrate <app> <previous_migration_name>
   ```
4. **Restore DB from backup** (only if data is corrupted):
   ```bash
   # See BACKUP.md
   ```

## Emergency contacts

Document on-call rotation in your team's incident response system. Include:
- SRE / platform team
- Database admin
- Application owner (factory maintenance lead)

## Scheduled jobs (Phase 2)

The following commands are deferred to Phase 2 (no Celery in v1.0.0). Run them via external cron until Celery is added:

```bash
# Daily — PM notifications
docker compose exec web python manage.py sync_pm_notifications
# Daily — PM overdue alerts
docker compose exec web python manage.py pm_overdue_alerts
# Daily — morning PM summary
docker compose exec web python manage.py pm_daily_routine
# Daily — general scheduled notifications
docker compose exec web python manage.py send_scheduled_notifications
```

Suggested crontab:
```
15 7 * * *   cd /opt/mms && docker compose exec -T web python manage.py sync_pm_notifications
30 7 * * *   cd /opt/mms && docker compose exec -T web python manage.py pm_overdue_alerts
0 8 * * *    cd /opt/mms && docker compose exec -T web python manage.py pm_daily_routine
0 9 * * *    cd /opt/mms && docker compose exec -T web python manage.py send_scheduled_notifications
```