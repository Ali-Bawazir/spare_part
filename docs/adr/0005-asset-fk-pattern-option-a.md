# Asset FK Pattern — Option A (Two-FK to Machine)

All entity models that target an asset in the hierarchy (WorkOrder, PurchaseRequest,
ExternalRepairOrder, PMSchedule, FailureHistory) use two FKs to the Machine table:
a required `machine` FK (level-3) and an optional `component` FK (level-5). The
`component`'s ancestor chain must resolve to the specified `machine`. Subassembly
(level-4) is inferred from the component's parent, not stored separately.

This avoids recursive tree traversal in reports and preserves historical integrity
when the hierarchy is reorganised.

**Considered Options:**

- **Option B (single `asset` FK)** — one FK to Machine, level inferred from
  `asset_level`. Requires recursive traversal to answer "which machine did this
  WO belong to?" since the FK could point to any level. Also fragile if the
  hierarchy is later reorganised.
- **Option C (computed/virtual machine)** — store `component` FK only and
  compute the machine via `parent` chain traversal at query time. Simple schema
  but expensive for reports and breaks if the hierarchy changes after the WO is
  closed.

Option A was chosen because fast reporting (no traversal), historical integrity
on hierarchy reorganisation, and clear audit trail are higher priorities than
schema simplicity.
