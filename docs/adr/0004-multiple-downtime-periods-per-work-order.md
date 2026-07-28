# Multiple Downtime Periods Per Work Order

Work orders in a factory can be interrupted by parts delays, vendor waits, or
emergency interruptions. The downtime clock continues during these interruptions —
only the labor timer stops. A single start/end timestamp pair cannot represent
this accurately for analytics.

## Decision

Separate Downtime model (one-to-many with WorkOrder). Each time a WO starts
work, a Downtime record is created with start_time. When the WO is closed,
the open Downtime record is ended and total_minutes is computed.
WorkOrder.downtime_started_at/downtime_ended_at remain as first-start /
final-close snapshots for backward compatibility.

## Consequences

- MTTR and downtime analytics computed from Downtime.total_minutes
- Emergency interruptions create new downtime records without closing prior ones
- Labor hours remain distinct from downtime hours
- Waiting-for-parts and waiting-for-vendor transitions do NOT end the Downtime record
- Multiple Downtime records per WO give accurate breakdown: productive time vs waiting time