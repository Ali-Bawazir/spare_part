# PM Module — End-to-End Scenario

A realistic walkthrough of the full preventive maintenance lifecycle from notification cascade to compliance reporting.

## Cast

| Role | User | Purpose |
|------|------|---------|
| Manager | `ahmed` (role=manager) | Owns PM schedules, spawns WOs, reviews PMs |
| Supervisor | `sara` (role=supervisor) | Gets T-1d alerts, escalation visibility |
| Technician | `omar` (role=technician) | Executes checklist, submits for review |

## Asset + Procedure

| Field | Value |
|---|---|
| Machine | `Line A — Press 1` (PRESS-01, asset_level=3) |
| Child machines | `Conveyor System` (PRESS-01-CONV, level=4), `Hydraulic Pack` (M1-PRESS-01-HYD, level=4) |
| PM Template | `PM-HYD-001 — Monthly Hydraulic Pump Inspection` |
| Priority (template) | Medium |
| Duration (template) | 30 min |
| Checklist items | (1) Check oil level [required], (2) Inspect for leaks [required], (3) Verify pressure gauge [required] |
| Schedule | `frequency_type=monthly, interval=1, start_date=2026-07-01, next_due_at=2026-07-01 09:00, grace_days=7, reminder_days_before=7` |

## Day-by-Day Timeline

### T-7 (2026-06-24, 09:00) — First Heads-Up

**09:00 — Cron: `python manage.py sync_pm_notifications`**

System computes `days_until_due = (2026-07-01 − 2026-06-24) = 7` → fires `PM_UPCOMING_7D`.

```
Notification(pm_sched:7|stage:UPCOMING_7D|due:2026-07-01)
  recipient: ahmed (manager)
  title: "PM due in 7 days: Monthly Hydraulic Pump Inspection"
  body:  "Machine Line A — Press 1 — due 2026-07-01 09:00."
  link:  /pm/
```

**09:01 — ahmed checks `/notifications/`** → sees the badge.

**09:02 — ahmed opens `/machines/1/`** → PM tab renders:

```
┌─────────────────────────────────────────────────┐
│  Active PMs: 1   Due This Week: 1   Overdue: 0 │
│  Compliance (90d): 87%                          │
└─────────────────────────────────────────────────┘

| Template        | Frequency | Next due        | Priority |
|-----------------|-----------|-----------------|----------|
| PM-HYD-001      | Monthly   | 2026-07-01 09:00| Medium   |
```

ahmed also sees the **sidebar `Preventive [0]` link** (no overdue badge yet).

**09:03 — ahmed opens `/pm/dashboard/`** — global compliance breakdown for context.

```
Compliance Breakdown (Last 90 Days)
  Scheduled PMs in window: 4
  Completed on time:       3
  Completed late:           0
  Missed:                  1
  Pending review:          0
  Compliance:             75%
```

### T-3 (2026-06-28, 09:00) — Reminder

**09:00 — Cron: `sync_pm_notifications`**

`days_until_due = 3` → fires `PM_UPCOMING_3D` (manager only).

```
Notification(pm_sched:7|stage:UPCOMING_3D|due:2026-07-01)
  recipient: ahmed
  title: "PM due in 3 days: Monthly Hydraulic Pump Inspection"
```

Dedupe tag `[pm_sched:7|stage:UPCOMING_3D|due:2026-07-01]` differs from T-7d tag (different stage) → new notification fires. T-7d dedupe still in effect (no T-7d fired today).

### T-1 (2026-06-30, 09:00) — Wider Team Alerted

**09:00 — Cron: `sync_pm_notifications`**

`days_until_due = 1` → fires `PM_UPCOMING_1D` (manager + supervisor).

```
Notification(pm_sched:7|stage:UPCOMING_1D|due:2026-07-01)
  recipients: ahmed (manager), sara (supervisor)
  title: "PM due tomorrow: Monthly Hydraulic Pump Inspection"
```

ahmed notes it but defers until tomorrow. sara notes that tomorrow's PM is on her team's plate.

### T-0 (2026-07-01) — Due Today

**08:00 — Cron: `sync_pm_notifications`**

`days_until_due = 0` → fires `PM_DUE_TODAY` (manager + supervisor + technicians).

```
Notification(pm_sched:7|stage:DUE_TODAY|due:2026-07-01)
  recipients: ahmed, sara, omar, [all technicians]
  title: "PM due today: Monthly Hydraulic Pump Inspection"
  is_critical: False
```

**08:30 — ahmed opens `/pm/`** with filter `status=due_soon` or default view:

```
PM-HYD-001  | Line A — Press 1 | Monthly | 2026-07-01 09:00 | in 0d  | Medium | [Create PM WO]
                  ↑ Template detail                    ↑ Yellow "in 0d" badge (warning color, ≤7d)
```

**08:32 — ahmed clicks `Create PM WO`** → `/pm/7/spawn-wo/`

New clean confirm form (Phase 8 cleanup):

```
┌─ Schedule ─────────────────────────────────────────────┐
│ Template:  PM-HYD-001 — Monthly Hydraulic Pump Inspection│
│ Machine:   Line A — Press 1 (PRESS-01)                 │
│ Frequency: Monthly                                     │
│ Next due:  2026-07-01 09:00                            │
│ Priority:  Medium                                      │
│ Duration:  30 minutes                                  │
└────────────────────────────────────────────────────────┘

☐ Also create PM work orders for child machines (2)
[Create PM Work Order]  [Cancel]
```

**08:33 — ahmed clicks `Create PM Work Order`** → POST `/pm/7/spawn-wo/`

View executes in a transaction:

```python
wo = WorkOrder.objects.create(
    category=WorkOrder.Category.PREVENTIVE,
    machine=schedule.machine,                    # Line A — Press 1
    lifecycle_status=WorkOrder.LifecycleStatus.ASSIGNED,
    created_by=ahmed,
    notes="PM: Monthly Hydraulic Pump Inspection",
)
transition_work_order(wo, ASSIGNED, actor=ahmed, note="PM work order")
create_pm_execution_for_wo(schedule, wo, actor=ahmed)
```

`create_pm_execution_for_wo` captures the immutable template snapshot:

```python
PMExecution.objects.create(
    pm_schedule=schedule,            # pk=7
    work_order=wo,                   # pk=100 (new)
    scheduled_due_at=schedule.next_due_at,    # 2026-07-01 09:00 (locks occurrence)
    execution_sequence=1,            # first cycle
    status=PMExecution.Status.SUBMITTED,
    completed_by=ahmed,
    completed_at=timezone.now(),
    template_snapshot_json={
        "template_code": "PM-HYD-001",
        "template_title": "Monthly Hydraulic Pump Inspection",
        "template_priority": "medium",
        "template_duration_minutes": 30,
        "checklist": [
            {"order": 1, "text": "Check oil level", "is_required": True},
            {"order": 2, "text": "Inspect for leaks", "is_required": True},
            {"order": 3, "text": "Verify pressure gauge", "is_required": True},
        ],
        "grace_days": 7,
        "captured_at": "2026-07-01T08:33:42+00:00",
    },
)
```

**08:34 — Redirect to `/work-orders/100/`**

ahmed sees:

```
WO-100
Machine: Line A — Press 1    Status: Assigned    Category: Preventive
Notes: "PM: Monthly Hydraulic Pump Inspection"      [PM: Submitted badge — Phase 3]
Lifecycle: Assigned    Operational: Paused    Priority: n/a    Open blockers: 0
```

**08:36 — ahmed assigns omar** via `AssignTechnicianForm`. `wo.assigned_technician = omar`.

`notify_wo_assigned(wo)` fires → notification to omar: "WO-100 assigned to you."

### T-0 (09:00) — Technician Executes

**09:00 — omar opens `/work-orders/my/`** → sees WO-100 (PM).

**09:01 — omar clicks `Execute PM`** → `/pm/100/execute/`

`pm_execute` reads checklist from `schedule.template.checklist_items`:

```
Machine:           Line A — Press 1 (PRESS-01)
PM Schedule:       Monthly Hydraulic Pump Inspection (every 1 monthly)
Technician:        omar

📋 Inspection Checklist
  ☐ Check oil level
     [Add note (optional)]
  ☐ Inspect for leaks
     [Add note (optional)]
  ☐ Verify pressure gauge
     [Add note (optional)]
```

**09:05 — omar checks all 3 items + adds notes:**

```
☑ Check oil level        — "Oil at MAX, no top-up needed"
☑ Inspect for leaks      — "Minor seep at fitting, noted for follow-up"
☑ Verify pressure gauge  — "Reads 145 psi, within spec"
```

Plus completion notes:

```
Root cause:  Routine monthly PM, no fault found
Action taken: All checklist items completed, system within spec
```

**09:06 — omar clicks `Submit for Review`**

View (POST):
1. Builds `wo.action_taken` from checked items:
   ```
   [✓] Check oil level
     Note: Oil at MAX, no top-up needed
   [✓] Inspect for leaks
     Note: Minor seep at fitting, noted for follow-up
   [✓] Verify pressure gauge
     Note: Reads 145 psi, within spec
   ```
2. `technician_submit_for_review(wo, omar)`:
   - Sets `wo.labor_stopped_at = now`
   - Transitions WO → PENDING_REVIEW
   - Fires `notify_wo_pending_review(wo)` → manager + supervisor + super admin
3. Redirects to `/work-orders/100/`

### T-0 (10:00) — Manager Review

**10:00 — ahmed gets notification**: "WO-100 pending review."

**10:02 — ahmed opens `/work-orders/100/`** → sees:

```
[⚖️ Review PM] button  ← Phase 3 — only on PREVENTIVE + PENDING_REVIEW
```

**10:03 — ahmed clicks `Review PM`** → `/work-orders/100/pm-review/`

`pm_review` view shows:

```
Work Order & Schedule
  WO-100 (link)        Status: Pending review
  Machine: Line A — Press 1 (PRESS-01)
  Technician: omar
  PM Template: PM-HYD-001 — Monthly Hydraulic Pump Inspection
  Schedule: every 1 monthly
  Scheduled due: 2026-07-01 09:00
  Execution sequence: #1
  Completion time: 2026-07-01 09:06

📋 Checklist Results (3 items)
  ✓ Done  | Check oil level       | required | Oil at MAX, no top-up needed
  ✓ Done  | Inspect for leaks     | required | Minor seep at fitting, noted for follow-up
  ✓ Done  | Verify pressure gauge | required | Reads 145 psi, within spec

📦 Template Snapshot (immutable)  ← historical record preserved
  Title (at spawn):   Monthly Hydraulic Pump Inspection
  Code (at spawn):    PM-HYD-001
  Priority (at spawn): medium
  Duration (at spawn): 30 minutes
  Captured at:         2026-07-01T08:33:42+00:00

⚖️ Manager Decision
  [Rejection reason (required if rejecting)]
  [✓ Approve & Close WO]  [✗ Reject & Return]  [Cancel]
```

**10:05 — ahmed clicks `✓ Approve & Close WO`** → confirms dialog → POST `/work-orders/100/pm-review/`

View calls `manager_approve_pm_execution(execution, manager=ahmed)`:

```python
@transaction.atomic
def manager_approve_pm_execution(execution, *, manager):
    execution.status = PMExecution.Status.APPROVED
    execution.approved_by = manager
    execution.approved_at = timezone.now()        # 2026-07-01 10:05
    execution.save()

    manager_close_work_order(execution.work_order, manager, approve=True)
    # ↑ Closes WO-100, transitions lifecycle → CLOSED

    schedule = execution.pm_schedule
    schedule.last_completed_at = timezone.now()   # 2026-07-01 10:05
    schedule.next_due_at = compute_next_due_at(schedule, schedule.next_due_at)
    # ↑ NO DRIFT: advance from CURRENT next_due_at = 2026-07-01 09:00
    # Result: 2026-08-01 09:00 (exactly +1 month)
    schedule.save()
```

`notify_wo_closed(wo)` fires → notification to manager/supervisor: "WO-100 closed."

**10:06 — ahmed redirected to `/work-orders/100/`** with success message:

> PM approved. Schedule advanced to 2026-08-01 09:00.

### T-0 (10:10) — Manager Verifies Compliance

**10:10 — ahmed checks `/pm/dashboard/`**:

```
Compliance Breakdown (Last 90 Days)
  Scheduled PMs in window: 5      ← was 4
  Completed on time:       4      ← was 3
  Completed late:           0
  Missed:                  1
  Pending review:          0      ← was 1 (now resolved)
  Compliance:             80%     ← was 75%

Per-Machine Compliance
| Machine         | Active PMs | Overdue | Scheduled | On time | Missed | Compliance |
|-----------------|------------|---------|-----------|---------|--------|------------|
| Line A — Press 1| 1          | 0       | 5         | 4       | 1      | 80%        |
```

**10:11 — ahmed checks `/pm/`** — schedule now shows `next_due_at = 2026-08-01` (next cycle).

### T+25 (2026-07-25) — Next Cycle T-7d

**09:00 — Cron: `sync_pm_notifications`**

`days_until_due = (2026-08-01 − 2026-07-25) = 7` → fires `PM_UPCOMING_7D` for the **next cycle**.

Dedupe tag `[pm_sched:7|stage:UPCOMING_7D|due:2026-08-01]` differs from previous T-7d tag (different `due` date) → fresh notification fires.

Cycle repeats: T-3, T-1, T-0, execute, approve, advance.

## Edge Case — Late Approval (Grace Window)

Same setup but ahmed is on vacation July 1–7. Omar submits normally on July 1, but no one reviews.

**July 1, 09:06** — omar submits → WO-100 PENDING_REVIEW.

**July 1–7** — no review action. `next_due_at` stays at July 1. PMExecution stays SUBMITTED.

**July 8 (T+7d)** — grace period expires. Status quo.

**July 15 (T+14d)** — still pending review. `compliance_pct` shows this as `pending`, not missed.

> **Note**: A future `sync_pm_executions` cron would auto-create a `MISSED` PMExecution when `now() > next_due_at + grace_days`. Currently deferred (Phase 4 said cron is later). When implemented, the cron must handle the case where a `SUBMITTED` execution already exists at the same `scheduled_due_at` — see [Known Design Decisions](#known-design-decisions) below.

## Edge Case — Manager Rejects

Same execution at July 1, but ahmed reviews at July 1, 10:05 and rejects:

```
[Rejection reason]: "Step 3 reading is borderline — please re-verify gauge calibration"
[✗ Reject & Return]
```

`manager_reject_pm_execution`:
```python
execution.status = PMExecution.Status.REJECTED
execution.approved_by = ahmed
execution.approved_at = now
execution.notes += "\n[Rejected 2026-07-01T10:05] Step 3 reading is borderline..."
execution.save()

manager_close_work_order(wo, ahmed, approve=False, rejection_reason="...")
# ↑ wo.rejection_count += 1, wo.rejected_at/by/reason set, WO → IN_PROGRESS
# ↑ schedule.next_due_at NOT advanced (stays overdue until approved)
```

**10:05 — omar sees notification** "WO-100 rejected — please redo Step 3."

omar opens `/pm/100/execute/` again. Re-checks Step 3 with calibrated gauge. Submits again. `pm_execute` POST:

```python
pm_execution = wo.pm_execution
if pm_execution.status == PMExecution.Status.REJECTED:
    pm_execution.status = PMExecution.Status.SUBMITTED
    pm_execution.approved_by = None
    pm_execution.approved_at = None
    pm_execution.completed_by = omar
    pm_execution.completed_at = now
```

WO → PENDING_REVIEW again. Manager approves. Cycle completes.

## Edge Case — Template Edited After Spawn

**July 15** — ahmed edits `PM-HYD-001` template:
- Adds Step 4: "Check belt tension"
- Removes Step 3: "Verify pressure gauge"

**Effect on existing PMExecution**: **none**. The `template_snapshot_json` field is immutable and was captured at spawn time. The review page still shows the original 3-step checklist.

**Effect on next cycle**: The next WO spawned after July 15 will capture the new 4-step snapshot.

This is the **CMMS best practice** the user spec called out: historical records stay accurate even as procedures evolve.

## Data Invariants Enforced

| Invariant | Mechanism |
|---|---|
| One PMExecution per (schedule, due_occurrence) | `UniqueConstraint(fields=["pm_schedule", "scheduled_due_at"])` |
| Schedule.template is mandatory | NOT NULL FK |
| Override falls back to template | `effective_priority` / `effective_duration_minutes` properties |
| Status transitions: SUBMITTED → APPROVED or REJECTED | Manager review view enforces |
| No drift in next_due_at | `compute_next_due_at(schedule, schedule.next_due_at)` (not from now) |
| Template state preserved at spawn | `template_snapshot_json` JSONField, never updated post-spawn |

## KPI / Reporting Impact

**Compliance % over 90 days** (after the approval):

```
on_time = 1  (WO-100, approved July 1 within grace)
scheduled = 1 (single cycle completed in window)
pct = int(round(1/1 * 100)) = 100%
```

**Per-machine breakdown** for `Line A — Press 1`:

```
scheduled = 1, on_time = 1, missed = 0
pct = 100%
```

If ahmed had 4 historical completed cycles (3 on time + 1 missed) plus the new one (on time):

```
scheduled = 5, on_time = 4, missed = 1
pct = int(round(4/5 * 100)) = 80%
```

## Known Design Decisions

- **`MISSED` cron** — Deferred to a future phase. The auto-creation of `PMExecution(status=MISSED)` at `next_due_at + grace_days` must handle the case where a `SUBMITTED` execution already exists at the same `scheduled_due_at`. Two viable approaches:
  1. Skip creating MISSED if any non-terminal execution exists at that occurrence
  2. Auto-transition SUBMITTED → MISSED when grace expires (loses audit of technician submission)
- **Plant Manager escalation** — Per CONTEXT.md, escalation at T+7d overdue to manager + plant manager. Deferred; current code uses `MANAGER + SUPER_ADMIN` for the T-7d/3d/overdue stages.
- **Auto-spawn WOs** — Celery/beat deferred. Phase 8 ships manual spawn only via `pm_spawn_wo`.
- **Meter-based triggers** — `trigger_type` field is in the schema but only `TIME` is implemented.