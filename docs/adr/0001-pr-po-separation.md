# ADR-0001: Purchase Request vs Purchase Order Separation

**Date:** 2026-05-21
**Status:** Accepted
**Deciders:** System Architect

---

## Context

In factory maintenance operations, we need to track two distinct procurement phases:
1. **Internal demand** — someone identifies a need for a part
2. **External transaction** — we actually order from a supplier

These are conceptually different and should not be conflated.

---

## Decision

We will maintain **two separate models**:

### PurchaseRequest (PR)
- **Purpose:** Internal demand signal
- **Created by:** Maintenance Manager
- **Trigger:** Low stock, emergency shortage, or work order needing parts
- **Status:** PENDING → CONVERTED_TO_PO → PARTIALLY_FULFILLED / FULFILLED / CANCELLED
- **May exist without PO:** For manual purchases (phone order, local market)

### PurchaseOrder (PO)
- **Purpose:** Actual supplier transaction
- **Created by:** Procurement Officer
- **Contains:** Line items with ordered vs received quantities
- **Status:** DRAFT → SENT → PARTIAL_RECEIVED → RECEIVED / CLOSED_SHORT / CANCELLED
- **Always links back to PR(s):** For traceability

### Why not merge?

Merging PR and PO into one model creates problems:
- A PR can be partially fulfilled by multiple PO deliveries
- A PO can fulfill multiple PRs (bulk ordering)
- The statuses have different meanings
- Audit trail requires knowing what was requested vs what was ordered

### Storage
- `attachments/originals/` — original files (JPG, PNG, WEBP, PDF)
- `attachments/thumbs/` — 300px thumbnails for fast loading
- Max 10 attachments per entity, 5MB per file

---

## Consequences

### Positive
- Clear separation of concerns
- Supports partial receipts naturally
- Better audit trail: what was needed vs what was ordered
- Attachments work for both PR (damaged part photo) and PO (invoice/delivery proof)

### Negative
- Slightly more complex data model
- UI needs to handle PR→PO conversion flow

### Neutral
- PO number format: `PO-YYYY-NNNN` (auto-generated)

---

## Notes

- Photo evidence is critical for procurement: PR photos show what's needed, PO receiving photos show what arrived
- Email notifications for procurement are hooks-only in Phase 1, active in Phase 2
- All uploads optional (not mandatory)

---

## Deferred
- Auto PM spawn (Phase 2: Celery/beat)
- Reservation workflow (Phase 2)
- Supplier analytics (Phase 2)