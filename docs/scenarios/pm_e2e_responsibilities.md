# PM End-to-End Scenario — Who Does What

A step-by-step walkthrough showing **which actor is responsible** for each action, what the system does, and what the data state looks like at each point.

---

## The Cast

| Actor | Role | Responsibility |
|---|---|---|
| **Manager (ahmed)** | `manager` | Owns PM schedules, decides when to spawn WOs, reviews completed PMs, decides approve/reject |
| **Technician (omar)** | `technician` | Executes the checklist, fills notes, submits for review |
| **System (cron)** | automated | Fires the 5-stage notification cascade daily |
| **System (Django view)** | automated | Handles every action — creates rows, transitions state, fires notifications |

---

## Setup (one-time)

Before starting, ensure a clean demo state:

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import PMSchedule, PMExecution
from django.utils import timezone
from datetime import timedelta
PMExecution.objects.all().delete()
s = PMSchedule.objects.first()
s.next_due_at = timezone.now() - timedelta(days=3)
s.last_completed_at = None
s.save()
print(f'Set {s.template.code} to be 3 days overdue')
"
```

This wipes old PMExecutions (so the next spawn won't hit the unique constraint) and pushes the demo PM 3 days into the past.

---

## The 6 Steps

### STEP 1 — System fires overdue notification (cron job)

**Who is responsible**: System (cron `sync_pm_notifications`)
**When**: Runs daily (e.g., 09:00) — check `maintenance/management/commands/sync_pm_notifications.py`
**What it does**:
```python
# Iterate active PMSchedules
for schedule in PMSchedule.objects.filter(is_active=True):
    days_until_due = (schedule.next_due_at.date() - today).days
    if days_until_due < 0:
        notify_pm_overdue(schedule)
```

**Responsible file**: `maintenance/notifications.py:sync_pm_overdue_notifications`
**Data created**:
```python
Notification(
    recipient=ahmed (manager),
    kind="pm_overdue",
    title="PM overdue: PM-HYD-001 — Monthly Hydraulic Pump Inspection",
    body="[pm_sched:4] Line A — Press 1 — due 2026-06-23 20:58.",
    link="/pm/",
)
```

**Dedupe**: 48-hour window — won't spam if cron runs multiple times per day.

---

### STEP 2 — Manager sees overdue PM in sidebar + list

**Who is responsible**: Manager (ahmed) — *observation only, no action yet*
**Where**:
- Sidebar: `Preventive [1]` (red badge from `nav_pm_overdue` counter in `maintenance/context_processors.py:mms_nav`)
- PM list (`/pm/`): row shows `3d overdue` (red badge, `mms-badge--danger`)

**What ahmed sees**:
```
┌──────────────────────────────────────────────────────────────────────┐
│  PM-HYD-001  Line A — Press 1  Monthly  2026-06-23 20:58  [3d overdue] │
│  ☐ (checkbox enabled — only for overdue rows)                        │
│                                          [Create PM WO]              │
└──────────────────────────────────────────────────────────────────────┘
```

**Responsible files**:
- `templates/base.html` (sidebar badge)
- `maintenance/views.py:pm_list` (list rendering + days_until_due annotation)
- `maintenance/context_processors.py:mms_nav` (sidebar counter)

---

### STEP 3 — Manager spawns the Work Order

**Who is responsible**: Manager (ahmed)
**Action**: Click `Create PM WO` button → opens `/pm/4/spawn-wo/` (confirm page) → click `Create PM Work Order` → confirm dialog

**What ahmed sees** (confirm page):
```
┌─ Schedule ────────────────────────────────────────────────┐
│ Template:  PM-HYD-001 — Monthly Hydraulic Pump Inspection│
│ Machine:   Line A — Press 1 (PRESS-01)                  │
│ Frequency: Monthly                                      │
│ Next due:  2026-06-23 20:58                             │
│ Priority:  Medium                                       │
│ Duration:  30 minutes                                   │
└─────────────────────────────────────────────────────────┘

☐ Also create PM work orders for child machines (2)
[Create PM Work Order]  [Cancel]
```

**What the system does** (in `pm_spawn_wo` view, single DB transaction):
```python
@transaction.atomic
def pm_spawn_wo(request, pk):
    sched = PMSchedule.objects.get(pk=pk)
    if not sched.is_active:
        messages.error(...); return redirect("pm_list")
    if PMExecution.objects.filter(pm_schedule=sched, status__in=[SUBMITTED, REJECTED]).exists():
        return redirect("work_order_detail", pk=existing.work_order_id)
    
    wo = WorkOrder.objects.create(
        category=WorkOrder.Category.PREVENTIVE,
        machine=sched.machine,
        lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
        created_by=request.user,
        notes=f"PM: {sched.template.title}",
    )
    transition_work_order(wo, ASSIGNED, actor=request.user, note="PM work order")
    create_pm_execution_for_wo(sched, wo, actor=request.user)
    return redirect("work_order_detail", pk=wo.pk)
```

**Data created**:

`WorkOrder` row:
```python
WorkOrder(
    pk=49, number=43, category="preventive",
    machine=Line A — Press 1,
    lifecycle_status="assigned",
    notes="PM: Monthly Hydraulic Pump Inspection",
    created_by=ahmed,
)
```

`PMExecution` row (with **immutable template snapshot**):
```python
PMExecution(
    pm_schedule=schedule_pk=4,
    work_order=49,
    scheduled_due_at=2026-06-23 20:58,  # locked to this occurrence
    execution_sequence=1,             # first cycle
    status="submitted",
    completed_by=ahmed,
    completed_at=2026-06-26 23:58,
    template_snapshot_json={
        "template_code": "PM-HYD-001",
        "template_title": "Monthly Hydraulic Pump Inspection",
        "template_priority": "medium",
        "template_duration_minutes": 30,
        "checklist": [
            {"order": 1, "text": "Check oil level", "is_required": true},
            {"order": 2, "text": "Inspect for leaks", "is_required": true},
            {"order": 3, "text": "Verify pressure gauge", "is_required": true},
        ],
        "grace_days": 7,
        "captured_at": "2026-06-26T23:58:08+00:00",
    },
)
```

**Responsible files**:
- `maintenance/views.py:pm_spawn_wo`
- `maintenance/services.py:create_pm_execution_for_wo` + `capture_template_snapshot`
- `templates/maintenance/pm_spawn_wo.html`

**Verify in DB**:
```bash
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import WorkOrder, PMExecution
wo = WorkOrder.objects.last()
e = PMExecution.objects.get(work_order=wo)
print(f'WO-{wo.number}: {wo.lifecycle_status}, category={wo.category}')
print(f'PMExec: status={e.status}, seq={e.execution_sequence}, checklist_items={len(e.template_snapshot_json[\"checklist\"])}')
"
```

---

### STEP 4 — Manager assigns the technician

**Who is responsible**: Manager (ahmed)
**Action**: On WO page (`/work-orders/49/`), scroll to **Manager actions** card → select `technician` in the **Technician** dropdown → click **Assign / reassign technician**

**What the system does**:
- Sets `wo.assigned_technician = omar`
- Fires `notify_wo_assigned(wo)` → creates Notification for omar

**Data updated**:
```python
WorkOrder(pk=49, assigned_technician=omar)
Notification(recipient=omar, kind="wo_assigned", title=f"WO-43 assigned to you")
```

**Responsible files**:
- `maintenance/views.py:work_order_assign`
- `maintenance/notifications.py:notify_wo_assigned`

---

### STEP 5 — Technician executes the checklist

**Who is responsible**: Technician (omar)
**Action**: 
1. Open `/work-orders/49/` (or `/work-orders/my/` → click WO)
2. Click **Execute PM** → opens `/pm/49/execute/`
3. Check all 3 checklist items (optionally add notes)
4. Fill in **Root cause** + **Action taken**
5. Click **Submit for Review**

**What omar sees** (execute page):
```
Machine:           Line A — Press 1 (PRESS-01)
PM Schedule:       Monthly Hydraulic Pump Inspection (every 1 monthly)
Technician:        technician

📋 Inspection Checklist
  ☑ Check oil level        [Oil at MAX]
  ☑ Inspect for leaks      [No leaks]
  ☑ Verify pressure gauge  [145 psi OK]

Completion Notes:
  Root cause:  Routine monthly PM
  Action taken: All items completed

[Submit for Review]  [Cancel]
```

**What the system does** (in `pm_execute` POST handler):

1. **Builds `wo.action_taken` from the form** — formatted as `[✓] item\n  Note: ...`:
   ```
   [✓] Check oil level
     Note: Oil at MAX
   [✓] Inspect for leaks
     Note: No leaks
   [✓] Verify pressure gauge
     Note: 145 psi OK
   ```

2. **Calls `technician_submit_for_review(wo, omar)`** which:
   - Sets `wo.labor_stopped_at = now`
   - Transitions WO: `ASSIGNED → PENDING_REVIEW` (via state machine)
   - Fires `notify_wo_pending_review(wo)` → notification to manager + supervisor + super admin

**Data updated**:
```python
WorkOrder(pk=49, lifecycle_status="pending_review", labor_stopped_at=now, action_taken="[✓] Check oil level\n...")
Notification(recipient=ahmed, kind="wo_review", title="WO-43 pending review")
```

**Note**: PMExecution stays at `status="submitted"`. It only transitions when manager reviews.

**Responsible files**:
- `maintenance/views.py:pm_execute`
- `maintenance/services.py:technician_submit_for_review`
- `maintenance/notifications.py:notify_wo_pending_review`
- `templates/maintenance/pm_execute.html`

---

### STEP 6 — Manager reviews and approves

**Who is responsible**: Manager (ahmed)
**Action**:
1. Refresh `/work-orders/49/` → see new **⚖️ Review PM** button
2. Click **Review PM** → opens `/work-orders/49/pm-review/`
3. Review checklist results + snapshot
4. Click **✓ Approve & Close WO** → confirm dialog

**What ahmed sees** (review page):
```
Work Order & Schedule
  WO-43 (link)        Status: Pending review
  Machine: Line A — Press 1 (PRESS-01)
  Technician: technician
  PM Template: PM-HYD-001 — Monthly Hydraulic Pump Inspection
  Schedule: every 1 monthly
  Scheduled due: 2026-06-23 20:58
  Execution sequence: #1
  Completion time: 2026-06-26 23:50

📋 Checklist Results (3 items)
  ✓ Done  Check oil level        required  Oil at MAX
  ✓ Done  Inspect for leaks      required  No leaks
  ✓ Done  Verify pressure gauge  required  145 psi OK

📦 Template Snapshot (immutable)
  Title (at spawn):   Monthly Hydraulic Pump Inspection
  Code (at spawn):    PM-HYD-001
  Priority (at spawn): medium
  Duration (at spawn): 30 minutes
  Captured at:         2026-06-26T23:58:08+00:00

⚖️ Manager Decision
  [Rejection reason textarea]
  [✓ Approve & Close WO]  [✗ Reject & Return]
```

**What the system does** (`manager_approve_pm_execution`):

```python
@transaction.atomic
def manager_approve_pm_execution(execution, *, manager):
    if execution.status not in (SUBMITTED, REJECTED):
        raise ValueError(...)
    
    execution.status = APPROVED
    execution.approved_by = manager
    execution.approved_at = now()
    execution.save(update_fields=["status", "approved_by", "approved_at"])
    
    manager_close_work_order(execution.work_order, manager, approve=True)
    # ↑ transitions WO → CLOSED
    
    schedule = execution.pm_schedule
    schedule.last_completed_at = now()
    schedule.next_due_at = compute_next_due_at(schedule, schedule.next_due_at)
    # ↑ NO DRIFT: advance from current next_due_at, NOT from now()
    schedule.save(update_fields=["last_completed_at", "next_due_at"])
```

**Data updated**:
```python
PMExecution(pk=3, status="approved", approved_by=ahmed, approved_at=now)
WorkOrder(pk=49, lifecycle_status="closed")
PMSchedule(pk=4, next_due_at=now + 30 days, last_completed_at=now)  # NO DRIFT
Notification(recipient=ahmed, kind="wo_closed", title="WO-43 closed")
```

**Verify no-drift**:
```bash
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import PMExecution
e = PMExecution.objects.last()
s = e.pm_schedule
delta_days = (s.next_due_at - e.scheduled_due_at).days
print(f'Original due: {e.scheduled_due_at}')
print(f'New due:      {s.next_due_at}')
print(f'Delta:        {delta_days} days  (must be exactly 30 for MONTHLY × 1)')
"
# Output:
# Original due: 2026-06-23 20:58
# New due:      2026-07-23 20:58
# Delta:        30 days
```

**Why no drift matters**: If the manager approved on July 5 (12 days late), the next cycle would still anchor to July 23 (one month from original due) — NOT July 5 (which would drift the cycle indefinitely).

**Responsible files**:
- `maintenance/views.py:pm_review`
- `maintenance/services.py:manager_approve_pm_execution` + `compute_next_due_at` + `manager_close_work_order`
- `maintenance/notifications.py:notify_wo_closed`
- `templates/maintenance/pm_review.html`

---

## Step 6.5 — Manager verifies outcomes (optional)

**Who is responsible**: Manager (ahmed) — *read-only verification*

Visit these URLs:

| URL | What changed |
|---|---|
| `/pm/` | Schedule row shows `next_due_at = 2026-07-23 20:58` (one month from original due) |
| `/pm/dashboard/` | Pending review: was 1, now 0. Compliance: was 0%, now 100% |
| `/machines/1/` → PM tab | Last execution column shows `Approved on 2026-07-23` |

---

## Responsibility Matrix (compact)

| Step | Manager | Technician | System (cron) | System (views) |
|---|---|---|---|---|
| 1. Cron fires overdue notification | — | — | ✅ | — |
| 2. Sidebar + list show overdue | (observes) | (observes) | — | ✅ renders |
| 3. Spawn WO + PMExecution | ✅ clicks Create | — | — | ✅ creates rows |
| 4. Assign technician | ✅ picks omar | (receives notification) | — | ✅ sets FK + notifies |
| 5. Execute checklist | — | ✅ checks + submits | — | ✅ saves action_taken + transitions WO |
| 6. Approve PM | ✅ clicks Approve | — | — | ✅ APPROVED + CLOSED + schedule advances |
| 7. Verify outcomes | (observes) | — | — | (no-op, views reflect state) |

---

## Error Handling

| Situation | Who handles | What happens |
|---|---|---|
| PM is inactive | Manager | `pm_spawn_wo` redirects with error message |
| Pending execution exists for current due_at | Manager | `pm_spawn_wo` redirects to existing WO (avoids duplicates) |
| Reject without reason | Manager | Service raises ValueError; PMExecution stays SUBMITTED |
| Review PM on non-PM WO | Manager | `pm_review` redirects with error |
| Technician tries to review | — | `role_required` decorator returns 302/403 |

---

## Notifications Generated During the Flow

| Step | Notification kind | Recipient |
|---|---|---|
| 1 (cron) | `pm_overdue` | Manager |
| 3 (spawn) | (none directly — but `notify_wo_assigned` is in step 4) | — |
| 4 (assign) | `wo_assigned` | Technician |
| 5 (submit) | `wo_review` | Manager + Supervisor + Super Admin |
| 6 (approve) | `wo_closed` | Manager + Supervisor + Super Admin |

---

## Quick Reset

To re-run the whole walkthrough:

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import PMSchedule, PMExecution
from django.utils import timezone
from datetime import timedelta
PMExecution.objects.all().delete()
s = PMSchedule.objects.first()
s.next_due_at = timezone.now() - timedelta(days=3)
s.last_completed_at = None
s.save()
"
```

Then start at Step 2.