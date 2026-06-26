# System End-to-End Scenarios

Five realistic, complete scenarios covering the full maintenance & spare parts lifecycle. Each scenario walks through the **complete data flow** with actors, actions, system responses, and edge cases.

---

## Actors (consistent across all scenarios)

| Username | Role | What they do |
|---|---|---|
| `operator` | Operator | Reports issues, logs consumables, uses tools |
| `supervisor` | Supervisor | Validates issues, oversight, escalation |
| `technician` | Technician | Executes work orders, requests parts |
| `manager` | Manager | Owns PMs, reviews WOs, approves procurement, closes WOs |
| `procurement` | Procurement | Creates POs, receives deliveries, manages suppliers |
| `super_admin` | Super Admin | Full access, escalation target |

---

## Scenario A — Operator Reports an Issue → Maintenance Cycle

The most common flow. Operator sees a problem → it's fixed.

### Setup

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import MaintenanceIssue, WorkOrder
print(f'Open issues: {MaintenanceIssue.objects.filter(status=\"new\").count()}')
print(f'Open WOs: {WorkOrder.objects.exclude(lifecycle_status=\"closed\").count()}')
"
```

### Step-by-step

| # | Actor | Action | System response | Data created |
|---|---|---|---|---|
| A1 | operator | Sees machine malfunction, opens `http://localhost:8000/issues/new/?machine=1` | Form renders | — |
| A2 | operator | Fills description "Belt slipping under load", marks **emergency** | — | — |
| A3 | operator | Clicks **Report Issue** | `issue_create` view saves row | `MaintenanceIssue(status=NEW, is_emergency=True, priority=CRITICAL)` |
| A4 | system | Auto-fires notification | `notify_new_issue()` | `Notification(recipient=manager+supervisor+super, kind=issue_new)` |
| A5 | manager | Sees notification, opens `/issues/` | Issue list shows 1 new | — |
| A6 | manager | Clicks issue → **Validate** | `issue_validate` view | `MaintenanceIssue(status=VALIDATED, validated_by=manager, priority=HIGH)` |
| A7 | manager | Clicks **Create WO from this issue** | `work_order_create_from_issue` view | `WorkOrder(category=BREAKDOWN, lifecycle=ASSIGNED, issue=this_issue)` |
| A8 | manager | **Assigns technician** via Manager actions | `work_order_assign` view | `WorkOrder.assigned_technician = technician` |
| A9 | system | Auto-fires notification | `notify_wo_assigned` | `Notification(recipient=technician)` |
| A10 | technician | Sees notification, opens `/work-orders/my/` | WO appears in queue | — |
| A11 | technician | Opens WO, requests part: types "BELT-100" qty=1 | `work_order_request_part` view | `PartIssueLine(status=PENDING, requested_by=technician)` |
| A12 | manager | Sees "Part request pending" on WO | — | — |
| A13 | manager | Approves part request | `work_order_approve_part` view | `PartIssueLine(status=APPROVED, approved_by=manager, approved_qty=1)` |
| A14 | system | Auto-fires `notify_part_approved` | — | `InventoryReservation(status=ACTIVE, part=BELT-100, work_order=wo, qty=1)` |
| A15 | manager | **Warehouse issues** the part | `work_order_warehouse_issue` | `PartIssueLine(status=ISSUED, issued_by=manager)` + `StockMovement(OUT)` |
| A16 | system | Auto-fires `notify_part_received` to WO | — | `InventoryReservation(status=RELEASED, released_at=now)` |
| A17 | technician | Returns to WO, clicks **Start** | `work_order_start` | `WorkOrder(lifecycle=IN_PROGRESS, labor_started_at=now)` |
| A18 | technician | Does the work, fills root cause + action taken | — | — |
| A19 | technician | Clicks **Submit for Review** | `technician_submit_for_review` | `WorkOrder(lifecycle=PENDING_REVIEW, labor_stopped_at=now)` + `notify_wo_pending_review` |
| A20 | manager | Opens WO, reviews, clicks **Approve & Close** | `manager_close_work_order` | `WorkOrder(lifecycle=CLOSED)` + `notify_wo_closed` |

### End state
- Issue: status=CONVERTED (when WO was created)
- WO: lifecycle=CLOSED, total_cost includes the belt part
- PartIssueLine: 1 line, status=ISSUED
- StockMovement: 1 OUT entry for BELT-100
- InventoryReservation: 1 row, status=RELEASED
- Notifications fired: 5 (issue_new, wo_assigned, part_approved, part_received, wo_pending_review, wo_closed)

### What can go wrong

| Situation | System behavior |
|---|---|
| Operator marks emergency | Issue.priority=CRITICAL + WO.is_emergency=True if created |
| Manager rejects part request | PartIssueLine.status=REJECTED, technician sees and re-requests |
| Part not in stock | `notify_part_shortage_reported` fires, PartShortageReport created |
| Manager over-issues more than approved | Validation blocks, error message shown |
| Technician submits incomplete checklist | Allowed (manager decides) |

---

## Scenario B — Procurement Cycle

A part is low → manager requests replenishment → PO created → delivery received → stock updated.

### Step-by-step

| # | Actor | Action | System response | Data created |
|---|---|---|---|---|
| B1 | manager | Opens `/stock/1/` for BELT-100 | Sees `quantity_on_hand=2`, `min_stock_level=5` | — |
| B2 | manager | Notices stock low, opens `/procurement/new/?part=1` | PR form renders | — |
| B3 | manager | Fills qty=10, urgency=normal, notes="Routine restock" | — | — |
| B4 | manager | Clicks **Create PR** | `purchase_create` view | `PurchaseRequest(status=PENDING, qty=10, created_by=manager)` |
| B5 | system | Auto-fires `notify_procurement_request` | — | `Notification(recipient=procurement+manager, kind=procurement)` |
| B6 | procurement | Sees notification, opens `/procurement/` | Sees 1 pending PR | — |
| B7 | procurement | Clicks PR → **Create PO** | `purchase_order_create_from_pr` view | `PurchaseOrder(status=DRAFT, supplier=Local Parts Co)` |
| B8 | procurement | Adds PO line: BELT-100 qty=10 unit_price=50, saves | — | `PurchaseOrderItem(qty=10, unit_price=50, received_qty=0)` |
| B9 | procurement | Marks PO as **Sent** | `purchase_order_send` | `PurchaseOrder(status=SENT, sent_at=now)` |
| B10 | (time passes — supplier delivers) | | | |
| B11 | procurement | Opens PO, clicks **Receive** | `purchase_order_receive` view | `PurchaseOrderItem.received_qty += arrived` + `StockMovement(IN)` |
| B12 | system | If `received_qty >= ordered_qty`: calls `auto_fulfill_wo_lines_from_po()` (when `PO_AUTO_ISSUE=True`) | Auto-issues to pending WO lines | `PartIssueLine.status=ISSUED` for matching pending lines |
| B13 | system | Fires `notify_po_received_summary` | — | `Notification(recipient=manager+procurement)` |
| B14 | procurement | Closes PO (full receive or close-short) | `purchase_order_close` | `PurchaseOrder(status=CLOSED)` |

### End state
- PR: status=FULFILLED
- PO: status=CLOSED
- PurchaseOrderItem: received_qty=10
- StockMovement: 1 IN for BELT-100 qty=10
- PartIssueLine: pending WO lines auto-issued (if any)
- Notifications: 4 fired

### What can go wrong

| Situation | System behavior |
|---|---|
| Supplier short-shipped (delivered 8 of 10) | `backordered_qty = 2`, PO stays PARTIAL_RECEIVED |
| Damaged goods on receipt | Mark `damaged=N` → quarantined in `Inventory.quantity_quarantine` |
| Manager requested 10 but PO has qty=15 | PO line keeps ordered_qty=15, separate from PR |
| PO_AUTO_ISSUE=False (default in some envs) | Stock updated, no auto-fulfillment of WO lines |
| Partial receive then close-short | Manager can close early; unreceived qty stays as backorder |

---

## Scenario C — Vendor Repair (External Repair Order)

A part can't be fixed in-house → sent to vendor → returned → manager accepts.

### Step-by-step

| # | Actor | Action | System response | Data created |
|---|---|---|---|---|
| C1 | technician | Working on WO-5, realizes "Servo drive needs vendor recalibration" | — | — |
| C2 | technician | Opens WO-5, scrolls to External Repairs, clicks **Request External Repair** | `work_order_request_external_repair` view | `ExternalRepairRequest(status=PENDING, requested_by=technician, part_description="Servo drive S7-300")` |
| C3 | system | Auto-fires `notify_repair_requested` | — | `Notification(recipient=manager+procurement)` |
| C4 | manager | Reviews request, opens WO, clicks **Approve** on the request | `work_order_decide_external_repair` (action=approve) | `ExternalRepairRequest(status=APPROVED, reviewed_by=manager)` |
| C5 | system | Auto-creates ERO | — | `ExternalRepairOrder(status=DRAFT, title="Servo drive recalibration", work_order=wo, created_by=manager)` |
| C6 | system | Fires `notify_repair_draft` | — | `Notification(recipient=procurement+manager)` |
| C7 | procurement | Opens ERO at `/repairs/<id>/officer/`, fills vendor_name="Calibration Co", estimated_cost=300, status=SENT | `repair_officer` view | `ExternalRepairOrder(status=SENT_TO_VENDOR, vendor_name=..., sent_at=now)` |
| C8 | system | Fires `notify_repair_sent` | — | `Notification(recipient=manager)` |
| C9 | (vendor completes work, returns part) | | | |
| C10 | procurement | Updates ERO: status=RETURNED, actual_cost=320 | `repair_officer` view | `ExternalRepairOrder(status=RETURNED, actual_cost=320, returned_at=now)` |
| C11 | system | Fires `notify_repair_returned` | — | `Notification(recipient=manager+supervisor+super)` |
| C12 | manager | Opens WO, sees "Pending repairs" badge, clicks **Accept** | `repair_manager_accept` view | `ExternalRepairOrder(status=ACCEPTED, invoice_ref=..., closed_at=now)` + creates `CostTransaction(vendor_repair, 320)` |
| C13 | system | WO total cost increases by 320 SAR | WO.cost_record updates | `CostTransaction(amount=320, category=VENDOR_REPAIR)` |
| C14 | manager | Manager closes WO | `manager_close_work_order` | `WorkOrder(lifecycle=CLOSED)` |

### End state
- ExternalRepairRequest: status=APPROVED
- ExternalRepairOrder: status=ACCEPTED, actual_cost=320
- WO total cost includes 320 SAR vendor repair
- Notifications: 6 fired
- WO Blocker (Phase 7) auto-resolved on accept (if was VENDOR_REPAIR kind)

### What can go wrong

| Situation | System behavior |
|---|---|
| Manager rejects the ERO request | ExternalRepairRequest.status=REJECTED, no ERO created |
| Vendor costs more than estimated | Manager can update actual_cost on accept; cost is captured |
| Manager doesn't add invoice_ref | Form rejects submit (UC-20 requires invoice + cost) |
| WO closed before ERO accepted | Shouldn't happen; UI shows "pending repairs" warning |

---

## Scenario D — Shortage Flow (Part Not in Stock)

A WO needs a part that's not in stock → shortage report → PR created → PO → received → WO continues.

### Step-by-step

| # | Actor | Action | System response | Data created |
|---|---|---|---|---|
| D1 | technician | Working on WO-7, needs HYDRAULIC-SEAL qty=2 | — | — |
| D2 | technician | Requests part, sees in real-time availability = 0 | — | — |
| D3 | technician | Clicks **Report Shortage** on the part request line | `work_order_request_shortage` view | `PartShortageReport(status=PENDING_REVIEW, qty_requested=2, qty_available=0, shortage=2)` |
| D4 | system | Fires `notify_part_shortage_reported` | — | `Notification(recipient=manager+supervisor+super+technician)` |
| D5 | manager | Opens WO, sees "Pending Shortage" panel, clicks **Decide** | `work_order_decide_shortage` view | Manager chooses: PR or Block |
| D6 | manager | Picks "Create PR" with qty=5, urgency=high | — | `PurchaseRequest(status=PENDING, qty=5, work_order=wo, part=seal, urgency=HIGH)` + `PartShortageReport(status=IN_FULFILLMENT, manager_decision=CREATE_PR)` |
| D7 | system | Auto-fires `notify_procurement_request` | — | `Notification(recipient=procurement+manager)` |
| D8 | (Scenario B runs — PR → PO → receive) | | | |
| D9 | system | When PO is received and `PO_AUTO_ISSUE=True`, auto-issues to WO line | `auto_fulfill_wo_lines_from_po()` | `PartIssueLine.status=ISSUED` |
| D10 | system | Auto-resolves shortage: status=FULFILLED | — | `PartShortageReport(status=FULFILLED, fulfilled_at=now)` + `notify_shortage_followup` |
| D11 | manager | Sees "Shortage fulfilled", WO continues | WO Blocker (SHORTAGE) auto-resolved | — |
| D12 | technician | Now has the part, completes work | — | — |
| D13 | technician | Submits for review | `technician_submit_for_review` | WO → PENDING_REVIEW |
| D14 | manager | Approves & closes | — | WO → CLOSED |

### End state
- PartShortageReport: status=FULFILLED
- WO: lifecycle=CLOSED, total_cost includes the part
- WO Blocker: SHORTAGE blocker auto-resolved
- Notifications: 8+ fired across the full flow

### What can go wrong

| Situation | System behavior |
|---|---|
| Manager blocks the WO instead of creating PR | PartShortageReport.status=BLOCKED, WO Blocker created |
| Manager edits qty in PR | Both PR and shortage updated; new shortage qty is reflected |
| PO not received in 7 days | `nav_shortage_overdue` counter increments |
| Shortage fulfilled but WO already closed | No-op, no error |

---

## Scenario E — Cross-Functional: Full Issue → WO → Shortage → PR → PO → Receive → Issue → Close

The grand tour. Touches every major system.

### Story

Production line stops. Operator reports. Manager creates WO. Technician needs a part. Part is out of stock. Shortage raised. PR → PO → receive. WO continues. Closed.

### Step-by-step (condensed — same as A+B+D combined)

```
0. Setup: BELT-100 quantity_on_hand=0 (force shortage)
1. operator → /issues/new/ → fills "Line stop" → save
2. system creates MaintenanceIssue(status=NEW, priority=CRITICAL)
3. manager → /issues/ → validate → priority=HIGH → create WO
4. system creates WorkOrder(category=BREAKDOWN, lifecycle=ASSIGNED, issue=this)
5. manager → assign technician
6. technician → /work-orders/my/ → opens WO → requests BELT-100 qty=2
7. system creates PartIssueLine(status=PENDING)
8. technician → sees availability=0 → clicks "Report Shortage"
9. system creates PartShortageReport(status=PENDING_REVIEW, shortage=2)
10. manager → /shortage/dashboard/ → decides: Create PR qty=5
11. system creates PurchaseRequest(status=PENDING) + PartShortageReport(status=IN_FULFILLMENT)
12. procurement → /procurement/ → create PO from PR → status=SENT
13. (supplier delivers)
14. procurement → /procurement/purchase-orders/ → receive
15. system creates StockMovement(IN) + auto-fulfills WO line (PO_AUTO_ISSUE)
16. PartIssueLine.status=ISSUED
17. PartShortageReport.status=FULFILLED
18. WO Blocker (SHORTAGE) auto-resolved
19. technician → /work-orders/<id>/ → belt now available → install → fill notes
20. technician → submit for review
21. WO → PENDING_REVIEW
22. manager → review → approve & close
23. WO → CLOSED
```

### Final data state

```
MaintenanceIssue:        1 row (CONVERTED)
WorkOrder:               1 row (CLOSED, total_cost includes belt)
PartIssueLine:           1 row (ISSUED)
InventoryReservation:    1 row (RELEASED)
PartShortageReport:      1 row (FULFILLED)
PurchaseRequest:         1 row (FULFILLED)
PurchaseOrder:           1 row (CLOSED)
PurchaseOrderItem:       1 row (received_qty=5)
StockMovement:           1 row (IN, qty=5, BELT-100)
Notifications fired:     10+
```

### What can go wrong

| Situation | System behavior |
|---|---|
| Operator's issue is duplicate | Manager sees the existing one, validates it instead |
| Supplier delivers wrong part | Procurement marks as `damaged`, quarantines, opens dispute PR |
| Multiple WOs need same part | Auto-fulfill uses FIFO (earliest WO first) |
| PO is closed-short (4 of 5 delivered) | Shortage remains PARTIAL, backorder tracked |
| WO already closed when shortage fulfilled | Shortage.status=FULFILLED but no auto-fulfillment on closed WO |

---

## Scenario F — Operator Self-Service Consumable

Operators can log low-value consumable usage without a WO. (Operator consumable flow from CONTEXT.md.)

### Step-by-step

| # | Actor | Action | System response | Data created |
|---|---|---|---|---|
| F1 | operator | Opens `/consumables/` | Sees list of approved consumables (`is_consumable=True AND allow_operator_consumption=True`) | — |
| F2 | operator | Clicks GREASE-5L, fills qty=1, machine=Line A — Press 1, note="Greased slides" | `consumables_view` POST | `ConsumableAssignment(part=GREASE, consumed_by=operator, issued_by=operator, source=SELF_SERVICE, approved=True, site=Main Factory, machine=Line A, qty=1, note=...)` + `StockMovement(OUT, qty=1, source=consumable_assignment)` — both in single transaction |
| F3 | system | Stock decreases | `SparePart.quantity_on_hand -= 1` | `StockMovement(OUT, qty=1)` |
| F4 | operator | Sees "Logged 1 × GREASE-5L on Line A — Press 1" success message | — | — |

### Phase 2+ (deferred)
- Supervisor approval queue (consumable.status=SUBMITTED, requires review)
- Quotas per operator per day
- Shift-based authorization

---

## Notifications Generated (cumulative)

| Scenario | # fired | Kinds |
|---|---|---|
| A (Issue → WO cycle) | ~6 | issue_new, wo_assigned, part_approved, part_received, wo_review, wo_closed |
| B (Procurement) | ~4 | procurement, po_received_summary, (auto-issues any pending WO lines) |
| C (Vendor repair) | ~6 | repair_requested, repair_draft, repair_sent, repair_returned, (wo_blocker_resolved on accept) |
| D (Shortage) | ~3 | part_shortage, shortage_followup, wo_blocker_resolved |
| E (Full cycle) | ~12+ | all of the above |
| F (Consumable) | 0 | no notification needed (self-service) |

---

## Data Flow Diagrams (textual)

### Scenario E (full cycle) — object graph

```
MaintenanceIssue
    └─ 1:1 ──> WorkOrder
                  ├─ 1:N ──> PartIssueLine
                  │             └─ consumed by: PartRequest → PartShortageReport
                  │                                          └─ 1:1 ──> PurchaseRequest
                  │                                                                └─ 1:1 ──> PurchaseOrder
                  │                                                                                       └─ 1:N ──> PurchaseOrderItem
                  ├─ 1:1 ──> PMExecution (if PM WO)
                  ├─ 1:N ──> ExternalRepairOrder (if vendor repair)
                  ├─ 1:N ──> WorkOrderBlocker (if blocked)
                  ├─ 1:N ──> WorkOrderStateLog (state transitions)
                  └─ 1:N ──> CostTransaction (cost ledger)
```

### Cost aggregation

```
WorkOrderCost
  ├─ material_cost:    sum(PartIssueLine.issued_qty × unit_cost)
  ├─ vendor_repair:    sum(ExternalRepairOrder.actual_cost)
  ├─ consumables_cost: sum(ConsumableAssignment.qty × unit_cost) for this WO
  ├─ additional:       sum(CostTransaction.amount WHERE category=ADJUSTMENT)
  └─ total_cost:       sum of all above (parts_cost + vendor_cost + consumables_cost + additional_cost)
       ↓
MachineCost (rollup to Machine → Component)
```

---

## State Machines

### WorkOrder.LifecycleStatus
```
DRAFT → ASSIGNED → IN_PROGRESS → PENDING_REVIEW → CLOSED  (terminal)
                  ↘ IN_PROGRESS (after reject)  ↗
                  ↘ CANCELLED (terminal)
```

### PartIssueLine.Status
```
PENDING → APPROVED → ALLOCATED → ISSUED  (terminal happy path)
   ↓         ↓           ↓
  REJECTED  CANCELLED   CANCELLED
```

### ExternalRepairOrder.Status
```
DRAFT → SENT_TO_VENDOR → RETURNED → ACCEPTED  (terminal)
                              ↓ (if rejected)
                           REJECTED
```

### PurchaseOrder.Status
```
DRAFT → SENT → PARTIAL_RECEIVED → CLOSED  (terminal)
   ↓          ↓
CANCELLED  PARTIAL_RECEIVED (loops until closed)
```

### PartShortageReport.Status
```
PENDING_REVIEW → IN_FULFILLMENT → FULFILLED  (terminal)
       ↓                ↓
    BLOCKED          CANCELLED
```

### PMExecution.Status
```
SUBMITTED → APPROVED  (terminal)
   ↓         ↓
 REJECTED → SUBMITTED  (loop until APPROVED)
   ↓
 MISSED  (auto-created by cron when past grace)
```

---

## Where Each Scenario Touches the Codebase

| Scenario | Main files |
|---|---|
| A (Issue → WO → close) | `maintenance/views.py:issue_create, issue_validate, work_order_create_from_issue, work_order_assign, work_order_request_part, work_order_approve_part, work_order_warehouse_issue, work_order_start, work_order_submit, work_order_close` |
| B (Procurement) | `procurement/views.py:purchase_create, purchase_order_create_from_pr, purchase_order_send, purchase_order_receive, purchase_order_close` + `inventory/services.py:auto_fulfill_wo_lines_from_po` |
| C (Vendor repair) | `maintenance/views.py:work_order_request_external_repair, work_order_decide_external_repair, repair_officer, repair_manager_accept` |
| D (Shortage) | `maintenance/views.py:work_order_request_shortage, work_order_decide_shortage` + `procurement/views.py:...` |
| E (Full cycle) | All of the above + `inventory/services.py:auto_fulfill_wo_lines_from_po` + `services_blocker.py` (blocker resolution) |
| F (Consumables) | `maintenance/views.py:consumables_view` + `inventory/services.py:consumable_use` |

---

## Quick Reset Between Scenarios

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import *
from inventory.models import *
from procurement.models import *
print('Cleaning demo data...')
# Don't delete machines, parts, users, PMTemplate
# Wipe transactional data to start fresh
MaintenanceIssue.objects.all().delete()
WorkOrder.objects.all().delete()
PartIssueLine.objects.all().delete()
InventoryReservation.objects.all().delete()
PartShortageReport.objects.all().delete()
ExternalRepairRequest.objects.all().delete()
ExternalRepairOrder.objects.all().delete()
PurchaseRequest.objects.all().delete()
PurchaseOrder.objects.all().delete()
StockMovement.objects.all().delete()
Notification.objects.all().delete()
print('Reset complete.')
"
```

This wipes all transactional data but keeps master data (machines, parts, users, PM templates). Run before each scenario to start clean.