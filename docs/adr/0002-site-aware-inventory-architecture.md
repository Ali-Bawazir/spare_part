# Site-Aware Inventory Architecture

The original SparePart.quantity_on_hand is a global scalar. A factory planning
for multiple sites needs per-site stock tracking with reserved quantities.

## Decision

Introduce Site (single default "Main Factory" for Phase 1) and Inventory
(part+site → available+reserved+rack_location) models. SparePart becomes a
catalog master. StockMovement records site and a structured reference JSON
object with invoice, supplier, and attachment info. The quantity_reserved
field is added to Inventory schema but reservation workflow is deferred to Phase 2.

## Consequences

- All inventory service functions query Inventory by (part, site), not SparePart directly
- StockMovement.reference is a JSON object (not a separate table) to avoid over-normalizing
- Single-site: all records default to the Main Factory site via site selector or auto-default
- Future multi-site: add Site FK to Machine, User, and filter all queries by user's site