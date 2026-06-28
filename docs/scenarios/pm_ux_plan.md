# PM UX — Comprehensive Improvement Plan

A holistic plan to fix every UX papercut in the PM lifecycle. The user
identified that ad-hoc fixes leave gaps; this document enumerates all known
PM UX issues and the proposed solutions. We implement in priority order.

---

## Current PM Lifecycle (Reality)

```
                          pm_spawn_wo
                                │
                                ▼
                       ┌────────────────┐
                       │   assigned     │  ← technician can't see checklist here
                       └───────┬────────┘
                          Start work
                                │
                                ▼
                       ┌────────────────┐
                       │  in_progress   │  ← checklist visible inline (after Phase 8 fix)
                       └───────┬────────┘
                       Submit for review
                                │
                                ▼
                       ┌────────────────┐
                       │ pending_review │  ← manager reviews, may reject
                       └───────┬────────┘
                          Approve
                                │
                                ▼
                       ┌────────────────┐
                       │    closed      │
                       └────────────────┘
```

---

## All Known PM UX Problems

### P1. Checklist hidden when lifecycle = Assigned
**Symptom (WO-13 user reported):** Technician opens a freshly-assigned PM WO.
Sees "Start work" button. Does NOT see the checklist. Has to click Start
work first to see the checklist.

**Root cause:** `_wo_actions_technician.html` gates the entire actions panel
(including the checklist + completion form) on `wo.lifecycle_status ==
'in_progress'`. The view sets `can_complete_pm` for assigned too, but the
template doesn't render the form.

**Fix:** Render the checklist + completion form for PM WOs in `assigned` too.
Combine "Start work" + "Submit for review" into one button: clicking submit
on a PM WO auto-starts labor (transitions assigned → in_progress
internally), records the checklist, then transitions in_progress →
pending_review. Net effect: technician goes from Assigned → Pending Review
in one click.

### P2. No PM context page for technicians before WO exists
**Symptom (user said earlier):** "technican should see the pm schedule
before coverting it to wo saw he can have idea"

**Root cause:** The PM schedule detail page (`/pm/schedules/<pk>/` or similar)
is manager-only. Technicians have no place to read "what is this PM?" before
the WO lands in their queue.

**Fix:** Make PM schedule detail publicly viewable (or at least
technician-visible). Show the full template + checklist + last 5 executions
on the schedule. Link from PM WO detail page.

### P3. No timer for PM execution
**Symptom (user said earlier):** "we should have a timer for each pm"

**Root cause:** PMExecution has no started_at/paused_at fields. No way to
measure actual duration vs estimated.

**Fix:**
- Add `started_at`, `paused_at`, `total_paused_seconds` to PMExecution.
- On `/pm/wo/<pk>/` and on the inline checklist, render a live timer
  counting up.
- On submit, store actual_duration_minutes on PMExecution.
- Compare actual vs template.estimated_duration for KPI.

### P4. Rejection feedback unclear to technician
**Symptom:** When manager rejects, technician sees "Rejected" badge but the
manager's rejection reason isn't surfaced prominently.

**Root cause:** `pm_review` view stores `manager_reject_pm_execution(...)` which
updates PMExecution + WorkOrderStateLog. But the technician-facing WO detail
page doesn't prominently show the rejection reason.

**Fix:** On the WO detail page and on `/pm/wo/<pk>/`, when PMExecution.status
is REJECTED, show a yellow banner: "Manager rejected this PM submission:
[reason]. Address the issue and submit again."

### P5. Downtime auto-created as BREAKDOWN for PM WOs
**Symptom (WO-8 user reported):** "there is different category in the same wo
like breakdown and pm"

**Root cause:** `technician_start_work` (services.py:229) creates Downtime
with `type=BREAKDOWN` for any non-emergency WO, including PMs. So PM WOs
generate misleading "Breakdown" downtime records.

**Fix:** PM WOs should create `Downtime.DowntimeType.SCHEDULED` (or no
downtime at all — PMs don't necessarily stop production). Update
`technician_start_work` to inspect `wo.category`.

### P6. PM notification noise
**Symptom:** 70+ notifications for PMs in the bell.

**Root cause:** `sync_pm_notifications` fires for each upcoming PM (7d, 3d,
1d, today, overdue). With many schedules, that's many notifications per
day. No batching.

**Fix:** Group notifications by day bucket. Show "5 PMs due in next 7 days"
as one notification rather than 5 separate ones. Already marked as deferred
to Phase 2 — formalize the approach.

### P7. Priority shows as "n/a" on PM WOs
**Symptom:** Health card shows "Priority: n/a" for PM WOs.

**Root cause:** `priority_badge_class` filter is fed from the WO, but WOs
don't have a priority field (PM priority lives on PMSchedule).

**Fix:** When WO has PMExecution.pm_schedule, show the schedule's
effective_priority instead of n/a. Update health card computation.

### P8. Mass PM spawn produces too many simultaneous WOs
**Symptom:** User has 4+ PM WOs on Press 1 active at once after a batch
spawn.

**Root cause:** Batch spawn creates WOs for all selected schedules in one
click. No staggering, no prioritization.

**Fix:** Default batch spawn to "next 5" with a "select all" option to
override. Sort by priority (HIGH first) + oldest-overdue first. Document
as a design choice.

### P9. PM Compliance metric — no drill-down for "missed" WOs
**Symptom:** Dashboard shows 78% compliance but no link to see WHICH PMs
were missed.

**Root cause:** `compute_compliance` returns counts but no PM list.

**Fix:** Add a `missed_pms` list (PMSchedule rows with at least one MISSED
PMExecution in the window) to the dashboard. Click → list view.

### P10. PM WO doesn't show "what's the next due date"
**Symptom:** After closing a PM WO, technician doesn't know when the next
PM is scheduled.

**Root cause:** WO detail page doesn't link to the PM schedule's
`next_due_at`.

**Fix:** On PM WO detail (and on /pm/wo/<pk>/), after lifecycle=closed,
show: "Next PM scheduled for [date]" with link to PM schedule.

---

## Implementation Priority

| # | Issue | Effort | User Impact | Status |
|---|---|---|---|---|
| P1 | Checklist hidden in Assigned | 2h | **High** | ✅ Done |
| P4 | Rejection feedback | 1h | High | Later |
| P5 | PM creates Breakdown downtime | 1h | Medium | Later |
| P2 | PM context page for tech | 4h | Medium | Later |
| P7 | Priority n/a on PMs | 1h | Medium | Later |
| P10 | Show next due after close | 1h | Medium | Later |
| P3 | Live PM timer | 6h | Medium | Deferred (Phase 2) |
| P6 | PM notification batching | 4h | Low | Deferred (Phase 2) |
| P8 | Mass spawn staggering | 2h | Low | Deferred (Phase 2) |
| P9 | Compliance drill-down | 3h | Low | Deferred (Phase 2) |

---

## Implementation Order

### Now: P1 (Checklist visible in Assigned)
**Scope:** 2 files, ~40 lines, 4-5 tests
- `templates/maintenance/_wo_actions_technician.html`: render checklist + completion form for PMs in assigned too
- `maintenance/views.py` `work_order_submit`: allow PM lifecycle=assigned (auto-start labor internally)
- New tests covering: PM in assigned shows checklist + submit works from assigned

### Next sprint: P5 + P7 + P10 (Small bugs)
**Scope:** 3 files, ~80 lines, 6 tests
- P5: `technician_start_work` distinguishes PM vs breakdown
- P7: WO health card shows PM priority
- P10: WO detail shows next due after close

### Later sprint: P2 + P4 (UX clarity)
**Scope:** 4 files, ~200 lines, 8 tests
- P2: Public PM schedule view + link from WO
- P4: Rejection banner on WO detail + /pm/wo/

### Phase 2: P3, P6, P8, P9
- Larger features, deferred to dedicated sprint

---

## Out of Scope (Documented Separately)

- Live timer (P3) — needs JS timer component + new PMExecution fields
- Notification batching (P6) — already in backlog
- Mass spawn UX (P8) — already in backlog
- Compliance drill-down (P9) — already in backlog
