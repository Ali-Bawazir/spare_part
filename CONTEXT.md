# Factory Maintenance & Spare Parts Management System (MMS)

One internal web application managing the full maintenance lifecycle of a factory:
issue reporting, work orders, technician assignment, spare parts inventory, procurement,
preventive maintenance, external repairs, reusable tools, downtime tracking, and KPI analytics.

## Language

**Work Order**:
A maintenance job assigned to a technician, tracking labor, parts, downtime, and closure.
_Avoid_: Job, Task, Service Request

**Maintenance Issue**:
An operational problem reported against a machine by an operator, awaiting validation
and work order conversion.
_Avoid_: Ticket, Request, Alert

**Downtime**:
A period during which a machine is not operational. Each downtime record belongs to
a Work Order and tracks type, start, end, and duration.
_Avoid_: Breakdown period, Stoppage, Outage

**Spare Part**:
A trackable inventory item (part or consumable) stored in inventory, issued against
work orders, and replenished via procurement.
_Avoid_: Part, Component, Material

**Inventory**:
The site-specific stock record linking a Spare Part to a physical location and
available/reserved quantity.
_Avoid_: Stock, Warehouse Record

**ConsumableAssignment**:
A business accountability record created when an operator self-logs an approved
consumable item. Contains part, consumed_by (operator), issued_by, quantity,
source, approved, site, optional machine, note, stock_movement FK, and created_at.
Links to StockMovement via direct FK and reference JSON for reconciliation.
_Avoid_: Consumable Log, Operator Consumption Record

**allow_operator_consumption**:
A Boolean flag on SparePart. When True, the item appears on the operator's
/consumables/ self-service page and can be logged without a work order.
When False, the item requires work-order-based issuance (manager approval).
_Avoid_: Operator Flag, Self-Service Item

**Stock Movement**:
An audit record of every change in inventory quantity. Each movement records what
happened (movement_type) and why (reference: invoice, PO, supplier, attachment).
_Avoid_: Transaction Log, Stock Log

**Purchase Order**:
A procurement document issued to a supplier for one or more spare parts, with
line-item receiving support (partial receipts, close-short).
_Avoid_: Purchase Request, Order

**Purchase Request**:
An internal request for procurement, created automatically when stock is insufficient
or manually by a manager. Can be linked to a Purchase Order.
_Avoid_: Requisition, procurement demand

**External Repair Order**:
A repair job sent to an external vendor. Tracked end-to-end: creation, vendor
assignment, sending, return, manager acceptance. Linked to a Spare Part.
_Avoid_: Repair Ticket, Vendor Repair, Out-of-house Repair

**Assignment History**:
An immutable append-only record of every technician assignment period on a Work Order.
Each record tracks who was assigned, when, and when they were released.
_Avoid_: Technician Log, Assignment Log

**Failure Category**:
A top-level classification of equipment failures (Mechanical, Electrical, Hydraulic,
etc.). Globally shared across all machines.
_Avoid_: Issue Type, Problem Category

**Failure Mode**:
A specific failure pattern within a category (e.g., Bearing Failure under Mechanical).
Globally shared. Auto-assigned a failure code (MECH-BRG-001).
_Avoid_: Failure Type, Defect Mode

**Asset Hierarchy**:
The plant → line → machine → component tree structure. Issues and PMs can be logged
at any level. Downtime aggregates from components to parent machines.
_Avoid_: Machine Tree, Equipment Hierarchy

**Site**:
A physical factory or warehouse location. One site (Main Factory) in Phase 1,
architected for multi-site later.
_Avoid_: Factory, Location, Warehouse

**Part Issue Line** (`PartIssueLine`):
The audit + cost record created when a spare part is issued against a work
order. Tracks part, quantity, unit cost, who issued, who requested (Phase 2.1),
approval status (`PENDING` / `APPROVED` / `REJECTED`), emergency auto-approve
flag, and timestamps. Rolls into `WorkOrderCost.parts_cost`.
_Avoid_: Part usage, Issue record

**External Repair Request** (`ExternalRepairRequest`):
The technician's PENDING request to send a part to an external vendor for
repair. Manager reviews and either APPROVES (which creates a DRAFT
`ExternalRepairOrder` on the WO) or REJECTS with a reason. Technicians cannot
create the ERO directly because EROs create vendor engagement and financial
obligation — those are management decisions.
_Avoid_: Vendor request, Repair request

**External Repair Order** (`ExternalRepairOrder`):
The actual external-repair engagement. Created either by a manager approving
a technician's request (Phase 2.2) or by a manager/supply officer directly
(legacy path). Status flow: `DRAFT` → `SENT_TO_VENDOR` → `RETURNED` →
`CLOSED` (manager acceptance) or `REJECTED`.
_Avoid_: Vendor job, Out-of-house ticket

**Assignment History** (`WorkOrderAssignmentHistory`):
Immutable append-only log of every technician assignment period on a work
order. Each record tracks who was assigned, when (`assigned_at`), when
released (`unassigned_at`), and the reason. Used for response-time metrics
in Phase 2.6. Never deleted or modified — only new rows added.
_Avoid_: Assignment log

**Pause Reason** (`WorkOrder.pause_reason`):
Categorized reason stored on a paused work order. Enum values:
`EMERGENCY` (auto-paused for emergency override), `OPERATIONAL` (technician
switched task), `OTHER` (free-text note required). Required for all
`IN_PROGRESS → PAUSED` transitions. `AWAITING_PARTS` / `AWAITING_VENDOR`
are *statuses* on the WO, not pause reasons (Phase 3 Q6 grill).
_Avoid_: Pause note, Free-text pause

**Maintenance Supply Officer**:
The renamed role formerly called "Procurement Officer". Covers spare-part
purchasing, tool purchasing, vendor repairs, quotations, POs, and supplier
communication. Display name updated in Phase 2.7. DB enum value
(`User.Role.PROCUREMENT`) is unchanged for migration safety.

---

## Phase 1.x Implementation Decisions

### PR vs PO Separation
- **PurchaseRequest (PR)** = internal demand signal (operator/manager creates when stock is low)
- **PurchaseOrder (PO)** = actual supplier transaction (procurement officer creates after negotiating with supplier)
- PR can exist without PO (manual purchase)
- PR can convert to PO (structured procurement)
- Status cascade: PENDING → CONVERTED_TO_PO → PARTIALLY_FULFILLED / FULFILLED

### PO Number Format
- Format: `PO-YYYY-NNNN` (e.g., PO-2026-0001)
- Auto-generated on creation, NOT editable
- Sortable by year and sequence number

### WO Rejection
- Manager MUST provide mandatory rejection reason when returning WO to technician
- Rejection reason stored on WorkOrder: `rejected_at`, `rejected_by`, `rejection_reason`, `rejection_count`
- Full timeline preserved in WorkOrderStateLog
- Rejection count tracked for reporting

### Machine Cost Formula
Machine Cost = parts_cost + vendor_cost + consumables_cost + additional_cost
- **Excludes** labor cost (fixed salaried technicians — not variable per machine)
- **Excludes** downtime cost (requires finance team to provide real values)
- Reintroduce when production accounting matures

### WorkOrderCost Model
- Tracks cost breakdown per WO: parts, vendor, consumables, additional
- Auto-calculates from PartIssueLine + ExternalRepairOrder + StockMovement
- Rolls up to machine for cost reporting

### Attachment System
- Centralized file attachment for all operational entities
- Storage: `attachments/originals/` + `attachments/thumbs/` (300px)
- Max: 10 attachments per entity, 5MB each, JPG/PNG/WEBP/PDF
- Hybrid upload: client-side preview during form fill, upload after save
- Entity types: work_order, machine, spare_part, purchase_request, purchase_order, maintenance_issue, stock_movement, repair_order

### Hybrid Photo Upload (Issue Reporting)
1. Operator selects photos on issue_form.html (client-side preview)
2. On submit: issue saved first (multipart/form-data)
3. After issue has PK: attachments linked via AJAX
4. If user cancels form: nothing stored (no orphaned uploads)

### PM Execution
- Manual spawn only in Phase 1 (manager clicks "Spawn WO" per schedule)
- Auto-spawn deferred to Phase 2 (Celery/beat)
- PM execution via `/pm/<pk>/execute/` checklist page

### Operator Self-Service Consumables
- Dedicated `/consumables/` page for operators (self-log approved consumables)
- SparePart flag `allow_operator_consumption` gates access
- Only items with `is_consumable=True AND allow_operator_consumption=True` appear
- On submit: creates both ConsumableAssignment (business ledger) AND StockMovement (inventory audit)
- Both created atomically in a single transaction via `consumable_use()` service
- ConsumableAssignment fields: part, consumed_by, issued_by, quantity, source, approved, site, machine, note, stock_movement FK
- `source` choices: SELF_SERVICE, SUPERVISOR_ISSUE, WO_CONSUMPTION
- Phase 1: `issued_by = consumed_by` (self-consumption, no supervisor pre-approval)
- Phase 1: `approved = True` (auto-approved, no approval queue)
- Phase 2: supervisor approval workflow, consumption quotas, shift-based authorization

### Operator Issue Tracking
- Operators can view their own reported issues via `/issues/` (filtered by reported_by)
- Issue detail page at `/issues/<pk>/` shows: description, machine, status, priority, uploaded photos,
  created_at, validated_by/at (if any), linked WO number (if exists)
- Operator cannot edit issues after submission
- Linked WO found via `issue.issues.all()` (OneToOne reverse relation from WorkOrder to MaintenanceIssue)
- Phase 2: push notifications when status changes

### Operator Role Summary
| Action | Operator |
|--------|----------|
| Report maintenance issue | ✅ |
| View own issues (list + detail) | ✅ |
| Self-log approved consumables | ✅ |
| View tool availability | ✅ |
| Return assigned tools | ✅ |
| View own consumable history | ✅ via ConsumableAssignment queries |
| Validate issues | ❌ |
| Create work orders | ❌ |
| Issue spare parts to WO | ❌ |
| Close WO | ❌ |
| Access system reports | ❌ |

### Technician

A maintenance executor who performs repair work on assigned work orders.
Owns the labor timer, parts consumption, downtime recording, and tool
checkout for tasks assigned by a Manager. Cannot validate issues,
create work orders, approve closure, or perform procurement.

_Avoid_: Worker, Mechanic, Engineer, Fitter

### Part Request Workflow (Hybrid Approval)

To balance inventory control with operational speed, parts are issued
through a hybrid approval flow on work orders:

```
Technician (assigned WO)
   → Add Part Request (PENDING)
      → Inventory NOT yet deducted

Manager / Supervisor
   → APPROVE  → StockMovement created, inventory deducted, PartIssueLine APPROVED
   → REJECT  → PartIssueLine marked REJECTED, no inventory change
   → Edit Qty → quantity adjusted before approval
```

**Emergency exception** — On `WorkOrder.is_emergency = True`, the
technician's part request deducts inventory immediately and is
auto-flagged for manager post-review (prevents production downtime).

**Field additions to `PartIssueLine`:**
- `status` — `PENDING` | `APPROVED` | `REJECTED`
- `requested_by` — User (the technician who added it)
- `approved_by` — User (nullable, set on approval/rejection)
- `approved_at` — DateTime (nullable)
- `rejection_reason` — Text (nullable, mandatory on rejection)

**Phase 1 rule:** Only the assigned technician can add a PENDING request
to their own WO. **Only the Manager (or Super Admin) can approve/reject/edit
any PENDING line** — parts = money = single approval authority. Supervisor
does NOT have approval power over part requests (confirmed Phase 2.10
grill). Manager gains `approve_part_request` capability; technician gains
`request_part_on_wo` capability (own WO only).

### External Repair Request Flow (Technician Requests, Manager Creates)

Technicians are maintenance executors, not purchasing or vendor
coordinators. They diagnose and request; managers authorise the
financial commitment; the support/procurement officer runs the vendor
side.

```
Technician (assigned WO)
   → Diagnose, add WO note
   → Click "Request External Repair"
      → Creates a PENDING request linked to the WO
      → Manager notified

Manager
   → Review diagnosis
   → APPROVE request
   → Creates External Repair Order (ERO) on the WO
   → Status: PENDING_VENDOR

Support / Procurement Officer
   → Receive ERO
   → Select vendor, request quotation
   → Send part to vendor
   → Status: SENT_TO_VENDOR
   → On return: record vendor, invoice, cost, return date
   → Status: RETURNED

Manager
   → Verify returned part, enter invoice, accept/reject
   → If accepted: ERO CLOSED, cost flows into machine cost report
```

**Why technician cannot create the ERO directly:** ERO creates
vendor engagement, cost, invoice, and financial obligation —
management decisions. Technicians must not create financial commitments.

**Field/model addition:** A small "ExternalRepairRequest" record (or
a boolean `external_repair_requested` + status field on `WorkOrder`)
is added so the technician's request is visible separately from
the ERO itself.

### Support / Procurement Officer

The role formerly called "Procurement Officer" is renamed to
**Maintenance Supply Officer** because it covers more than buying:

- Spare-part purchasing
- Tool purchasing
- Vendor repairs (External Repair Orders)
- Quotations
- Purchase Orders
- Supplier communication

**Phase 1 caveat:** The role name in the database (`User.Role.PROCUREMENT`)
is kept for migration safety. Display name and notifications use
"Maintenance Supply Officer". The role enum is renamed to
`User.Role.SUPPLY` in Phase 2 migration.

### Emergency Workflow

Emergency work orders are created **upstream** of the technician —
operators flag the issue, supervisors escalate, and managers create
the WO. Technicians cannot self-raise emergencies (prevents abuse and
keeps prioritization under management control).

```
Operator reports issue → marks it EMERGENCY
  → Supervisor validates
    → Manager creates Emergency WO
      → is_emergency = True
        → Auto-pauses the technician's other WOs
          → Technician starts the Emergency WO
            → Previous WO PAUSED (reason = "EMERGENCY")
              → After Emergency closes → previous WO can be resumed
```

**Effects of `is_emergency = True`:**
- Skip manager approval on part requests (deduct inventory immediately)
- Top priority in technician queue
- Tagged in KPI reporting (separate emergency metric)
- Other technician WOs auto-pause with reason `EMERGENCY`

**Phase 1 source paths for emergencies:**
- Operator reports issue with `is_emergency = True` checkbox
- Supervisor escalates a normal issue to emergency during validation
- Manager creates Emergency WO directly (no upstream issue required)

### Work Order State Model (Technician Visibility)

**Busy** (blocks starting another WO for that technician):
- `IN_PROGRESS`

**Free** (technician can pick up another WO):
- `PAUSED`
- `WAITING_FOR_PARTS`
- `WAITING_FOR_VENDOR`
- `PENDING_REVIEW`
- `COMPLETED`
- `CLOSED`

**Why WAITING_FOR_PARTS and WAITING_FOR_VENDOR are "free":** If
the technician is blocked on supply, there is no productive work
for them on that WO. Holding them hostage reduces utilization and
creates scheduling pressure on managers. The WO itself stays alive
(parts arrive, vendor returns), and the technician can resume when
the blocker clears.

### Pause Reason Categories

Pause transitions (`IN_PROGRESS → PAUSED`) MUST record a categorized
reason — not a free-text string. Allowed categories:

| Category | Meaning |
|----------|---------|
| `EMERGENCY` | Another emergency WO started, auto-paused |
| `OPERATIONAL` | Operational interruption (e.g., other priority, meeting, instruction) |
| `OTHER` | Anything else, free-text `pause_note` required |

**Removed in Phase 3 (Q6 grill):** `AWAITING_PARTS` and `AWAITING_VENDOR`
were removed from the enum. They are *work-order statuses* (with their
own dedicated workflow), not pause reasons. A technician waiting for
parts should transition the WO to `WAITING_FOR_PARTS`; waiting for a
vendor → `WAITING_FOR_VENDOR`. Use `PAUSED` only when there is no
dedicated status that fits the interruption.

### Resume Validation (Emergency Precedence)

When a technician tries to resume a `PAUSED` work order, the system
MUST check whether another `IN_PROGRESS` work order assigned to that
technician has `is_emergency = True`. If yes, the resume is blocked
and the technician is told to finish the emergency first. This
matches SRS UC-06 step 2D ("system validates: no conflicting
emergency state").

### Technician Reports

Per SRS Section H, technician performance is reported on:
- Completed work orders
- Average repair duration (labor hours)
- Average response time (assignment → first start)
- Reopened jobs (rejected WOs returned to technician)
- External repair count (WOs that went through WAITING_FOR_VENDOR)

These roll up on the existing `/reports/` and `/kpi/` pages, and a
dedicated `/reports/technicians/<id>/` drill-down shows per-technician
detail.

### Technician-Facing URLs

`/work-orders/my/` — the technician's personal queue. Same data as
`/work-orders/` filtered to `assigned_technician=request.user`, but
with a clearer title, a "back to dashboard" link, and the technician's
open-WO badge. The existing `/work-orders/` list also continues to
work (and is where managers view all WOs).

### Deferred to Phase 2

- Role-based login redirect (Q7)
- Dedicated mobile bottom nav for technicians (Q8)
- Auto PM spawn (Celery beat)
- Reservation workflow (quantity_reserved field already in schema)
- Supplier analytics
- SLA engine
- Full offline PWA
- ERP integration
- Advanced forecasting / predictive maintenance
- IoT/runtime meters
- Barcode printer integration
- Email notifications (hooks in place, not active)
- Operator consumption approval workflow (supervisor pre-authorization)
- Consumption quotas / daily limits per operator
- Shift-based authorization for consumables
- ConsumableAssignment supervisor approval queue
- Real-time push notifications for status changes

### Technician Role Summary
| Action | Technician |
|--------|------------|
| View own assigned WOs (prioritized queue) | ✅ |
| Start work on own WO | ✅ |
| Pause work on own WO (with categorized reason) | ✅ |
| Resume own paused WO | ✅ (unless blocked by active emergency) |
| Mark own WO WAITING FOR PARTS / VENDOR | ✅ |
| Add part request to own WO (PENDING) | ✅ |
| Directly deduct inventory on own WO (emergency only) | ✅ |
| Submit own WO for manager review | ✅ |
| Execute PM checklist on own assigned PM WO | ✅ |
| Request external repair on own WO | ✅ (creates PENDING request) |
| Create quick maintenance logs | ✅ |
| View read-only stock lookup | ✅ |
| View & return assigned tools | ✅ |
| View own notifications | ✅ |
| View reports & KPIs (own scope) | ✅ |
| View own performance drill-down (`/reports/technicians/<self>/`) | ✅ |
| Approve own part requests | ❌ |
| Issue parts on WOs not assigned to self | ❌ |
| Edit inventory thresholds / stock-in | ❌ |
| Validate issues | ❌ |
| Create work orders | ❌ |
| Assign self or other technicians to WOs | ❌ |
| Close / approve WOs | ❌ |
| Create purchase requests | ❌ |
| Create external repair orders | ❌ (request only) |
| Manage tools / assign to others | ❌ |

### Phase 2 Implementation Decisions

#### Technician-Facing URL
`/work-orders/my/` — the technician's personal queue. Same data as
`/work-orders/` filtered to `assigned_technician=request.user`, but
with a clearer title, an "open WO" / "in-progress" badge, and a
back-to-dashboard link. The existing `/work-orders/` list also
continues to work (and is where managers view all WOs).

#### Manager drill-down per technician
`/reports/technicians/<id>/` shows per-technician performance:
- Completed WOs (closed count)
- In-progress WOs
- Reopened jobs (sum of `rejection_count`)
- External repair count (WOs that went through `WAITING_FOR_VENDOR`)
- Avg. repair duration (mean labor minutes on closed WOs)
- Avg. response time (assignment → first start in minutes)
- Recent closed work orders

Linked from `/reports/` and `/kpi/` technician tables. Technicians can
also view their own report; managers/supervisors/super admins can view
any technician.

#### Role display name polish
The display name "Procurement Officer" is renamed to **Maintenance
Supply Officer** in the UI (sidebar, user list, delete confirmation).
The DB enum value (`User.Role.PROCUREMENT`) is unchanged for
migration safety. Helper: `accounts.utils.role_display_name()` and
the `{% user_role %}` template filter.

### Phase 2.x Grill Decisions (Phase 2.10)

A grilling session against the SRS Section 6 (Maintenance Manager)
surfaced several deviations between our current implementation and the
locked SRS scope. Decisions recorded:

- **Q1 — Part-request flow vs SRS UC-09**: keep the Phase 2.1 hybrid
  approval (technician PENDING → manager approval). Manager retains
  the legacy `work_order_issue_part` as an override for urgent
  corrections. Documented as a deliberate Phase 2 extension to the
  SRS. The standard flow is "tech requests, manager approves".
- **Q1.5 — Approver scope**: `approve_part_request` capability is
  MANAGER + SUPER_ADMIN only. Supervisor is explicitly excluded
  (parts = money = single approval authority).
- **Q2 — External repair flow**: keep the Phase 2.2 two-step
  `ExternalRepairRequest` → `ExternalRepairOrder` flow. Technician
  creates the PENDING request; manager/supply officer approves and
  spawns the DRAFT ERO. Legacy `repair_create` preserved as an
  override.
- **Q3 — Emergency WO creation paths**: keep `EmergencyWOForm` AND
  add (Phase 3) operator emergency-issue reporting AND supervisor
  escalation. Three canonical sources of `is_emergency=True`,
  manager approval never removed.
- **Q4 — Manager action gaps** → see **Phase 3 to-do list** below.
- **Q5 — Reports / KPIs access**: `/reports/` and `/kpis/` add
  Supervisor to the role decorator and gain a section-level filter.
  Roles stay `MANAGER + SUPERVISOR + SUPPLY + SUPER_ADMIN`, but each
  section is gated per the matrix in Phase 3.1.5 below.
- **Q6 — Pause reason enum cleanup**: drop `AWAITING_PARTS` and
  `AWAITING_VENDOR` from the enum. Pause reasons are now
  `EMERGENCY | OPERATIONAL | OTHER` only.
- **Q7 — Rejection count**: keep `rejection_count` as the
  "reopened jobs" KPI in the technician drill-down. Meaningful
  signal for diagnosing chronic-quality issues.
- **Q8 — WO close/reject roles**: keep `MANAGER + SUPER_ADMIN` only.
  Matches SRS UC-07.

### Phase 3 — Manager Operations Completion (to-do list)

The three highest-value SRS gaps surfaced by the Phase 2.10 grill.
Build order: P0 → P1 → P2 → P3 → P4 → P5 → P6.

#### Phase 3.0 — UI/UX Audit (do this first)
Walkthrough every user-facing page as each role. Output is a backlog
of friction points. Findings are folded into P1–P6 (not a separate
project). The audit focuses on:
1. **Information architecture** — is the right action visible at the
   right time for each role? (no hidden forms, no missing nav links)
2. **Form affordance** — every form has visible labels, clear
   submit/cancel, post-submit feedback (toast/banner), and validation
   errors inline.
3. **State visibility** — WO/Part/ERO/ERR/PR/PO statuses have a
   single shared visual language (badge colour + label).
4. **Mobile readiness** — technician pages (the most field-used) are
   usable on a 375px-wide phone screen.
5. **Role dashboards** — each role has a single "today" page that
   surfaces pending work + quick actions, not a wall of links.

Audit output is a flat list of ~30–50 items grouped by template.
Items get assigned to P1–P6 by proximity.

##### Phase 3.0 — Audit Findings (initial pass)

**workorder_detail.html (607 lines — the worst offender)**
- F1.1 ❗ **Pause reason `<select>` still has `awaiting_parts` and
  `awaiting_vendor` options** at line 132-133 even though Q6 removed
  them from the `WorkOrder.PauseReason` enum. Form-vs-model drift.
  → Fix: P5.2 (drop the two `<option>` tags).
- F1.2 ❗ **Template precedence bug** at line 347:
  `{% if pending_external_repair_requests and request.user.role == 'manager' or pending_external_repair_requests and request.user.is_superuser %}`
  binds `and` before `or`. Currently safe but fragile.
  → Fix: use `{% with %}` to evaluate once, or use `{% elif %}`.
- F1.3 ❗ **Action card has 9+ action groups stacked** with subtle
  visual separation (dashed borders, dividers, colour-tinted headers).
  Hard to scan. No "do now" vs "approval queue" grouping.
  → Fix: split into "My actions" + "Pending decisions" cards. Use
  collapsible `<details>` for low-frequency actions.
- F1.4 ❗ **Manager has two paths to issue parts** (legacy form at
  line 86 + pending approvals at line 268). Operators get confused
  about which to use.
  → Fix: add a clear "Direct issue (override)" label on the legacy
  form. Hide the hybrid approval form for managers — they use
  approve/reject on PENDING lines instead.
- F1.5 ❗ **Pause action is hidden in `<details>`** at line 122.
  Discoverability is poor — techs may not know the pause option
  exists.
  → Fix: render the pause form inline, not in `<details>`. Or
  promote to a primary button.
- F1.6 ❗ **Technician can see BOTH the part-request form AND the
  manager's approve-pending panel** if they scroll. Wrong audience
  for the second card.
  → Fix: gate the manager-pending panel on `perm_approve_part_request`
  (already done) but also add a clear "Manager queue" header.
- F1.7 ❗ **"Part to send out" form** uses raw `<label>` HTML instead
  of form field's `label_tag`. Inconsistent with the other forms.
  → Fix: use `{{ field.label_tag }}{{ field }}`.
- F1.8 **Manager review checklist message** is plain `<div>` text at
  line 213 — easy to miss before the buttons.
  → Fix: use `mms-alert mms-alert--info`.
- F1.9 **No "Last 5 actions" / quick-jump back** to the manager's
  pending-review queue.
  → Fix: add a `<a href="?back=...">← Back to review queue</a>` link.
- F1.10 **PR link at line 94** goes to `?wo=...` — the query param
  is opaque. Some users will be confused.
  → Fix: add a tooltip explaining "Open a PR for parts you can't
  source through the request flow".

**workorder_list.html (36 lines)**
- F2.1 **No row click-to-open** like issue_list has. User has to
  click the WO number.
  → Fix: add `class="mms-row--clickable" onclick=...`.
- F2.2 **"Archive" link in last column** for every row is risky
  (delete-like). Should be in a row-action menu.
  → Fix: move to a per-row menu (`<select>` action dropdown or
  kebab menu).
- F2.3 **Only one filter** (status). No technician / machine /
  priority / emergency / age filter.
  → Fix: add filter row in P6.
- F2.4 **No search**. For sites with 100+ WOs, this matters.
  → Fix: add a search box over the WO number / machine name.

**my_workorders.html (85 lines)**
- F3.1 **"View all work orders" link** at line 81 sends the
  technician to the global WO list, where they may lack permission
  for some actions.
  → Fix: gate on `perm_view_work_orders` or remove the link
  entirely — techs stay in their queue.
- F3.2 **Duplicate "Back to dashboard" link** (in `page_heading`
  + card at line 82).
  → Fix: remove the card-level link.

**issue_list.html (53 lines)**
- F4.1 **No filters** (status / machine / priority / reporter / age).
  → Fix: add filter bar in P6.
- F4.2 **No search** by issue description.
  → Fix: add a search box.
- F4.3 **"Validate" + "Create WO" + "Archive"** all in the action
  column — cluttered when the WO action is the most important.
  → Fix: move to kebab menu, keep only the most-used action visible.

**dashboard.html (104 lines)**
- F5.1 **KPI cards have no priority ordering** — labels vary
  (New issues, Open work orders, Pending review, Open emergency
  WO, PR pending, Stale issues, Overdue PM).
  → Fix: pick the top 3 KPIs per role. The other 3 go on a
  "More" tab.
- F5.2 **"Your queue" card is a dead-zone for the manager** —
  `my_issues` is empty (manager didn't report any).
  → Fix: make this card role-aware. Manager sees pending decisions
  count. Tech sees their queue. Operator sees their issues.
- F5.3 **Stale issue alerts use hard-coded emoji + colour tokens**.
  → Fix: use the design system's `mms-alert` classes everywhere.
- F5.4 **No "Quick action" button** for the most common task per
  role (e.g. "Report issue" for ops, "Approve parts" for managers).
  → Fix: add a hero CTA at the top of the dashboard.

**base.html (sidebar nav)**
- F6.1 ❗ **Mobile bottom nav only exists for operators**. Technicians
  (most field-used) use a tiny hamburger menu.
  → Fix: add a technician bottom nav (Q8 from Phase 2). Add a
  manager/supply-officer bottom nav with key shortcuts.
- F6.2 **No badge/counter for the supply officer's "Procurement"
  section** showing pending PRs. Manager's "Pending review" has
  a badge; supply officer's does not.
  → Fix: add `nav_pr_pending` badge (already added for manager but
  check for supply officer).
- F6.3 **No badge for active emergencies** in the manager's sidebar.
  → Fix: add `nav_emergencies_open` badge.
- F6.4 **"Stock" + "Inventory Lookup" overlap for technicians**.
  Technicians only need a quick SKU lookup, not the full stock
  dashboard.
  → Fix: hide `stock_dashboard` for technicians; keep
  `stock_lookup` only.

**Cross-cutting**
- F7.1 ❗ **Status badges use inconsistent colour mapping** across
  templates. The `wo_status_badge_class` filter is centralised but
  parts/ERO/ERR/PR/PO statuses are not. Different templates use
  `--success` for "approved" in one place and `--info` in another.
  → Fix: create `part_status_badge_class`, `ero_status_badge_class`,
  `err_status_badge_class`, `pr_status_badge_class`,
  `po_status_badge_class` filters. Use them everywhere.
- F7.2 **No "Last viewed" or "Recent items"**. Users re-navigate
  from the dashboard.
  → Fix: add a `recently_viewed` list per user (cookie or DB).
- F7.3 **No loading skeletons** on AJAX calls. The attachment list
  shows "Loading..." then snaps.
  → Fix: add CSS skeleton classes.
- F7.4 **No empty-state illustrations**. Just "No items" text.
  → Fix: add SVG illustrations to `mms-empty`.
- F7.5 **No keyboard shortcuts**. `?` for help, `g d` for dashboard,
  `g w` for work orders. Speeds up power users.
  → Fix: add a JS keyboard handler in `base.html`.
- F7.6 **Toast notifications limited to Django messages**. No
  in-page success indicator (e.g. "✓ Saved" badge).
  → Fix: add a simple toast system.
- F7.7 **No dark mode**.
  → Fix: add `prefers-color-scheme: dark` CSS. Lower priority.
- F7.8 **Form validation errors are sometimes inline, sometimes as
  a Django message banner**.
  → Fix: pick one pattern (inline + summary banner).
- F7.9 **Pause form `<details>` is invisible until clicked** (also
  F1.5).
  → Fix: see F1.5.
- F7.10 **No way to filter the audit log** by user / entity type
  / date.
  → Fix: add filter form on `audit_log.html`.

##### Triage & assignment

Of the ~30 friction points:
- **P0.1 — Critical bugs** (4 items, ❗ marked): F1.1, F1.2, F1.3,
  F1.4, F1.5, F1.6, F1.7, F6.1, F7.1. All Phase 3 sub-phases
  should sweep these.
- **P6 polish** (10+ items): F1.8, F1.9, F1.10, F2.x, F3.x, F4.x,
  F5.x, F6.2, F6.3, F6.4, F7.2–F7.10.
- **Future phase**: F7.5, F7.7, F7.10.

#### Phase 3.1 — UC-09 Inventory & Procurement Automation (highest)

**Design principle (locked P1.5 grill):** "Manager is the financial
gatekeeper." The technician's request creates a PENDING line and
auto-creates a PR for the shortage, but the line stays PENDING until
the manager approves. **No automatic stock deduction.** The manager
decides `approved_qty`; the system then deducts stock for the
approved amount. Auto-PRs do not auto-update when the manager
changes `approved_qty` (PR is a separate procurement document).

- **P1.1** Add 4 fields to `PartIssueLine` (migration):
  - `requested_qty` — set on creation (mirror of `quantity`)
  - `approved_qty` — set by manager on approve/edit; 0 until then
  - `issued_qty` — set on approval = min(approved_qty, available)
  - `shortage_qty` — computed = max(0, requested - approved)

  Data backfill: existing APPROVED lines get
  `approved_qty = issued_qty = quantity`, `shortage_qty = 0`.
  PENDING lines get `approved_qty = issued_qty = 0`, `shortage_qty = 0`.

- **P1.2** Rewrite `inventory.services.request_part_on_wo`:
  - Always creates a PENDING `PartIssueLine` with `requested_qty = quantity`
  - Computes `available = Inventory.quantity_available` for the part/site
  - Computes `shortage = max(0, requested - available)`
  - If `shortage > 0`: call `_create_procurement_for_shortage` for `shortage`
  - **Idempotent**: if a PENDING line exists for the same WO+part,
    return the existing line. If an auto-PR exists for the same
    WO+part, do not duplicate. (Manager edits PR manually if needed.)
  - Emergency WO path unchanged: auto-approve, deduct stock, audit flag.

- **P1.3** Update `_create_procurement_for_shortage`:
  - Accept the `shortage` quantity (not the requested quantity)
  - Find existing PENDING PR for `(work_order, part)`; if found,
    skip creation. If found but PR is for a different qty, leave
    the original alone (manager can edit).
  - Create new PR with note `"Auto-created from WO-XXXX part short
    (requested {n}, available {a}, shortage {s})"`.

- **P1.4** Update `approve_part_request` and `edit_part_request_qty`:
  - On approve: `approved_qty = new_qty` (or original `quantity`),
    `issued_qty = min(approved_qty, available_at_approval_time)`,
    `shortage_qty = max(0, requested - approved)`,
    deduct `issued_qty` from inventory, status = APPROVED.
  - On edit (before approval): change `quantity` and update
    `shortage` recalculation. No stock movement.
  - On reject: status = REJECTED, `approved_qty = 0`,
    `issued_qty = 0`, `shortage_qty = 0`. PR stays (procurement
    decision is separate from WO issue decision).

- **P1.5** Show **last supplier price** on the part-request form.
  - In `work_order_request_part` view, build
    `last_prices = {part_id: (unit_cost, supplier_name, date)}`
    from the most recent `StockMovement(movement_type=STOCK_IN,
    unit_cost__gt=0)` per part.
  - Template: under the Part select, show
    `"Last received: 12.50 / unit (Supplier X, 2026-05-14)"`
    updated via JS on select change.

- **P1.6** Manager direct issue path (`work_order_issue_part` /
  `issue_part_to_work_order`) **bypasses** auto-PR. Manager has
  full visibility; if there's a shortage, the system should
  suggest creating a PR but not auto-create it.

- **P1.7** New tests in `PartRequestWorkflowTests`:
  - `test_request_part_partial_stock_creates_pending_line_and_pr`
  - `test_request_part_zero_stock_creates_pending_line_and_pr`
  - `test_request_part_full_stock_creates_pending_line_no_pr`
  - `test_request_part_is_idempotent_no_duplicate_line_or_pr`
  - `test_request_part_appends_to_existing_pr_for_same_wo_part`
  - `test_manager_approve_with_edited_qty_deducts_correctly`
  - `test_manager_reject_keeps_pr_alone`
  - `test_last_supplier_price_in_form_context`
  - `test_manager_direct_issue_does_not_auto_create_pr`
  - `test_part_issue_line_new_fields_present_and_backfilled`

#### Phase 3.2 — UC-20 Vendor Cost Flow (high)
- **P2.1** Extend `repair_manager_accept` view/service: capture
  vendor invoice number + vendor cost on ERO acceptance (form
  fields). Make them mandatory on the accept form per SRS UC-20
  ("every issued part must have cost + supplier + invoice").
- **P2.2** On `ExternalRepairOrder.status = CLOSED`, push the cost
  into `WorkOrderCost.vendor_cost` and roll up into
  `MachineCostReport`. Use the same pattern as
  `WorkOrderCost.parts_cost` already does.
- **P2.3** Update `kpi_dashboard` and `machine_cost_report` so that
  ERO vendor cost appears in the "Machine Cost" totals.
- **P2.4** Add `ExternalRepairAcceptanceTests` for cost flow.

#### Phase 3.3 — Emergency Escalation Paths (medium)
- **P3.1** Add `is_emergency` boolean to `MaintenanceIssue` (default
  False). Migration backfill (all existing issues stay False).
- **P3.2** Add an `is_emergency` checkbox on the operator
  `issue_create` form, gated to OPERATOR + SUPERVISOR. When checked,
  the issue lands with `priority = CRITICAL` and a `EMERGENCY`
  badge in the issue list.
- **P3.3** On `issue_validate`, add a "Escalate to emergency"
  button visible to SUPERVISOR + MANAGER + SUPER_ADMIN that flips
  `is_emergency=True` and `priority=CRITICAL` on a normal issue.
- **P3.4** On `work_order_create_from_issue`, when the issue is
  emergency, the resulting WO is auto-created with
  `is_emergency=True`. The legacy `EmergencyWOForm` keeps working
  as the no-issue-required path.
- **P3.5** Update `notify_emergency_issue_reported` /
  `notify_emergency_work_order_created` (add if missing) to notify
  the on-call technician + manager when an emergency is created.
- **P3.6** Add `EmergencyEscalationTests` covering operator-report
  and supervisor-escalate paths.

#### Phase 3.4 — Reports / KPIs Role Filter (medium)
- **P4.1** Add SUPERVISOR to the `@role_required` decorator on
  `reports_view` and `kpi_dashboard`.
- **P4.2** Extract each report section into a partial or include.
  Add a `REPORT_SECTIONS` registry mapping section name → (allowed
  roles, view fn, template partial). Filter the rendered sections
  per `request.user.role` at view time.
- **P4.3** Section matrix:
  | Section | Admin | Manager | Supervisor | Supply |
  |---------|-------|---------|------------|--------|
  | WO performance / Tech / MTTR-MTBF | ✓ | ✓ | ✓ | ✗ |
  | Machine cost | ✓ | ✓ | ✓ | ✓ (read-only) |
  | Spare parts consumption | ✓ | ✓ | ✓ | ✓ |
  | Supplier / PR / PO / Dead stock | ✓ | ✓ | ✗ | ✓ |
- **P4.4** Update `/reports/` and `/kpis/` templates to render only
  the allowed sections for the current user.
- **P4.5** Add `ReportsAccessTests` for the matrix.

#### Phase 3.5 — Pause Reason Enum Cleanup (low)
- **P5.1** Remove `AWAITING_PARTS` and `AWAITING_VENDOR` from
  `WorkOrder.PauseReason` TextChoices. Migration to drop the
  values; no data backfill needed (no rows should exist with these
  values if the form validation was already in place).
- **P5.2** Update `WorkOrderPauseForm` to drop the two choices.
  Update `pause_other_in_progress` signature / callers.
- **P5.3** Add a guard: if a pause transition is attempted with a
  reason of `AWAITING_*` (shouldn't happen post-cleanup), suggest
  transitioning the WO to `WAITING_FOR_PARTS` / `WAITING_FOR_VENDOR`
  status instead.
- **P5.4** Update `WorkOrderPauseReasonTests` to assert the new
  enum and the redirect-to-status behavior.

#### Phase 3.6 — UI Polish / Dashboard (lowest)
- **P6.1** Add a manager dashboard with action counters: pending
  part requests, pending external-repair requests, paused WOs,
  open emergencies, overdue WOs.
- **P6.2** Add quick-action shortcuts on the manager dashboard:
  Approve Parts, Review EROs, Resume Paused, Close WOs.
- **P6.3** Polish the technician dashboard counterpart with
  shortcut to `/work-orders/my/` and the last 5 closed WOs.

#### Phase 3.7 — Manager UX Enhancements (post-Phase 3.6 polish)

User-driven refinements surfacing in the manager walkthrough. Same Phase 3
scope (UI/UX), no role-shape changes.

**Escalation in-page confirmation (Q2 grill):**
The "Escalate to emergency" button previously used a native browser
`confirm()` dialog. Replaced with an in-page confirmation card that
reveals on click: lists 4 consequences (priority → CRITICAL, paging
on-call tech + manager, top-of-queue, WO inherits `is_emergency=True`),
a "Yes, escalate to emergency" red button + "Cancel" ghost button.
No native popup, no page navigation. The card uses a JS toggle on the
`hidden` attribute and a `scrollIntoView({behavior:'smooth'})` to keep
the user's scroll position stable. The original trigger button
disables while the card is open to prevent double-submits.

**ERO supply-officer notification chain (Q1 grill):**
Three transitions in the ERO lifecycle, three notification firings.
| Transition | Notify | Kind | Link |
|---|---|---|---|
| PENDING ERR created (tech) | → managers | REPAIR_REQUESTED | WO detail (existing) |
| DRAFT ERO created (manager approves) | → **supply officers** | REPAIR_DRAFT | repair officer form |
| SENT_TO_VENDOR (supply officer) | → **managers** | REPAIR_SENT | repair officer form |
| RETURNED (supply officer) | → managers | REPAIR_RETURNED | manager accept form (existing) |
| CLOSED (manager) | existing audit only | — | — |

The supply officer no longer has to poll `/repairs/` to discover new
work. The manager gets visibility that the part actually left the
facility. Pattern mirrors PR → PO: each step notifies the next
responsible role so ownership transfers cleanly.

**Manager navigation (Q3 grill):**
Three new sidebar items added under INSIGHTS, gated to users with
`close_or_review_wo` capability (manager + super admin):
- **Machine cost report** → `/reports/machine-costs/`
- **Pending repairs** → `/repairs/?status=returned` (badge: count of
  RETURNED EROs awaiting manager acceptance)
- **Pending approvals** → `/work-orders/?status=pending_review`
  (badge: count of WOs awaiting manager review/close)

Dashboard "Shortcuts" card mirrors the same three items with badge
counts in parentheses. Dashboard "Quick actions — manager queue" card
adds a "X returned repair(s) awaiting acceptance" link when
`nav_ero_returned > 0`. The "All caught up" empty-state condition
also accounts for the new counter.

**Supporting changes:**
- `work_order_list` view now accepts `?overdue=1` and
  `?has_pending_part=1` query params for filtered lists
- `repair_list` view now accepts `?status=<choice>` query param
- Context processor (`maintenance/context_processors.py`) gains
  `nav_ero_returned`, `nav_ero_draft`, `nav_wo_overdue` counters
- Two new `Notification.Kind` enum values: `REPAIR_DRAFT` and
  `REPAIR_SENT`

### Final Manager-Role Assessment (Phase 2.10 close-out)

SRS alignment: **~95–98%**. Factory operational readiness: **High**.
Role separation: **Strong** (operator → supervisor → manager → supply
officer, each with non-overlapping authority). Auditability: **Strong**
(WorkOrderStateLog + PartIssueLine.status trail + WorkOrderAssignmentHistory).
Inventory control: **Strong** (post-hybrid-approval). Procurement control:
**Strong** (auto-PR triggers + manager approval on parts).

**Role definition is finalized.** Remaining work is workflow refinement
(Phase 3 P1–P3), reporting polish (P4), and UI automation (P6) — not
role-shape changes.

#### Phase 4 candidates (deferred)
- **Supplier performance dashboard** — on-time delivery %, repair
  turnaround time, average vendor cost (post-Phase 3 once supplier
  data has weight).
- Role-based login redirect (Q7 deferred from Phase 2)
- Dedicated mobile bottom nav for technicians (Q8 deferred from Phase 2)
- Auto PM spawn (Celery beat)
- Reservation workflow (quantity_reserved field already in schema)
- SLA engine
- Full offline PWA
- ERP integration
- IoT / runtime meters
- Barcode printer integration
- Email notifications (hooks in place, not active)
- Operator consumption approval workflow (supervisor pre-authorization)
- Consumption quotas / daily limits per operator
- Real-time push notifications for status changes