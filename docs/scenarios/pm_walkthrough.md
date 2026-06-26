# PM End-to-End Walkthrough — Guided Tour

Walk through the full preventive maintenance lifecycle with explanations at each step.

---

## Setup (do this once)

You need:
- Browser open at `http://localhost:8000`
- Two users (use 2 browser windows or use private/incognito for one):
  - **Manager**: username `manager`, password `test1234`
  - **Technician**: username `technician`, password `test1234`

Make sure the demo PM is in a good starting state. From terminal:

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

What this does:
- **`PMExecution.objects.all().delete()`** — wipes any leftover PMExecutions from previous test runs. Without this, the next "Create PM WO" will fail with a unique constraint error (one PMExecution per (schedule, due_occurrence)).
- **`s.next_due_at = now - 3 days`** — pushes the schedule into the past so we have something overdue to act on.
- **`s.save()`** — persists the change.

After this, your PM is **3 days overdue**, the sidebar will show a red **`Preventive [1]`** badge, and the PM list will show a red **`3d overdue`** badge.

---

## The 6 Steps

### Step 1 — Manager sees the overdue PM

**Action**: Login as `manager`. Click **Preventive** in the sidebar (or visit `/pm/`).

**What you see**:
- A row for `PM-HYD-001 — Monthly Hydraulic Pump Inspection` on `Line A — Press 1`
- The **Days Until Due** column shows `3d overdue` in **red**
- A **checkbox** at the start of that row (only overdue rows are checkable for batch spawn)
- A **Create PM WO** button on the right
- Sidebar shows red **`Preventive [1]`** badge

![PM list with overdue row](walkthrough_01_pm_list_overdue.png)

**What this means**:
- The red badge is computed in `pm_list` view — `days = (next_due_at.date() - now.date()).days` → if `< 0`, color is `danger` and label is `{N}d overdue`.
- The sidebar badge comes from `nav_pm_overdue` counter in `maintenance/context_processors.py:mms_nav` — `PMSchedule.objects.filter(is_active=True, next_due_at__lt=timezone.now()).count()`.
- The checkbox is enabled only when `days_until_due <= 0 AND is_active=True` — see the template condition in `pm_list.html`.

---

### Step 2 — Manager spawns the work order

**Action**: Click **Create PM WO** on the row.

**What you see**: A clean confirm page showing the schedule's details (template, machine, frequency, next due, priority, duration) — no editing, just confirmation. There's an optional checkbox to also create WOs for child machines (this machine has 2 children: Conveyor System and Hydraulic Pack).

![Spawn confirm form](walkthrough_02_spawn_form.png)

**Action**: Click **Create PM Work Order**. Confirm the dialog.

**What you see**: You're redirected to a brand new Work Order page (`/work-orders/<id>/`) with:
- Status: **Assigned**
- Category: **Preventive**
- A small **PM: Submitted** badge (Phase 3 feature — shows the PMExecution status)

![WO created with PM badge](walkthrough_03_wo_created.png)

**What happened behind the scenes** (in `pm_spawn_wo` view, single transaction):
1. Created a `WorkOrder` with `category='preventive'`, `lifecycle_status='assigned'`, machine = the schedule's machine
2. Transitioned WO through state machine → ASSIGNED (logs the transition)
3. Called `create_pm_execution_for_wo(schedule, wo, manager)` which:
   - Snapshotted the template state into JSON: `{template_code, template_title, template_priority, template_duration_minutes, checklist: [...], captured_at}`
   - Created a `PMExecution` row with `status='submitted'`, `scheduled_due_at=schedule.next_due_at` (locked to this occurrence), `execution_sequence=1` (first cycle)

**Verify in DB**:
```bash
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import PMExecution
e = PMExecution.objects.last()
print(f'status={e.status} seq={e.execution_sequence} due={e.scheduled_due_at}')
print(f'snapshot.checklist: {len(e.template_snapshot_json[\"checklist\"])} items')
"
```

You should see `status=submitted`, `seq=1`, `due=2026-06-23 ...`, and 3 checklist items in the snapshot.

---

### Step 3 — Manager assigns the technician

**Action**: On the WO page, scroll down to the **Manager actions** card. In the **Technician** dropdown, select `technician`. Click **Assign / reassign technician**.

If the dropdown isn't visible, do it via shell:
```bash
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from accounts.models import User
from maintenance.models import WorkOrder
from maintenance.notifications import notify_wo_assigned
tech = User.objects.get(username='technician')
wo = WorkOrder.objects.last()
wo.assigned_technician = tech
wo.save()
notify_wo_assigned(wo)
"
```

**What happened**:
- The WO now has `assigned_technician=technician`
- `notify_wo_assigned(wo)` fired a notification to the technician

---

### Step 4 — Technician executes the checklist

**Switch to the technician window.**

**Action**: Go to `/pm/<wo_id>/execute/` (or click **Execute PM** from the WO page). You'll see:
- Machine info at the top
- An **Inspection Checklist** with 3 items (loaded from the template, NOT the snapshot — fresh per execution)
- A **Completion Notes** form at the bottom

![Technician PM execute form](walkthrough_04_execute_form.png)

**Action**: Check all 3 checkboxes. Optionally type notes. Fill in **Root cause** and **Action taken**. Click **Submit for Review**.

**What you see**: Redirected to the WO page. Status changed from **Assigned** to **Pending review**.

**What happened**:
1. `pm_execute` POST handler built `wo.action_taken` from the form:
   ```
   [✓] Check oil level
     Note: Oil at MAX
   [✓] Inspect for leaks
     Note: No leaks
   [✓] Verify pressure gauge
     Note: 145 psi OK
   ```
2. Called `technician_submit_for_review(wo, technician)` which:
   - Set `wo.labor_stopped_at = now`
   - Transitioned WO → `PENDING_REVIEW` via the state machine
   - Fired `notify_wo_pending_review(wo)` → notification to manager + supervisor + super admin

The PMExecution row is still `status='submitted'`. It only changes when the manager reviews.

---

### Step 5 — Manager reviews and approves

**Back to the manager window.**

**Action**: Refresh the WO page. You'll see a new button in the header: **⚖️ Review PM** (Phase 3 feature — only shown when WO is PREVENTIVE + has PMExecution + status is PENDING_REVIEW + user is manager). Click it.

**What you see**: The PM Review page (`/work-orders/<id>/pm-review/`) with 3 cards:
1. **Work Order & Schedule** — WO link, status, machine, technician, template, schedule, scheduled due, execution sequence, completion time
2. **Checklist Results** — table with ✓ Done for each item + technician notes
3. **Template Snapshot (immutable)** — frozen copy of the template as it was at spawn time
4. **Manager Decision** form — rejection reason textarea + Approve & Close WO button + Reject & Return button

![PM review form with snapshot](walkthrough_05_review_form.png)

**Why immutable snapshot?** This is CMMS best practice: if you edit the template later (add/remove steps), historical PMExecutions still show what was actually done. The snapshot is write-once.

**Action**: Click **✓ Approve & Close WO**. Confirm the dialog.

**What you see**: Redirected to the WO page with success message:
> PM approved. Schedule advanced to 2026-07-23 23:49.

**What happened** (`manager_approve_pm_execution`):
1. `PMExecution.status = APPROVED`
2. `PMExecution.approved_by = manager`, `approved_at = now`
3. `manager_close_work_order(wo, manager, approve=True)` — closes the WO
4. `schedule.last_completed_at = now`
5. **`schedule.next_due_at = compute_next_due_at(schedule, schedule.next_due_at)`** — NO DRIFT (see below)
6. Fired `notify_wo_closed(wo)` → notification

![WO closed after approval](walkthrough_06_after_approval.png)

**Verify the no-drift advance**:
```bash
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import PMSchedule, PMExecution
e = PMExecution.objects.last()
s = e.pm_schedule
delta_days = (s.next_due_at - e.scheduled_due_at).days
print(f'Original due: {e.scheduled_due_at}')
print(f'New due:      {s.next_due_at}')
print(f'Delta:        {delta_days} days  (must be exactly 30 for MONTHLY × 1)')
"
```

Output:
```
Original due: 2026-06-23 20:49
New due:      2026-07-23 20:49
Delta:        30 days  (must be exactly 30 for MONTHLY × 1)
```

**Why this matters**: If we advanced from `now()` instead of `schedule.next_due_at`, a late approval would shift the cycle. With no-drift, the cycle stays anchored to the original due date even if the approval happens 3 days late. The next PM is always exactly 1 month from the **scheduled** date, not from when we got around to approving.

---

### Step 6 — Verify the outcomes

Visit these 3 URLs as the manager to see the full effect:

**`/pm/`** — PM list now shows:
- Schedule with **next_due_at = 2026-07-23** (one month later, not now+1 month)
- **Days until due**: `in 27d` (gray/muted badge, future)
- No overdue checkbox (no longer overdue)

**`/pm/dashboard/`** — Compliance dashboard updated:
- **Pending review**: was 1, now 0
- **Compliance %**: jumps to 100% (or higher if there were existing approved executions)
- Per-machine table shows this machine with 100% (or whatever %)

![Compliance dashboard after approval](walkthrough_07_compliance_after.png)

**`/machines/1/`** → PM tab — Shows:
- The PM in the table with **last_execution** = Approved on 2026-07-23

---

## What You Just Tested

| Capability | Phase | Verified |
|---|---|---|
| Sidebar overdue badge | Phase 2 | ✅ red `[1]` badge visible |
| PM list filters + days-until-due | Phase 6 | ✅ red `3d overdue` badge |
| PM Template → Schedule → Execution | Phase 1 | ✅ snapshot captured at spawn |
| pm_spawn_wo clean form | Phase 8 cleanup | ✅ confirm form, not full schedule form |
| Manager review flow | Phase 3 | ✅ dedicated review page |
| Approve closes WO | Phase 3 | ✅ `lifecycle=closed` |
| No-drift scheduling | Phase 3 | ✅ exact +30 days from original |
| Compliance dashboard | Phase 5 | ✅ updated after approval |
| Pending execution dedupe | Phase 8 cleanup | ✅ blocks duplicate spawn |

---

## Try Variations

### A. Reject the PM instead

At Step 5, type *"Step 3 reading is borderline — please recalibrate gauge"* in the rejection reason textarea. Click **✗ Reject & Return**.

- PMExecution.status → REJECTED (loops back when technician resubmits)
- WO.lifecycle_status → IN_PROGRESS (returns to technician)
- WO.rejection_count → +1
- Schedule.next_due_at stays put (only advances on approve)

The technician sees a "WO rejected" notification. They open the same `/pm/<wo_id>/execute/` page and resubmit. The PMExecution transitions back to SUBMITTED, the WO goes back to PENDING_REVIEW, and you can re-review.

### B. Spawn to child machines

At Step 2, before clicking Create, check **"Also create PM work orders for child machines"**. You'll get:
- 1 parent WO + 1 PMExecution (the parent has the PM cycle)
- 1 child WO per active child machine (no separate PMExecution — they share the parent's cycle)

This is useful for asset hierarchies where the same procedure applies to a parent machine AND its subassemblies.

### C. Watch the notification cascade

To see the notifications fire without waiting for a real cron run:

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py sync_pm_notifications
```

Output shows counts per stage:
```
  upcoming_7d: 0
  upcoming_3d: 0
  upcoming_1d: 0
  due_today: 0
  overdue: 1
PM notifications sync complete.
```

Then check `/notifications/` to see the new overdue notification.

### D. Edit the template after approval

Visit `/pm/templates/1/edit/`, add a new checklist item ("Check belt tension"). Save.

Now go spawn a NEW PM (set next_due_at to past again, then click Create PM WO). The NEW PMExecution will have 4 items in the snapshot. The OLD approved PMExecution still shows 3 items — historical records preserved.

---

## Reset for Another Run

To do the walkthrough again:

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
print('Reset complete')
"
```

This wipes PMExecutions and pushes the schedule to overdue again. Now run through Steps 1-6 again.