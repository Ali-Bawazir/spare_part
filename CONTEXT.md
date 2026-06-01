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

### Deferred to Phase 2
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