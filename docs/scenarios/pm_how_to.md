# PM Module — How To Do It End-to-End

A simple step-by-step guide. **What to click, what to type, what you'll see.**

---

## Before You Start

You need:
- A web browser (Chrome recommended)
- Dev server running: `http://localhost:8000`
- Two users — open them in **two different browser windows** (or use private/incognito for one)

| User | Username | Password | Role | Window |
|---|---|---|---|---|
| Manager | `manager` | `test1234` | Manager | Main window |
| Technician | `technician` | `test1234` | Technician | Incognito |

The manager **owns** PM schedules. The technician **executes** them.

---

## Goal

A PM is due. The manager spawns a work order. The technician fills in a checklist. The manager approves. The schedule advances to the next cycle. Compliance goes up.

---

## Step 0 — See What's Already Set Up

The dev database has one demo PM ready:

- **Machine**: Line A — Press 1
- **PM Template**: `PM-HYD-001` — Monthly Hydraulic Pump Inspection (3 checklist items)
- **PM Schedule**: Monthly, next due in ~7 days

To see it:

1. Login as **manager**
2. Go to **Preventive** in the sidebar → you'll see the schedule list
3. Go to **Compliance dashboard** in the top action bar → you'll see the global stats

If you want to start fresh (skip the demo data):

```
http://localhost:8000/admin/maintenance/pmschedule/delete/
```

Then re-seed:
```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py migrate maintenance
```

---

## Step 1 — Make a PM Overdue (so we have something to work with)

The demo PM is due in ~7 days. To speed things up, push it into the past.

**In the manager window:**

1. Go to **Preventive** in the sidebar
2. Click the schedule's template link (`PM-HYD-001`)
3. Click **Edit template** (or use the schedule directly)
4. Find the **Next due at** field on the schedule
5. Change to **yesterday at 09:00**
6. Save

Or do it via Django shell (faster):

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from django.utils import timezone
from datetime import timedelta
from maintenance.models import PMSchedule
s = PMSchedule.objects.first()
s.next_due_at = timezone.now() - timedelta(days=2)
s.save()
print(f'Set next_due_at to {s.next_due_at}')
"
```

Now the PM is overdue. The sidebar shows a red **`Preventive [1]`** badge.

---

## Step 2 — Manager: Spawn the Work Order

**In the manager window:**

1. Go to **Preventive** in the sidebar (`/pm/`)
2. You'll see the schedule with status "2d overdue" (red badge)
3. Find the **Create PM WO** button on the right of that row
4. Click it

You'll land on a clean confirm page showing:
- The template (PM-HYD-001)
- The machine (Line A — Press 1)
- The next due date
- Optional checkbox: "Also create PM work orders for child machines"

5. Click **Create PM Work Order**

You'll be redirected to a new Work Order page (`/work-orders/<id>/`). Look for:

- Status: **Assigned**
- Category: **Preventive**
- A badge saying **PM: Submitted** (top of the WO header)

✅ Done. The WO + a PMExecution record were created in one transaction.

---

## Step 3 — Manager: Assign a Technician

**Still in the manager window:**

1. On the WO page, scroll down to **Manager actions**
2. Find the **Technician** dropdown
3. Select `technician`
4. Click **Assign / reassign technician**

omar now owns the WO. They get a notification.

---

## Step 4 — Technician: Execute the Checklist

**Switch to the technician window (incognito).**

1. Login as **technician**
2. Go to **My queue** in the sidebar (or visit `/work-orders/my/`)
3. Click on the PM WO
4. Click **Execute PM**

You're now on `/pm/<wo_id>/execute/`. You'll see:
- Machine info at the top
- An **Inspection Checklist** with 3 items from the template
- A "Completion Notes" section

5. **Check all 3 checkboxes** for the checklist
6. Optionally add notes in the textboxes next to each item
7. Fill in **Root cause** and **Action taken**
8. Click **Submit for Review**

You'll be redirected back to the WO page. Status changes to **Pending review**.

---

## Step 5 — Manager: Review and Approve

**Back to the manager window.**

1. Refresh the WO page (or open `/work-orders/<id>/`)
2. You'll see a new button: **⚖️ Review PM**
3. Click it

You're now on `/work-orders/<id>/pm-review/`. You'll see:
- WO info at the top
- **Checklist Results** table (3 ✓ Done rows)
- **Template Snapshot** card (immutable — frozen at spawn time)
- **Manager Decision** form at the bottom

4. Optionally type a rejection reason in the textarea (leave empty if approving)
5. Click **✓ Approve & Close WO**
6. Confirm the dialog

You'll be redirected to the WO page with a success message:

> PM approved. Schedule advanced to 2026-08-03 17:16.

✅ Done. The WO is closed. The PMExecution is APPROVED. The schedule moved forward by exactly **one month** (or whatever the interval × frequency says).

---

## Step 6 — Verify the Outcome

**In the manager window**, visit these 3 places:

| URL | What you should see |
|---|---|
| `/pm/` | Schedule row shows new `next_due_at` (one month from original due) |
| `/machines/1/` (PM tab) | Stats updated — one more "Approved" execution in the last-execution column |
| `/pm/dashboard/` | Compliance % updated. If this was your first PM in the window, compliance jumps to 100% |

---

## Behind the Scenes (one-liners)

If you're curious what the system actually did:

- **Step 2 (spawn)**: Created 1 WorkOrder + 1 PMExecution in one DB transaction. PMExecution captures a JSON snapshot of the template (code, title, priority, duration, checklist, captured_at).
- **Step 4 (technician submit)**: Saved checklist results to `wo.action_taken` as `[✓] item\n  Note: ...` lines. Transitioned WO to PENDING_REVIEW.
- **Step 5 (manager approve)**: Set PMExecution to APPROVED. Closed the WO. Advanced `schedule.next_due_at` by `frequency_type × interval` from the **original** `next_due_at` (not from now — this prevents drift).

---

## Common Variations

### Want to reject the PM instead?

At Step 5, instead of clicking Approve, type a reason like *"Step 3 reading is borderline"* and click **✗ Reject & Return**.

The technician will see the rejection and can resubmit. The PM goes back to SUBMITTED status (on the same execution row).

### Want to test the notification cascade?

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py sync_pm_notifications
```

This runs the same code the cron runs daily. Output shows how many notifications were created per stage:

```
  upcoming_7d: 0
  upcoming_3d: 1
  upcoming_1d: 0
  due_today: 0
  overdue: 0
PM notifications sync complete.
```

Each stage only fires once per (schedule, due_occurrence) — re-running the command won't create duplicates.

### Want to apply the PM to child machines?

At Step 2, check the "Also create PM work orders for child machines" box before clicking Create. The parent machine gets a WO + PMExecution; each child machine gets a WO only (they share the parent's PM cycle).

### Want to create a brand new PM template first?

In the manager window:

1. Go to **Preventive → Manage templates** (or `/pm/templates/`)
2. Click **New template**
3. Fill in code, title, description, duration, priority
4. Add 3-5 checklist items (order + text + required)
5. Click **Create template**

Then assign it to a machine:

1. Go to **Preventive** (`/pm/`)
2. Click **New schedule**
3. Pick the template + machine
4. Set frequency, interval, start date, next due date
5. Click **Save**

Now this schedule appears in the list and will start receiving notifications when the cron runs.

---

## Quick Reference — URLs

| Page | URL |
|---|---|
| PM list | `/pm/` |
| New PM schedule | `/pm/new/` |
| Spawn WO from schedule | `/pm/<schedule_id>/spawn-wo/` |
| Execute PM (technician) | `/pm/<work_order_id>/execute/` |
| Review PM (manager) | `/work-orders/<work_order_id>/pm-review/` |
| PM templates | `/pm/templates/` |
| New template | `/pm/templates/new/` |
| Compliance dashboard | `/pm/dashboard/` |
| Machine detail (PM tab) | `/machines/<id>/#tab-pms` |

---

## If Something Goes Wrong

| Problem | Fix |
|---|---|
| "A PM work order for this schedule's current due date is already in progress" | Open the existing WO and finish it, or advance the schedule's `next_due_at` to a new date |
| Sidebar badge doesn't update | Hard-refresh the page (Cmd+Shift+R) — the counter is in the context processor |
| "Cannot spawn a WO from an inactive PM schedule" | Go to `/pm/`, click Edit on the schedule, set `is_active=True` |
| Tests fail | `DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 -m pytest -q --reuse-db` |

---

## The 30-Second Version

If you just want to see the whole flow happen:

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import PMSchedule, PMExecution, WorkOrder
from maintenance.services import create_pm_execution_for_wo, manager_approve_pm_execution
from django.utils import timezone
from datetime import timedelta

s = PMSchedule.objects.first()
print(f'Schedule: {s.template.code} on {s.machine}, next_due={s.next_due_at}')

wo = WorkOrder.objects.create(
    machine=s.machine, category='preventive',
    lifecycle_status='assigned', created_by=s.created_by,
    notes=f'PM: {s.template.title}',
)
create_pm_execution_for_wo(s, wo, actor=s.created_by)
print(f'Created WO-{wo.number} + PMExecution(SUBMITTED)')

e = PMExecution.objects.get(work_order=wo)
e.completed_by = s.created_by
e.completed_at = timezone.now()
e.save()

manager_approve_pm_execution(e, manager=s.created_by)
s.refresh_from_db()
print(f'After approve: WO-{wo.number} = {wo.lifecycle_status}, status={e.status}, next_due={s.next_due_at}')
"
```

That single shell command does Steps 2, 4, and 5 in one shot. Use it when you just want to verify the system works without clicking through the UI.