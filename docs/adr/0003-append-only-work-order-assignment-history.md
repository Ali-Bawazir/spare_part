# Append-Only Work Order Assignment History

Factories operate in shifts. A work order may be assigned to technician A, released,
then assigned to technician B. Assignment decisions need full audit traceability
for accountability and labor analytics.

## Decision

WorkOrderAssignmentHistory model with append-only records. Each assignment period
creates a new record. When a technician is reassigned, the previous record is
closed (unassigned_at set, reason recorded) but never deleted or modified.
Records are never updated — only new ones added. Manager reassignment auto-closes
prior records. Auto-pause from technician starting another WO closes the prior
assignment record.

## Consequences

- Full shift handover accountability visible on WO detail page
- Accurate labor timeline: sum of assigned periods per technician per WO
- Assignment analytics: who worked on what, for how long, with what outcome
- Immutable history prevents audit manipulation