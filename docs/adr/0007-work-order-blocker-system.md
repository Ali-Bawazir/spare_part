# ADR-0007: Work Order Blocker System

**Date:** 2026-06-15
**Status:** Accepted
**Deciders:** System Architect

---

## Context

A Work Order waits on many things in parallel: an approved part still being
issued, a part shortage waiting on procurement, an external vendor repair in
flight, an operational pause triggered by an emergency. Today these waits live
in three loosely-coupled tables (`PartIssueLine`, `PartShortageReport`,
`ExternalRepairOrder`/`ExternalRepairRequest`) with no unified view, no single
audit trail, and no derived "what is this WO actually doing right now" status.
The legacy `WorkOrder.status` CharField conflates lifecycle (assigned vs
closed) with operational state (active vs waiting on parts) and is updated by
ad-hoc branching scattered across services.

---

## Decision

### 1. WorkOrder Status Field Split

Replace the single `WorkOrder.status` with **two** fields:

- **`lifecycle_status`** — explicit, user-driven: `draft`, `assigned`,
  `in_progress`, `pending_review`, `closed`, `cancelled`. Forward motion only
  by user action.
- **`operational_status`** — derived, computed by
  `WorkOrderService.recompute_operational_status()`: `active`, `pending_parts`,
  `waiting_vendor`, `paused`. Moves automatically with blocker state.

Keep `WorkOrder.status` as a **computed property** for back-compat (returns
`lifecycle` unless `IN_PROGRESS`, then returns `operational`). Migration map:
legacy `approved` → `assigned`; legacy `pending_parts`/`waiting_vendor`/
`paused` → `lifecycle=in_progress` with `operational_status` computed from
external entities. `draft` is reserved for Phase 2 manager-composed WOs and
is not produced in Phase 1.

### 2. WO Blocker as Visibility Layer

`WorkOrderBlocker` is a **read-model** view over four authoritative entities:
`PartIssueLine`, `PartShortageReport`, `ExternalRepairRequest`,
`ExternalRepairOrder`. Those four remain the **source of truth** for "what is
being waited on." The blocker row carries WO-specific metadata only
(`opened_at`, `opened_by`, `note`, `pause_reason`, `source_work_order`,
lifecycle timestamps). If a blocker row is deleted, it can be reconstructed
from external entity state (with loss of historical metadata). `OPERATIONAL`
blockers carry nullable `source_work_order` FK pointing to the Emergency WO
that auto-paused the current WO.

### 3. WorkOrderBlocker + WorkOrderBlockerEvent Models

- **`WorkOrderBlocker`**: `kind` ∈ {`PART`, `SHORTAGE`, `VENDOR_REPAIR`,
  `OPERATIONAL`}; `status` ∈ {`OPEN`, `RESOLVED`, `CANCELLED`}; generic FK to
  the originating external entity; nullable `related_ero` FK on
  `VENDOR_REPAIR` set ONCE at manager approval (immutable); nullable
  `source_work_order` FK on `OPERATIONAL`; `pause_reason` on `OPERATIONAL`
  (replaces legacy `WorkOrder.pause_reason`); `migrated_from_legacy` boolean.
  `WorkOrder.blocker_system_version` (0 = legacy, 1 = new) tracks which
  generation created the WO.
- **`WorkOrderBlockerEvent`**: immutable structured audit log with
  `event_type` ∈ {`BLOCKER_CREATED`, `BLOCKER_RESOLVED`, `BLOCKER_CANCELLED`,
  `PART_REQUEST_CREATED`, `PART_APPROVED`, `PART_REJECTED`, `PART_ISSUED`,
  `PART_RECEIVED`, `SHORTAGE_RAISED`, `SHORTAGE_DECIDED`, `SHORTAGE_FULFILLED`,
  `ERO_CREATED`, `ERO_SENT`, `ERO_RETURNED`, `ERO_ACCEPTED`,
  `EMERGENCY_INTERRUPTED`, `LABOR_RESUMED`}, plus `blocker` FK, `actor` FK
  (nullable for system), `payload` JSON, `created_at`. Distinct from generic
  `AuditEntry` — structured for KPI queries.
- **DB invariant:** partial unique constraint — at most one OPEN blocker per
  `(work_order, content_type, object_id)`.
- **Never reopen** a RESOLVED/CANCELLED blocker; a new wait episode = new blocker.

### 4. External Service Hooks (`sync_from_external_event`)

`WorkOrderBlockerService` is the **single** entry point for all blocker
mutations; `WorkOrderBlockerEventService.record()` is the single event write
path. External services call
`WorkOrderBlockerService.sync_from_external_event(external_obj, event_type,
actor, payload)` when their underlying entity changes state. The blocker
service decides what to do (resolve, cancel, no-op) — external services
publish a fact, the blocker service owns the state machine. Hooks needed in:
`request_part_on_wo`, `approve_part_request`, `reject_part_request`,
`work_order_request_part_re_review`, `mark_shortage_fulfilled`,
`work_order_request_external_repair`, `work_order_decide_external_repair`,
`repair_manager_accept`, `technician_start_work`, `work_order_pause`,
`pause_other_in_progress`.

### 5. Migration: Dual-Read Fallback (No Synthetic Backfill)

New tables start **empty**. `recompute_operational_status()` has a dual-read
fallback: primary = read `WorkOrderBlocker` rows; fallback (when no blockers
exist for a WO) = query external entities directly — PENDING PartIssueLines
or open PartShortageReports → `pending_parts`; PENDING ExternalRepairRequests
or active EROs → `waiting_vendor`; legacy `pause_reason` set → `paused`.
**No synthetic blocker backfill** for legacy WOs ("fake history is worse than
missing history"). Legacy WOs stuck in old `pending_parts`/`waiting_vendor`/
`paused` with no current external entity get a "Stuck Legacy" flag and appear
on a **Reconciliation Dashboard** at `/work-orders/reconciliation/`
(filtered on `blocker_system_version=0`) showing WO, old status, last activity,
and suggested action (resume / cancel / log a real part/vendor request).
Manager decides; no auto-modification. Fallback kept for **2 release cycles**,
then removed. Legacy WOs with `pause_reason='emergency'` get **no**
`source_work_order` link — the dual-read fallback handles them.

### 6. Operational Pause Rule (Content-Based)

A "Pause work" action creates an `OPERATIONAL` WO Blocker **only if any of**:
pause was auto-triggered (emergency override); pause reason is `other`; pause
note is non-empty. Otherwise the pause is recorded in `WorkOrderStateLog`
only. Rationale: avoid noise from micro-pauses while preserving audit trail
for meaningful ones. This is **content-based**, not duration-based (no
15-minute threshold). On "Resume labor", any open `OPERATIONAL` blockers on
that WO auto-resolve with `resolution_note="Resumed at HH:MM"`.

### 7. Part Issue Pipeline — 5-Stage Allocation

The `PartIssueLine` model gains a 5-stage pipeline:
- Stage 1 `REQUESTED`: `requested_qty` (tech asks)
- Stage 2 `APPROVED`: `approved_qty` (manager approves)
- Stage 3 `ALLOCATED`: `allocated_qty` (NEW field; inventory reserved)
- Stage 4 `ISSUED`: `issued_qty` (warehouse delivers)
- Stage 5 `CONSUMED`: implicit when the WO is closed

The new `allocated_qty` Decimal field on `PartIssueLine` is set by `approve_part_request`:
- `allocated_qty = min(approved_qty, available - reserved_other_lines)`
- If `allocated_qty < approved_qty`: the gap is `shortage_qty` and triggers a `PartShortageReport`
- `Inventory.quantity_reserved` is incremented by `allocated_qty` (soft planning)
- Stock is NOT yet physically deducted; that happens in the warehouse issue step

The display surface (Part Issue card on WO detail) shows all 5 stages as separate values so the tech, manager, and warehouse can each see where the line is in the pipeline.

### 8. Queue Position — Priority-Ranked, Not FIFO

The `PartAllocationService.queue_position(line, part)` method computes a position based on composite priority:
1. `WO.is_emergency` (Emergency = first)
2. `WO.priority` (`CRITICAL` > `HIGH` > `NORMAL` > `LOW`)
3. Line age (oldest first as tiebreaker)

A new `PartAllocationService.reallocate_for_part(part)` is called when a PO is received and stock is replenished. It walks all open lines in priority order and grants new `allocated_qty` based on available + replenished stock. Notifications fire to the affected WOs.

### 9. Impact Score — Procurement Prioritization KPI

A new `PartImpactService.compute_impact(part, site=None)` returns a composite score for the part's blocking impact:
- `affected_wos`: count of open `PartIssueLine`s
- `affected_assets`: distinct machines with open lines
- `estimated_downtime_hours`: sum of current open `Downtime` for the affected WOs
- `blocked_labor_hours`: time since affected WO creation
- `revenue_impact`: `estimated_downtime_hours × site.default_revenue_per_hour` (Phase 1: per-site; Phase 2: per-machine)
- `impact_score`: weighted composite (0-100)

Threshold bands: `<40` LOW, `40-75` MEDIUM, `>75` HIGH. The "Purchase now" recommendation fires for HIGH.

Displayed in the manager view of the Part Request modal, on the Shortage Dashboard per row, and on the Active Blockers Dashboard.

### 10. Repair Viability Score — REPAIR vs REPLACE

A new `RepairViabilityService.compute(part, asset=None, component=None)` returns:
- `avg_cost`, `lowest_cost`, `highest_cost`, `replacement_cost`, `repair_count`, `avg_tat_days`
- `mtbf_days` (computed for the specific asset if provided, requires ≥2 historical failures)
- `repair_ratio` (avg_repair_cost / replacement_cost × 100)
- `recommendation`: `REPAIR` (🟢) | `REPLACE` (🔴) | `BORDERLINE` (🟡)
- `rule`: which rule fired (for transparency)

Decision rules:
- `repair_ratio < 30` → `REPAIR` (low_ratio)
- `repair_ratio > 70 AND (mtbf_days is None OR mtbf_days < 30)` → `REPLACE` (high_ratio_low_mtbf)
- `repair_ratio > 50 AND repair_count >= 5 AND mtbf_days < 30` → `REPLACE` (frequent_failures)
- `50 < repair_ratio < 70` → `BORDERLINE` (marginal)
- default → `REPAIR`

Displayed in the External Repair Request modal alongside the asset history panel.

### 11. Video Compression Pipeline

Accept `MP4`, `MOV`, `WEBM` in the attachment system. New `VideoCompressionService` (in `inventory/services_attachments.py`):
- Uses ffmpeg (subprocess) to compress to 720p H.264 + AAC audio at 1500k bitrate
- Generates a thumbnail frame at 1 second
- Stores `Attachment.is_video`, `Attachment.compressed_path`, `Attachment.thumbnail_path`
- Max input: 100MB pre-compression; max output: ~30MB compressed
- Phase 1 fallback: if ffmpeg is not installed, log warning, store original as-is, show "processing skipped" badge in the UI. No upload rejection.

The attachment system is extended to accept video MIME types. Per-entity limits (10 attachments) and image/PDF limits (5MB) remain unchanged.

### 12. Root Cause Emergency — Recursive Source Chain

A new `WorkOrderBlocker.root_source_emergency` property walks the `OPERATIONAL` blocker chain upward:
- Start at the current blocker
- Follow `source_work_order` until we reach a blocker with `source_work_order = NULL` OR detect a cycle
- Return the root `WorkOrder` (the original emergency)

The chain visualization on the WO detail page shows the full trail (e.g., "WO #500 → WO #555 → WO #556") with each WO as a clickable link. Cycle guard via a `visited` set prevents infinite loops.

### 13. Work Order Health Card

A new `WorkOrderHealthService.compute(wo)` returns a 1-glance summary:
- `lifecycle_status`, `operational_status`, `priority`
- `open_blockers_count` (count of `OPEN` blockers)
- `waiting_days` (time since the WO entered a non-active state — paused, `pending_parts`, `waiting_vendor`, etc.)
- Cost breakdown: `total_cost`, `parts_cost`, `vendor_cost`, `procurement_cost`, `consumables_cost`, `additional_cost`
- `risk`: `LOW` / `MEDIUM` / `HIGH` (composite of `waiting_days` + `blocker_count` + cost + priority)

Risk rules:
- `is_emergency OR priority == CRITICAL` → `HIGH`
- `waiting_days > 30 OR open_blockers >= 3` → `HIGH`
- `waiting_days > 7 OR open_blockers >= 1 OR total_cost > 5000` → `MEDIUM`
- default → `LOW`

Displayed on the WO Detail page, right under the header, as a single card. The card is collapsible.

### 14. Procurement Return Form — Data Capture at Source

The current single `ExternalRepairOfficerForm` is split into two:
- `ExternalRepairReturnForm` (procurement officer, `SENT` → `RETURNED`): captures `actual_cost`, `invoice_ref`, `vendor_name`, `return_condition_note` (all required); photos/videos via the attachment system
- `RepairManagerAcceptForm` (manager, `RETURNED` → `CLOSED` — simplified): just verification; `actual_cost` / `invoice_ref` / `vendor_name` are read-only (pre-filled by procurement)

The cost flows up automatically: `WorkOrderCost.recalculate()` is called on RETURN, updating `vendor_cost`. Asset-level rollup picks it up.

---

## Considered Options

- **Option A (rejected):** Keep three loosely-coupled waiting systems as-is.
  Rejected because techs have to mentally join tables to answer "what is this
  WO waiting on" and there is no unified audit trail — KPIs like "avg wait
  per WO" are not computable.
- **Option B (rejected):** Make `WorkOrderBlocker` the **source of truth** for
  all wait state, replacing the four external entities. Rejected because it
  violates separation of concerns, duplicates state with `PartIssueLine`/
  `ERO`, and breaks the existing domain events those entities already publish
  to inventory and procurement.
- **Option C (chosen):** `WorkOrderBlocker` as a **visibility layer** over
  authoritative external entities. Best of both — single queryable view and
  audit log for the WO, while `PartIssueLine`/`PartShortageReport`/`ERO`/
  `ExternalRepairRequest` keep owning their state and feeding inventory,
  procurement, and vendor flows.

---

## Consequences

- **Positive:** single queryable "what is this WO waiting on" view; structured
  event-sourced audit trail; KPI-friendly (blocker duration, time-in-state);
  asset-centric rollups enabled; legacy `status` field stays computable
  during the migration window.
- **Negative:** migration complexity (dual-read period, Reconciliation
  Dashboard, 2-cycle fallback); new `operational_status` requires careful
  UI integration (no double-counting with `lifecycle_status`); the
  `WorkOrderBlockerEvent` table grows fast, mitigated by the partial unique
  constraint and a future retention policy.
- **Neutral:** the blocker layer does not change existing data flows — it
  derives from them. External services grow one hook each.
- **Phase 2 deferrals** (per `PHASES.md`): SLA engine (blocker-duration
  alerts), escalation chains, predictive "this WO will be blocked" forecasting.

### Operational Intelligence

- **Positive:** Managers get a 1-glance WO Health Card; procurement gets an Impact Score for prioritization; techs see their queue position; the system surfaces root cause for emergency pauses
- **Negative:** Multiple new service classes (`PartAllocationService`, `PartImpactService`, `RepairViabilityService`, `WorkOrderHealthService`, `VideoCompressionService`) increase the code surface; testing surface grows proportionally
- **Neutral:** These are read-model computations, no new business state; they can be removed or replaced without data integrity impact

### Video Storage

- **Positive:** Compressed storage keeps the attachment size manageable; thumbnail enables previews
- **Negative:** ffmpeg dependency on the server; Phase 1 fallback (no compression) is a degraded experience
- **Neutral:** Phase 2: per-machine revenue_per_hour override, video streaming optimization
