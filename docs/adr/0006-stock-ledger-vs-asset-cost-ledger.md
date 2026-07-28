# ADR-0006: Stock Ledger vs Asset Cost Ledger

**Date:** 2026-06-09
**Status:** Accepted
**Deciders:** System Architect

---

## Context

A factory maintenance system needs to surface two related but conceptually
different financial views: the value of parts currently sitting in the warehouse
(a balance-sheet view) and the value of parts and services already consumed
by each asset (a P&L view). Conflating them produces a number that means
neither "what do we own" nor "what did this machine cost to maintain" — the
two move in opposite directions on the same event. When a part is issued to
a work order, stock value drops while the asset's accumulated cost rises.

---

## Decision

- **Stock Ledger** is a balance-sheet view: `sum(Inventory.quantity_available × unit_cost)`.
  It drops when parts leave the warehouse. It is NOT tied to any asset.
- **Asset Cost Ledger** is a P&L view: `sum(WorkOrderCost.total_cost)` per asset,
  rolled up through the asset hierarchy (machine → line → area → site). It
  rises on the same event that makes the Stock Ledger drop.
- Both are surfaced on the **same page** (`machine_cost_report.html`) as
  **separate cards**: Stock Total on top, hierarchical cost tree below. They
  are **not summed** into a grand total.
- **Cost formula** for WorkOrderCost remains: `parts + vendor + consumables + additional`,
  excluding labor and downtime (per CONTEXT.md).
- **Stock Total is site-wide** in Phase 1 (single site). Per-site breakdown
  is deferred to Phase 2 multi-site.

---

## Considered Options

- **Option A (rejected):** Show stock as a single column inside the per-asset
  cost table. Rejected because stock is not attributable to a single asset —
  a part in the warehouse hasn't been issued to any machine yet, so placing
  it inside an asset table implies false ownership and confuses the two views.
- **Option B (rejected):** Collapse both into one grand number (e.g., "Total
  Maintenance Value"). Rejected because the two numbers mean fundamentally
  different things and move in opposite directions. A single sum hides the
  signal: a falling stock number with a stable or rising asset cost is
  exactly the kind of insight the report exists to surface.
- **Option C (chosen):** Two distinct cards on the same page, presented as
  sibling views with their own context. Chosen because it preserves the
  semantic distinction, keeps each card's formula auditable, and lets a
  reader compare them visually without merging.

---

## Consequences

- The `machine_cost_report.html` page must clearly label which view is which
  and avoid any visual sum across the two cards.
- Cost-reporting queries remain simple: one aggregation over `Inventory`,
  one recursive sum over `WorkOrderCost` by asset FK chain.
- Per-site stock breakdown, asset-depreciation overlays, and finance-team
  downtime cost integration remain deferred to Phase 2.
