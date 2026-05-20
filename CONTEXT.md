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