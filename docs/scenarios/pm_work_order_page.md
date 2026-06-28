# PM Work Order Page — Technician Execution Flow

A focused scenario for the dedicated PM work order page (`/pm/wo/<pk>/`).
This is the **one page** a technician uses to actually execute a PM.

---

## Why This Page Exists

Before this page, a technician assigned to a PM work order opened `/work-orders/<id>/` and saw:

- A "Preventive" category badge
- A "PM: Submitted" badge
- **No checklist, no obvious next step**

The checklist only lived at `/pm/<wo_id>/execute/` which required typing the URL.
The PM Work Order page (`/pm/wo/<pk>/`) fixes this by being the **canonical
technician-facing PM work order view**, reachable from a prominent button on
the regular WO detail page.

---

## Before You Start

You need:
- Dev server running: `http://localhost:8000`
- One user: `technician` / `test1234`
- An existing PM work order assigned to that technician

If you don't have one yet, follow `pm_how_to.md` first (Steps 1–5 will get you to a
manager-assigned PM WO).

---

## Scenario: Technician Executes a PM Work Order

### 1. Open My Queue

URL: `http://localhost:8000/work-orders/?assigned=me` (or click "My queue" in sidebar)

Find a work order with **Preventive** in the category column.

### 2. Open the Work Order

Click the WO number. You land on `/work-orders/<id>/`.

**If the WO is in `Assigned`** — you'll see the **PM Inspection Checklist**
inline immediately. The submit button reads **"✓ Start work & submit PM
for review"**. Click it once you finish: labor starts, the WO moves to
`In progress`, then `Pending review` — all in one transaction.

**If the WO is in `In progress`** — same checklist, but the submit button
reads **"Submit for review"** (labor is already running).

Both lifecycles support the same inline checklist workflow. The legacy
"Start work" button is preserved so you can still begin labor without
filling the checklist yet (useful for "I need to look at it first" pauses).

There is also a green button at the top:

> ▶ **Execute PM (with checklist)**

…that links to the dedicated `/pm/wo/<pk>/` page (Section 3). Use it if you
prefer a larger, focused view. Both paths produce the same `action_taken`
summary.

### 3. Land on the PM Work Order Page

Clicking the button takes you to `/pm/wo/<pk>/`.

You'll see four regions:

| Region | What It Shows |
|---|---|
| **Breadcrumb** | "← Back to WO-N" — returns to the regular WO page |
| **Blue info banner** | "🔁 This is a Preventive Maintenance work order" + PM badge |
| **PM Context card** | WO number, lifecycle, machine, priority, technician, PM schedule, frequency, estimated duration, last completed / next due, PM execution sequence |
| **Inspection Checklist** | One row per item, each with a checkbox + optional note input |

The "PM Schedule" line tells you which template is being used. The "Frequency"
line tells you the cadence (e.g. "Every 1 daily" or "Every 1 weekly").

### 4. Fill the Checklist

For each item:
- Tick the checkbox if the step is done
- Optionally type a note (e.g. "oil level OK, top-up not needed")

You don't have to tick all of them. Whatever you tick is what the manager sees
in the action_taken summary.

### 5. Fill the Completion Notes

Scroll below the checklist. Three fields:
- **Root cause** — short description of any issue found (or "none")
- **Action taken** — short free-text summary
- **Notes** — anything else worth flagging to the manager

### 6. Submit for Review

Click the green **✓ Submit for Review** button.

What happens:
1. The form POSTs to `/pm/wo/<pk>/`
2. The view writes a structured checklist summary into `wo.action_taken`, formatted like:

   ```
   [✓] check oil
     Note: oil level OK
   [✗] check the filter
     Note: filter dirty, replace soon
   [✓] check the color
   ```

3. The WO's lifecycle moves: `assigned` → `pending_review`
4. The `PMExecution.status` stays `submitted`
5. You are redirected to `/work-orders/<pk>/`
6. A success message appears: "PM submitted for manager review."

### 7. Hand Off to Manager

The page now shows:
- Lifecycle badge: **Pending review**
- The "Execute PM" button is gone (replaced by the read-only state)
- The action_taken summary is visible in "Action taken"

The manager now opens the same `/pm/wo/<pk>/` page in read-only mode, clicks
"⚖️ Review PM" on the WO detail page (or visits `/work-orders/<pk>/pm-review/`)
to approve or reject.

---

## What If the Manager Rejects?

The lifecycle goes back to `in_progress`. The PMExecution status becomes
`rejected`. The technician opens `/pm/wo/<pk>/` again — the checklist is now
**read-only** (showing the previous attempt) and the lifecycle banner reads
"Pending review (rejected)" or similar.

The technician can review what the manager wrote in the rejection note, fix
the underlying issue, and submit again. The same `_pm_wo_detail` page handles
the resubmit — it detects the rejected status and moves the PMExecution back
to `submitted` on the next POST.

---

## Edge Cases

| Situation | What You See |
|---|---|
| WO is closed/cancelled | Page shows read-only checklist + "← Back to WO" only |
| You're not the assigned technician and not a manager | Page shows 403 (forbidden) |
| WO is not PM (category = Breakdown/Repair/Emergency) | Redirects to `/work-orders/<pk>/` with an error message |
| PM template has no checklist items | Empty form still works; you can submit with notes only |
| You're using a bookmarked old URL `/pm/<id>/execute/` | Auto-redirects to `/pm/wo/<id>/` |

---

## URL Summary

| URL | Purpose | Who Can Access |
|---|---|---|
| `/work-orders/<pk>/` | **The WO detail page — now shows inline PM checklist** | Assigned technician + manager roles |
| `/pm/wo/<pk>/` | The dedicated PM work order page (this scenario) | Assigned technician + manager roles |
| `/pm/<pk>/execute/` | Legacy URL — redirects to `/pm/wo/<pk>/` | (always redirects) |
| `/work-orders/<pk>/submit/` | POST endpoint for inline checklist submission | Assigned technician |
| `/work-orders/<pk>/pm-review/` | Manager approve/reject page | Manager only |
| `/pm/schedules/<pk>/` | PM schedule detail (for context) | Manager |
| `/pm/` | PM list (overdue, all schedules) | Manager + supervisor |
| `/pm/dashboard/` | Compliance dashboard | Manager + supervisor |

---

## Related Docs

- `pm_how_to.md` — full PM lifecycle (create → spawn → execute → review)
- `pm_end_to_end.md` — narrative walkthrough
- `pm_walkthrough.md` — step-by-step with setup
- `pm_e2e_responsibilities.md` — who does what at each step
