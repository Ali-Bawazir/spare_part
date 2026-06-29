# Preventive Maintenance Workflow — Daily Scenario

This is the canonical end-to-end test of the new PM architecture. Follow it
exactly to validate the system behaves correctly.

## Roles

- **Manager** (e.g. `manager` / `test1234`) — plans maintenance, reviews submissions
- **Technician** (e.g. `technician` / `test1234`) — executes today's maintenance
- **Supervisor** — deferred to Phase 2

## The 10-phase workflow

### Phase 1 — Create Maintenance Template (one-time setup)

Manager → Templates → + New Template.

Example: "Hydraulic Inspection", 30 min, 4 checklist items, 1 photo required.

### Phase 2 — Create Maintenance Plan (per machine)

Manager → Maintenance Plans → + New Plan.

Example: Press-01, "Hydraulic Inspection", Monthly, due 08:00, assigned Ahmed.

### Phase 3 — Daily automatic generation (07:00 cron)

`python manage.py pm_daily_routine` runs at 07:00 server time:

- For each active PMSchedule where `next_due_at` is today, creates a `PMExecution` row in SUBMITTED status
- Sends morning summary notifications to all technicians with work today
- Sends manager morning summary: "12 scheduled, 2 overdue, 1 unassigned"
- Idempotent: re-runs same day are no-ops

### Phase 4 — Manager Today's Schedule

Manager → Today's Schedule. Sees today's occurrences grouped by time slot.

Per row actions:
- **Assign** (Unassigned rows only) — modal with tech dropdown
- **Reassign** (already-assigned) — modal
- **Open** — jumps to Plan Details

### Phase 5 — Technician opens My Maintenance

Technician → Preventive Maintenance → My Maintenance.

Single page with 4 sections:
- Today (3)
- Upcoming (9) — calendar-style: Tomorrow / Tue 1 Jul / Wed 2 Jul
- Completed (2) — includes Waiting for approval rows
- Yellow banner if any item was returned

### Phase 6 — Start Maintenance

Technician clicks Start on Hydraulic Inspection → lands on execution page with:
- Elapsed / Expected timer (top-right, color shifts: green < 80%, amber 80-100%, red > 100%)
- Checklist (4 items)
- Notes (textarea)
- Photo upload (1 required for this template)

### Phase 7 — Complete Maintenance

Technician ticks checkboxes + adds notes + uploads photo, clicks "Complete Maintenance".

Server-side gating:
- ≥1 checklist item checked OR notes non-empty (else: "Please check at least one item or add notes before completing.")
- Photos ≥ `requires_photo_min_count` (else: "At least 1 photo required.")

On success:
- WO → PENDING_REVIEW
- Card moves to Completed section as "Waiting for approval"
- Manager receives instant notification

### Phase 8 — Manager Reviews

Manager → Reviews (badge in sidebar).

Queue sorted oldest first (FIFO). Per row:
- **Approve** — instantly
- **Return** — opens reason modal (required). Technician gets instant notification with reason.

### Phase 9 — Manager Maintenance Plans (overview)

Manager → Maintenance Plans. Machine-oriented table:

| Machine | Maintenance | Due | Assigned | Status |
|---|---|---|---|---|
| Press-01 | Hydraulic Inspection | Tomorrow | Ahmed | 🟢 Scheduled |
| Press-02 | Lubrication | Today | Ali | 🟡 In Progress |

Filter pills: Active | Paused | Archived | All.

### Phase 10 — Plan Details (single source of truth per plan)

Manager clicks "Open" on any plan row → Plan Details page.

Sections:
- **Plan Information** — machine, maintenance, frequency, due time, priority, active, created
- **Quick Actions** — Edit, Assign/Reassign, Pause/Resume, Run Now, Archive, View Machine
- **Upcoming Schedule** — next due + last completed (with technician + duration)
- **Recent Executions** — last 5 with status and actual vs expected
- **View all history** link

## Notifications (9 events total)

### Technician (4 events)
1. Morning summary (07:00 daily)
2. New assignment (instant)
3. Returned (instant)
4. Overdue (14:00 daily, if not done)

### Manager (5 events)
1. Morning summary (07:00 daily)
2. Waiting Review submitted (instant)
3. Overdue (14:00 daily)
4. Unassigned (09:00 daily)
5. Plan paused (instant)

## URLs

```
TECHNICIAN  (1 page)
    /preventive/my/                                  My Maintenance
    /preventive/my/<occurrence_id>/                  Begin Maintenance
    /preventive/my/<occurrence_id>/start/            POST
    /preventive/my/<occurrence_id>/complete/         POST
    /preventive/my/<occurrence_id>/photo/            POST
    /preventive/my/<occurrence_id>/return/           POST

MANAGER  (6 + 1 detail)
    /preventive/manage/                               Dashboard (5 counts)
    /preventive/manage/today/                         Today's Schedule
    /preventive/manage/reviews/                       Reviews queue
    /preventive/manage/reviews/<occ_id>/approve/      POST
    /preventive/manage/reviews/<occ_id>/return/       POST
    /preventive/manage/templates/                      Templates list
    /preventive/manage/templates/<id>/                 Template edit
    /preventive/manage/plans/                         Maintenance Plans list
    /preventive/manage/plans/<id>/                    Plan Details
    /preventive/manage/plans/<id>/edit/               Plan edit
    /preventive/manage/plans/<id>/assign/             POST
    /preventive/manage/plans/<id>/pause/              POST
    /preventive/manage/plans/<id>/archive/            POST
    /preventive/manage/plans/<id>/run-now/            POST
    /preventive/manage/history/                       Searchable history
```

## Old URL redirects

| Old URL | New URL | Status |
|---|---|---|
| `/pm/` | `/preventive/manage/plans/` | 302 |
| `/pm/dashboard/` | `/preventive/manage/` | 302 |
| `/pm/<pk>/execute/` | `/preventive/my/<pm_exec_pk>/` | 302 |
| `/pm/wo/<pk>/` | `/preventive/my/<pm_exec_pk>/` | 302 |
| `/pm/<pk>/spawn-wo/` | `/preventive/manage/plans/<pk>/` | 302 |
| `/pm/batch-spawn-wo/` | `/preventive/manage/plans/` | 302 |

Flip to 301 after 2-4 weeks per deployment runbook.

## Cron setup (production)

```
# /etc/cron.d/mms-pm

# 07:00 daily: generate today's occurrences + morning summaries
0 7 * * * www-data /usr/bin/python3 /app/manage.py pm_daily_routine >> /var/log/mms-pm.log 2>&1

# 09:00 daily: unassigned alerts
0 9 * * * www-data /usr/bin/python3 /app/manage.py pm_overdue_alerts --skip-overdue

# 14:00 daily: overdue alerts
0 14 * * * www-data /usr/bin/python3 /app/manage.py pm_overdue_alerts
```

## Manual commands

```bash
# Generate today's occurrences (idempotent same day)
python manage.py pm_daily_routine

# Force regenerate (skip idempotency check)
python manage.py pm_daily_routine --force-generate

# Just overdue alerts (no morning summary)
python manage.py pm_overdue_alerts

# Skip overdue, just generate + morning
python manage.py pm_daily_routine --skip-overdue
```

## Architecture summary

```
            ┌──────────────────────────────┐
            │   MaintenanceEngine facade    │
            │   maintenance/preventive_engine/ │
            └──────────────┬─────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   Technician UI      Manager UI        (Phase 2: Supervisor)
   1 page: My         6+1 pages: Dashboard,
   Maintenance        Today's, Reviews, Plans,
                      Plan Details, Templates, History
```

## Test count: 642 passing

- 620 existing tests (regression)
- 22 new tests in `maintenance/test_preventive_workflow.py`

## Future expansion (Phase 2+)

The flat service layer is the seam for:
- `preventive_engine.predictive_service` — ML signals spawn PMs
- `preventive_engine.calibration_service` — calibration schedules
- `preventive_engine.autonomous_service` — IoT-driven auto-execution
- Supervisor UI (Team Board)

Each new type is a peer service + page, no nesting.