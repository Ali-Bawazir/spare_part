# Factory MMS — Phase Specification

## Overview

The system is delivered across three phases. Phase 1 is the MVP — fully built and in
production use. Phases 2 and 3 contain the remaining work.

---

## Phase 1 — MVP ✅ Built

**Scope**: Single site, synchronous operations, internal-only workflow.

### Modules Delivered

| Module | Features |
|--------|----------|
| **Accounts** | Login/logout; 6 roles (Operator, Supervisor, Technician, Manager, Procurement Officer, Super Admin); role-based capabilities; user CRUD with self-protection guards (cannot delete/deactivate self, last active super admin protected) |
| **Machines** | Master list; QR codes (`MACHINE:{code}`); parent/child hierarchy; asset levels (Plant / Line / Machine / Component); location; failure category FK |
| **Maintenance Issues** | Report via QR scan or manual → Validate (set priority) → Convert to Work Order; archive support |
| **Work Orders** | Full lifecycle (Approved → Assigned → In Progress → Paused / Waiting Vendor / Pending Parts → Pending Review → Closed); labor timer start/stop; root cause + action taken; state logs; append-only assignment history (ADR-0003) |
| **Emergency WOs** | Direct creation bypassing issue validation; emergency notifications sent to relevant users |
| **Quick Logs** | Ad-hoc maintenance notes without full work order overhead |
| **Spare Parts** | SKU; auto-generated QR code (`PART:{sku}`); category; unit; is_consumable flag; min/max stock levels; avg_cost; last_purchase_cost; status (active / obsolete / discontinued) |
| **Inventory** | Site-aware per-part stock (ADR-0002): `quantity_available`, `quantity_reserved` (schema only, workflow deferred to Phase 2), rack location, cycle-count tracking; site selector on all stock views |
| **Stock Movements** | Full audit trail; movement types: stock-in, stock-out, issue-to-WO, consumable use, adjustment; structured JSON reference field (invoice, supplier, attachment) |
| **Part Issue Lines** | Parts issued from inventory against a work order; captures invoice ref and supplier name |
| **Procurement — Purchase Requests** | PR lifecycle: create → Pending Officer → Ordered → Received / Stock Updated; created manually or auto-triggered by stock shortfall; linked to work order and part |
| **Suppliers** | CRUD; auto code (`SUP-001` …); QR codes (`SUPPLIER:{code}`); contact person; phone; email; address; is_repair_vendor flag |
| **Preventive Maintenance** | PM schedules: machine, title, frequency (days), checklist, next due; spawns PM work orders; propagates to child machines; overdue sync + notifications |
| **Reusable Tools** | Tool master; assign to user; return with condition (Good → Available, Damaged/Lost → Out of Service) |
| **External Repairs** | Repair order → Sent to Vendor → Returned → Manager Acceptance → Closed; linked to work order |
| **Downtime Tracking** | Multiple downtime periods per work order (ADR-0004); types: breakdown / emergency / scheduled / idle; `total_minutes` computed automatically on save |
| **Notifications** | 10 kinds: new issue, issue validated, WO assigned, WO emergency, low stock, procurement, PM overdue, repair returned from vendor, WO pending review; in-app only |
| **KPI Dashboard** | MTTR, MTTW, MTBF, avg downtime hours, PM compliance %, repeat failures, machine failure rate, technician efficiency, tool loss rate, supplier cost ranking, downtime trends |
| **Reports** | Machines by issue count; low-stock items; most-used parts; WO status distribution; technician throughput |
| **Audit Log** | Immutable `AuditEntry` log; actor, action, entity, object_id, JSON payload |
| **Attachments** | File uploads for any entity (work order, machine, spare part, purchase request, repair order); max 10 MB |
| **QR Scanning** | Live camera (browser native API); image upload (OpenCV server-side decode); manual code entry |
| **Infrastructure** | PostgreSQL via Docker (ADR-0001); SQLite fallback when `DB_PASSWORD` not set; Celery/Redis scaffolded in settings (Phase 2) |

### Architecture Decisions (Phase 1)

| ADR | Title | Key Decision |
|-----|-------|--------------|
| ADR-0001 | PostgreSQL with Docker | SQLite insufficient → PostgreSQL in Docker; Redis for Celery (Phase 2) |
| ADR-0002 | Site-Aware Inventory | `Inventory` model (part+site → available+reserved); `SparePart` is catalog master only |
| ADR-0003 | Append-Only WO Assignment History | `WorkOrderAssignmentHistory` — never delete records; reassignment closes prior record |
| ADR-0004 | Multiple Downtime Periods Per WO | `Downtime` model (one-to-many); `WorkOrder.downtime_started/ended` are first-start / final-close snapshots |

---

## Phase 2 — Near-Term Extensions ⬜

**Scope**: Multi-site, async processing, procurement maturation, SLA/escalation.

### Items

| # | Item | Description | Dependency |
|---|------|-------------|------------|
| **P2-1** | **FailureMode** | Sub-classification under `FailureCategory` (e.g. "Bearing Failure" under "Mechanical"). Auto-codes like `MECH-BRG-001`. `MaintenanceIssue.issue_type` currently FK to `FailureCategory` only | — |
| **P2-2** | **Stock Reservation Workflow** | Activate `Inventory.quantity_reserved`; reserve parts when issuing to WO; release on WO close/cancel; prevent over-reservation | Requires P2-3 (PO receiving) to replenish reserved stock |
| **P2-3** | **Purchase Order (entity)** | Supplier-facing document with line items; partial receipts; close-short (order 100, receive 80, close remaining 20); PO approval before sending to supplier; PRs become line items on a PO | Critical — biggest gap vs CONTEXT.md spec |
| **P2-4** | **Celery Background Jobs** | Activate `celery[redis]`, `django-celery-beat`: async notification dispatch, periodic PM due checks, stale issue escalation, low-stock polling | Redis; settings already scaffolded |
| **P2-5** | **Multi-Site** | Extend `Site` beyond single "Main Factory"; add `site` FK to `Machine` and `User`; filter all queries by user's site; per-site stock dashboards | `site` on `Machine` already nullable |
| **P2-6** | **Email Notifications** | Complement in-app notifications with outbound email for: emergency WOs, overdue PM, low stock alerts | Celery + SMTP config |
| **P2-7** | **WO SLA / Stale Escalation** | Auto-escalation when issues/WOs exceed priority-based time thresholds (e.g. Critical issue not validated in 1 hr, High priority WO pending review > 4 hrs) | Celery periodic tasks |
| **P2-8** | **Shift Handover Notes** | Formal handover notes when reassigning a WO between shift technicians; visible on WO detail and assignment history | `WorkOrderAssignmentHistory` already records periods |

---

## Phase 3 — Future / Optional ⬜

**Scope**: External integration, advanced analytics, reporting, finance.

### Items

| # | Item | Description |
|---|------|-------------|
| **P3-1** | **REST API** | Expose core endpoints via `djangorestframework` (already in requirements.txt); mobile app backend; token auth |
| **P3-2** | **Supplier Portal** | External web portal for suppliers: view POs, confirm orders, record shipments |
| **P3-3** | **Advanced Analytics / ML** | Failure prediction from historical issue data; optimal PM frequency recommendations; anomaly detection |
| **P3-4** | **1D Barcode Scanning** | Extend beyond QR to standard 1D barcodes for parts inventory (in addition to QR) |
| **P3-5** | **Document Management** | Versioned attachments; document approval workflows; external document linking |
| **P3-6** | **Finance / Cost Module** | Labor cost per technician; cost per machine; P&L per asset; budget tracking |
| **P3-7** | **Maintenance Planner** | Gantt-style maintenance scheduling; resource (technician + tool) availability calendar |
| **P3-8** | **Custom Reporting Builder** | User-defined report templates; scheduled report generation and email delivery |

---

## Critical Gap: Purchase Order vs Purchase Request

CONTEXT.md defines two distinct procurement entities:

| Term | Definition |
|------|------------|
| **Purchase Request (PR)** | ✅ Built — internal request for procurement; created manually or auto-triggered by stock shortfall; can be linked to a PO |
| **Purchase Order (PO)** | ❌ Not built — supplier-facing document issued to a supplier for one or more spare parts; supports line items, partial receipts, close-short |

**Current gap**: The system has `PurchaseRequest` but no separate `PurchaseOrder` model with:
- PO header (supplier, created by, status, notes, total value)
- Line items (part, qty ordered, qty received, unit price)
- Partial receipt (receive 80 of 100 ordered → remaining 20 auto-closes)
- Close-short (cancel remaining lines when PO is finalized)
- PO sending workflow (Draft → Sent → Received / Cancelled)
- PO receiving page: update line-by-line received quantities → auto stock-in on save

**PR → PO flow**: One or more PRs selected → converted into a single PO with each PR as a line item.

---

## Phase Timeline (Conceptual)

```
Phase 1 (MVP)  ─────────────────────────────────────────────────── ✅ Done
Phase 2 (Near-term)  ────────────────────────────────────────────  Planned
  P2-3 PO model          ← highest priority
  P2-1 FailureMode
  P2-2 Reservation
  P2-4 Celery
  P2-5 Multi-site
  P2-6 Email
  P2-7 SLA Escalation
  P2-8 Shift Handover
Phase 3 (Future)  ───────────────────────────────────────────────  Optional
  P3-1 REST API
  P3-2 Supplier Portal
  P3-3 Analytics
  P3-4 1D Barcodes
  P3-5 DMS
  P3-6 Finance
  P3-7 Planner
  P3-8 Custom Reports
```

---

## Change Log

| Version | Date | Change |
|---------|------|--------|
| 1.0 | $(date +%Y-%m-%d) | Initial — captured from codebase analysis and ADR documents |
