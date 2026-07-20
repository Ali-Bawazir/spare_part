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

**WO Blocker**:
A visibility-layer record of what a Work Order is currently waiting on. Each blocker has a kind (PART, SHORTAGE, VENDOR_REPAIR, OPERATIONAL), a status (OPEN, RESOLVED, CANCELLED), a generic external reference to the underlying domain entity (PartIssueLine, PartShortageReport, ExternalRepairRequest, or ExternalRepairOrder), and lifecycle timestamps. Blockers derive from the underlying domain entities and do not own business state; the PartIssueLine, PartShortageReport, ExternalRepairRequest, and ExternalRepairOrder remain the authoritative sources of truth. WorkOrder status is derived from open blockers.
_Avoid_: Wait, Hold, Obstacle

**Allocated Quantity**:
The third stage of the Part Issue pipeline. Represents the number of units reserved in inventory for a specific PartIssueLine after manager approval, before physical warehouse issue. Computed as `min(approved_qty, inventory.quantity_available - other_lines_reserved)`. Tracked on `PartIssueLine.allocated_qty`. The full pipeline is: Requested → Approved → Allocated → Issued. A line where `allocated_qty < approved_qty` has a shortage that triggers a PartShortageReport.
_Avoid_: Reserved Quantity, Blocked Quantity

**InventoryReservation**:
A dedicated record representing the soft claim of N units of a SparePart by a Work Order, after manager approval and before warehouse issue. Each reservation has a part, work_order, quantity, status (ACTIVE, RELEASED, CANCELLED), source_line (the originating PartIssueLine), and lifecycle timestamps. The aggregate `Inventory.quantity_reserved` is a derived cached value computed as `sum(InventoryReservation.quantity where status=ACTIVE)`. Replaces the legacy single-aggregate `Inventory.quantity_reserved` field with full audit trail, per-WO reservation history, reallocation support, and shortage analysis.
_Avoid_: Reservation Log, Stock Hold

**Backordered Quantity**:
The number of units a supplier owes on a Purchase Order line item that have not yet been delivered. Tracked on `PurchaseOrderItem.backordered_qty`. Computed as `ordered_qty - received_qty - cancelled_qty` and stored explicitly for audit and reporting. A non-zero backordered quantity indicates the line is in `PARTIAL_RECEIVED` state and the supplier is expected to deliver the remaining qty in a future shipment.
_Avoid_: Pending Delivery, Outstanding Qty

**Free Stock**:
The quantity of a SparePart that is actually available for new requests, computed as `Inventory.quantity_available - Inventory.quantity_reserved`. Distinct from gross `quantity_available` (which includes already-reserved stock). Used as the basis for shortage decisions: a PartShortageReport is raised when `requested_qty > free_stock`, not when `requested_qty > quantity_available`. This prevents over-allocation when many WOs are simultaneously requesting the same part.
_Avoid_: Net Stock, Usable Stock

**Quarantine Quantity**:
The quantity of a SparePart held in `Inventory.quantity_quarantine` after being received from a supplier in damaged condition. Distinct from `quantity_available` (usable) and `quantity_rejected` (rejected at inspection, audit only). Damaged units in quarantine are not visible to maintenance technicians, not usable for work orders, and await return-to-supplier or write-off. Tracked separately so procurement, warehouse, finance, and maintenance each see their relevant inventory view without conflating usable stock with damaged stock.
_Avoid_: Damaged Stock, Hold Inventory

**Impact Score**:
A composite 0-100 score representing the business impact of a blocked spare part, used for procurement prioritization. Computed by `PartImpactService.compute_impact(part)` from: affected WOs (×10), affected assets (×5), estimated downtime hours (×1.5), blocked labor hours (×1), and revenue impact in thousands of dollars (×2). Threshold bands: <40 LOW, 40-75 MEDIUM, >75 HIGH. Drives the "Purchase now" recommendation on the Shortage Dashboard and in the manager view of the Part Request modal.
_Avoid_: Priority Score, Urgency Score

**Repair Viability Score**:
A 0-100 ratio (also expressed as a percentage) of average historical repair cost to replacement cost for a specific spare part, with a categorical REPAIR / REPLACE / BORDERLINE recommendation. Computed by `RepairViabilityService.compute(part, asset, component)`. Rules: repair_ratio < 30% → REPAIR; > 70% with low MTBF → REPLACE; > 50% with high repair count and low MTBF → REPLACE; 50-70% → BORDERLINE. The asset-specific MTBF is computed when at least 2 historical failures exist for the part on that asset.
_Avoid_: Repair Health, Cost Ratio

**Work Order Health Card**:
A 1-glance summary card on the Work Order Detail page, immediately under the header. Shows lifecycle status, operational status, priority, risk level (LOW/MEDIUM/HIGH), open blockers count, waiting days, and full cost breakdown (parts, vendor, procurement, consumables, additional, total). Computed by `WorkOrderHealthService.compute(wo)`. Risk is HIGH if the WO is emergency, has critical priority, is waiting >30 days, has 3+ open blockers, or has total cost > $5000. Risk is MEDIUM if waiting >7 days, has any open blocker, or has total cost > some threshold. Otherwise LOW.
_Avoid_: WO Summary, Status Card

**WO Blocker Event**:
An immutable, structured audit record of a domain event related to a WO Blocker (e.g. BLOCKER_CREATED, BLOCKER_RESOLVED, BLOCKER_CANCELLED, PART_APPROVED, SHORTAGE_FULFILLED, ERO_ACCEPTED, EMERGENCY_INTERRUPTED, LABOR_RESUMED). Carries actor, payload, and timestamp. Used for operational analytics, forensic timelines, and KPI computation. Distinct from the generic AuditEntry table.
_Avoid_: Audit Log, State Log

**Lifecycle Status**:
The explicit, business-process state of a Work Order, set by user actions: draft (reserved for future use; not produced in Phase 1), assigned (WO created from a validated issue; may or may not have a technician assigned), in_progress (technician started labor), pending_review (technician submitted for manager review), closed (manager accepted; terminal), cancelled (never completed; terminal). Lifecycle moves forward by user action, never derived. The legacy `WorkOrder.status` value `approved` maps to `assigned` during the dual-read migration; the value `draft` is unused in Phase 1 but reserved for Phase 2+ (e.g. manager-composed WOs without a triggering issue).
_Avoid_: WO Status, Workflow Status

**Operational Status**:
The derived, current execution condition of a Work Order, computed from open WO Blockers and labor state: active (lifecycle is in_progress and no blockers), pending_parts (any PART or SHORTAGE blocker open), waiting_vendor (any VENDOR_REPAIR blocker open), paused (any OPERATIONAL blocker open, or lifecycle is in_progress/assigned/draft with no labor running). Cannot be set directly; always computed by WorkOrderService.recompute_operational_status.
_Avoid_: Execution Status, Work State

**procurement_cost**:
The cost of parts procured for a Work Order via Purchase Request / Purchase Order (sum of POItem.received_qty × unit_price for POs linked via PRs to this WO). Distinct from parts_cost, which is the cost of parts actually issued to the WO. procurement_cost can be positive while parts_cost is zero (parts received but not yet issued). Rolled up to Asset and Component.
_Avoid_: Ordered Cost, PO Cost

**source_work_order**:
On a WO Blocker of kind OPERATIONAL, the Work Order that triggered the operational pause (typically an Emergency Work Order whose start auto-paused the current WO). Nullable; only set for system-driven pauses, not for technician-initiated pauses.
_Avoid_: Cause WO, Origin WO

**Failure Category**:
A top-level classification of equipment failures (Mechanical, Electrical, Hydraulic,
etc.). Globally shared across all  machines.
_Avoid_: Issue Type, Problem Category

**Failure Mode**:
A specific failure pattern within a category (e.g., Bearing Failure under Mechanical).
Globally shared. Auto-assigned a failure code (MECH-BRG-001).
_Avoid_: Failure Type, Defect Mode

**Asset Hierarchy**:
The site → area → production line → machine → subassembly → component tree structure.
Implemented as a single self-referential table (Machine) with `parent` FK and `asset_level`.
Levels: 1=Area, 2=Production Line, 3=Machine, 4=Subassembly, 5=Component.
Issues, PMs, WOs, EROs, and PRs can be logged at any level (3+). Downtime aggregates upward.
The Asset Tree Widget is rendered on every asset-targeting page so users can navigate
the hierarchy and create child records from any node.
_Avoid_: Machine Tree, Equipment Hierarchy

**Asset Tree Widget**:
A sidebar component rendered on every asset-targeting page (Machine, Subassembly,
Component, Work Order, Maintenance Issue, PM Schedule, Purchase Request, External
Repair Order). Shows the vertical chain from the site root down through the
current node, with descendants one level deep. The current node is highlighted.
Includes quick-create controls (`+ Subassembly`, `+ Component`, `+ Quick` dropdown
for Issue/WO/PM/ERO/PR) that deep-link to the corresponding create forms with
`?machine=<id>&component=<id>` URL parameters pre-filled.
_Avoid_: Asset Browser, Asset Navigator

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

### WorkOrder Status Split (lifecycle vs operational)
- `WorkOrder.status` (single field, deprecated) is replaced by TWO fields:
  - `lifecycle_status` — explicit, user-driven: draft, assigned, in_progress, pending_review, closed, cancelled
  - `operational_status` — derived from open WO Blockers + labor state: active, pending_parts, waiting_vendor, paused
- Single `status` field is kept as a computed property for back-compat until deprecation window ends
- Lifecycle moves forward only by user action; operational moves automatically with blocker state

### WorkOrder Migration Version
- `WorkOrder.blocker_system_version` (IntegerField, default=0) distinguishes WOs by which generation of the state model they were created under
- 0 = created under the legacy single-field `status` model (pre-blocker-system)
- 1 = created under the new `lifecycle_status` + `operational_status` + `WorkOrderBlocker` model
- New WOs auto-increment to 1 on creation; legacy WOs keep 0 until a real domain event (new part request, new pause, etc.) creates the first blocker row
- The Legacy Reconciliation Dashboard filters on `blocker_system_version=0`
- Reports can segment KPIs by version to detect migration-window anomalies
- Field is retained until all WOs are version 1, then dropped

### WO Blocker as Visibility Layer
- WO Blockers are a read-model view over PartIssueLine, PartShortageReport, ExternalRepairRequest, and ExternalRepairOrder
- These four external entities remain the source of truth for "what is being waited on"
- The blocker row carries WO-specific metadata (opened_at, opened_by, note, pause_reason, source_work_order) and history
- Never reopen a RESOLVED or CANCELLED blocker; emit a new blocker for a new wait episode
- Backfill strategy: do NOT backfill blockers for legacy WOs; compute operational_status by querying external entities directly until a WO has its first new-domain-event blocker

### Operational Pause Rule (content-based)
- A "Pause work" action creates an OPERATIONAL WO Blocker only if ANY of:
  - The pause was auto-triggered (emergency override)
  - The pause reason is "other"
  - The pause note is non-empty
- Otherwise the pause is recorded in WorkOrderStateLog only (no blocker row)
- Rationale: avoid noise from micro-pauses (grab a coffee, brief interruption) while preserving audit trail for meaningful pauses

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
## Internationalization (i18n)

The MMS is bilingual (English + Arabic) using Django's i18n machinery.

### Key files
- `mms/settings.py` — `LANGUAGES`, `LANGUAGE_CODE`, `LocaleMiddleware`, locale paths
- `templates/base.html` — language switcher form, `<html lang dir>`
- `locale/en/LC_MESSAGES/django.po` — English msgids (source of truth)
- `locale/ar/LC_MESSAGES/django.po` — Arabic translations
- `maintenance/management/commands/check_i18n.py` — `python manage.py check_i18n` lint

### Adding a new translation (translator workflow)
1. Edit `.po` file directly — find the msgid, add `msgstr` Arabic translation
2. Run `python manage.py compilemessages`
3. **Never delete the .po file** — `makemessages` only ADDS new msgids, preserves existing msgstr

### Adding a new translatable string in code
1. Wrap with `{% trans "text" %}` in templates
2. Or `_("text")` / `gettext_lazy("text")` in Python
3. Run `python manage.py makemessages --locale ar` — adds new msgid with empty msgstr
4. Translator adds the Arabic translation
5. Run `python manage.py compilemessages`

### Validating
- `python manage.py check_i18n` — runs R1-R4 (templates); exit code 0 = pass, 1 = issues
- Rules:
  - R1: templates using `{% trans %}` must also `{% load i18n %}`
  - R2: text between HTML tags should be `{% trans "..." %}`
  - R3: `placeholder=`, `title=`, `alt=`, `aria-label=` should be wrapped
  - R4: JS `confirm()` / `alert()` strings should be wrapped
- Wire into CI: `<your CI config>` runs `python manage.py check_i18n`; non-zero exit fails the build

### Adding a 3rd language
1. Add `("xx", "XxxName")` to `LANGUAGES` in `settings.py`
2. `python manage.py makemessages --locale xx`
3. Translate `locale/xx/LC_MESSAGES/django.po`
4. `python manage.py compilemessages`

### Conventions
- Database values stay English (e.g., `WorkOrder.Status.ASSIGNED == "assigned"`)
- Only display strings are translated
- RTL is handled via `[dir="rtl"]` CSS rules in `static/css/mms.css`
- Western digits in both locales (USE_THOUSAND_SEPARATOR=True)
- Arabic plural forms: msgstr[0] through msgstr[5] all set to same value (Arabic doesn't grammatically distinguish 1 vs many)

---

## Post-Phase-1 Architecture Notes (Phases 0-7)

### Emergency Review (Phase 3)
Manager post-review on an emergency auto-issued part line. Stored on
`PartIssueLine.emergency_review_status ∈ {approved, exception, investigate, None}`.
Three outcomes — none of them revert the inventory deduction or the
cost posting. The user's emergency design is one-way: issue → audit →
review. Access via the dedicated panel at
`POST /work-orders/<pk>/line/<line_id>/emergency-review/`.

### activity_uuid (Phase 5)
Stable per-line `UUIDField` on `PartIssueLine`. Used for:
- Grouping lines of the same maintenance activity in the WO timeline.
- Audit cross-references between lines.
- Future Phase 2.5+ Activity model extraction (where one Activity has
  many PartIssueLines).
**NOT a uniqueness key.** Duplicate prevention is enforced at the
service layer in `request_part_on_wo()` via `select_for_update(WorkOrder)`
plus an ACTIVE-line check. The activity_uuid is auto-generated per
request and stable for the line's lifetime.

### activity_label (Phase 5)
Free-text display label (max 120 chars, optional). Human-readable only.
Surfaced in the PartRequestForm as an optional input next to qty.

### Duplicate Detection Rule (Phase 5)
A PartIssueLine is considered ACTIVE (eligible for duplicate
prevention) when its status is one of:
`PENDING`, `APPROVED`, `ALLOCATED`.
Statuses `ISSUED`, `REJECTED`, `CANCELLED` are not ACTIVE and are
ignored. The constant `ACTIVE_REQUEST_STATUSES` in
`inventory/results.py` is the single source of truth.

### Free-Stock Correctness Invariant (Phase 1)
Reservation math:
`Inventory.quantity_available` (physical on-hand) is always reduced by
the sum of ACTIVE `InventoryReservation.quantity` rows.
`Inventory.compute_quantity_reserved()` aggregates this live. The
legacy `Inventory.quantity_reserved` column was dropped in migration
0017. `free_stock = quantity_available - sum(ACTIVE reservations)`.

When a shortage transitions to CLOSED, all reservations on the
(part, work_order) pair are released — both line-linked ones
(`source_line__related_shortage_report=report`) AND legacy ones
(`source_line=None`). Legacy reservations created by the pre-Phase-1
shortage-decision path can be cleaned up via
`manage.py reconcile_legacy_reservations`.

### Cost Card Composition (Phase 2)
WO cost card exposes (all `WorkOrderCost` properties):
- `material_cost` — ledger sum of `CostTransaction(category="material")`
- `vendor_repair_cost` — sum of `CostTransaction(category="vendor_repair")`
- `consumables_cost` — sum of `CostTransaction(category="consumable")`
- `additional_cost` — sum of `CostTransaction(category="adjustment")`
- `procurement_cost` (derived property, cached on instance) —
  `sum(POItem.received_qty × POItem.actual_unit_price where PO is linked
  via PR to this WO)`. NOT stored as a column.

### Future: Activity Extraction (Phase 2.5+)
Long-term, the activity concept graduates to its own model:
```
Activity(id, uuid, label)
PartIssueLine.activity_id → Activity
```
This enables multi-line activities (one bearing-replacement activity
covering bearing + seal + circlip lines), per-activity cost rollups,
and group-level approval workflows. Phase 5 ships `activity_uuid`
inline on `PartIssueLine` as the seed for this evolution; the migration
to a dedicated `Activity` model is non-breaking.

### Future: Cost Card Evolution (Phase 2.5+)
Replace the current `material_cost / vendor_repair_cost /
consumables_cost / additional_cost / procurement_cost` breakdown with
the manager-facing 5-bucket model:
- **Committed Cost** = ordered but not yet received (PR + non-received PO lines)
- **Consumed Cost** = `material_cost` (as today)
- **External Service Cost** = `vendor_repair_cost` (as today)
- **Pipeline Cost** = received but not yet issued (`procurement_cost − material_cost`)
- **Total Operational Cost** = `Consumed + External Service + adjustments`

Phase 2 ships the building blocks (`procurement_cost` derivation,
ledger strict rollback, ERO single-post guard) needed to compute
`Pipeline Cost` correctly. Phase 2.5 will rebuild the cost card UI
around the 5-bucket model.

---

## Implementation Status (snapshot, this branch)

| Component | State |
|---|---|
| `manage.py check` | OK |
| Migrations needed | 4 migrations pending apply (apply before push) |
| Test pass rate | 244/247 across maintenance + inventory + procurement blocks (3 pre-existing failures from in-flight working tree: 1 decimal format + 2 PostgreSQL `SUBSTRING FROM n` on SQLite) |

