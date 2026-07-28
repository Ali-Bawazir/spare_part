# Bawazir Factory Maintenance & Spare Parts Management System

## Quick start (Docker — production / company server)

The canonical deployment runs in Docker with PostgreSQL. For local dev with
hot-reload, run `python manage.py runserver` natively against SQLite.

```bash
git clone <repo-url> /opt/mms
cd /opt/mms
cp .env.example .env
# Edit .env: set DB_PASSWORD, SECRET_KEY, ALLOWED_HOSTS
docker compose up -d --build
curl http://localhost:8000/health/    # {"status":"ok","db":"ok"}
```

See [`docs/DOCKER.md`](docs/DOCKER.md) for the full deployment runbook,
service architecture, health endpoint contract, and Phase 2 roadmap.

## Overview

This project is a Django-based internal maintenance management system for factory operations. It combines machine issue reporting, maintenance work orders, spare-parts inventory, procurement, preventive maintenance, reusable tools, external repairs, notifications, KPIs, and user management in one web application.

The goal of the system is to replace informal maintenance handling with a clear and traceable workflow:

- machine issue is reported
- issue is validated and prioritized
- work order is created and assigned
- technician executes repair
- parts, tools, and procurement are tracked
- manager reviews and closes the job
- history, KPIs, and audit logs remain available

## Main Modules

- `Accounts`
  - login/logout
  - role-based access
  - MMS user management
- `Machines`
  - machine master list
  - QR code mapping per machine
  - machine-based issue intake
- `Maintenance`
  - issue reporting
  - issue validation
  - work orders
  - emergency work orders
  - quick logs
- `Inventory`
  - spare parts
  - stock-in
  - issue parts to work order
  - consumables usage
- `Procurement`
  - purchase requests
  - officer processing
  - receiving and stock update
- `Assets`
  - preventive maintenance schedules
  - reusable tools
  - external repair orders
- `Insights`
  - notifications
  - KPI dashboard
  - reports
  - audit log

## System Users

- `Operator / Labor`
- `Supervisor`
- `Technician`
- `Maintenance Manager (Storekeeper)`
- `Procurement Officer`
- `Super Admin`

`Super Admin` and Django `superuser` can access all MMS areas. Other users only see the pages allowed for their role.

## What The System Solves

- unorganized maintenance workflow
- missing machine maintenance history
- delayed repair coordination
- unclear spare-part usage
- low stock and stock-out issues
- lack of procurement traceability
- poor visibility of downtime and repeated failures
- no proper tool assignment tracking
- no structured external repair flow

## Navigation Structure

After login, the user lands on the `Dashboard`. Based on role, the sidebar can show:

- `Overview`
  - `Dashboard`
- `Maintenance`
  - `Issues`
  - `Work orders`
  - `Emergency WO`
  - `Quick log`
- `Inventory`
  - `Stock`
  - `Consumables`
- `Procurement`
  - `Requests`
- `Assets`
  - `Machines`
  - `Preventive`
  - `Tools`
  - `External repairs`
- `System`
  - `Users`
- `Insights`
  - `Notifications`
  - `KPIs`
  - `Audit log`
  - `Reports`

## Important URLs

- Main app: `http://127.0.0.1:8000/`
- Login: `http://127.0.0.1:8000/accounts/login/`
- Admin: `http://127.0.0.1:8000/admin/`
- User management: `http://127.0.0.1:8000/users/`
- Procurement: `http://127.0.0.1:8000/procurement/`

## Complete Business Flow

### 1. Login and dashboard

1. User opens `/accounts/login/`.
2. User signs in with username and password.
3. System opens the dashboard.
4. Dashboard shows:
   - shortcut cards
   - workload counters
   - personal queue items
   - notifications
   - alerts such as overdue PM or pending review

### 2. Machine master and QR setup

Used by: `Maintenance Manager`, `Super Admin`

1. Open `Assets -> Machines`.
2. Add or edit machine details:
   - machine name
   - QR code
   - location
   - active status
3. Save the machine.
4. The saved QR code becomes the machine identifier used during issue reporting.

This step is important because issue reporting and QR-based intake depend on the machine master being maintained correctly.

### 3. Machine issue reporting

Used by: `Operator`, `Supervisor`, `Manager`, `Super Admin`

Two reporting methods are available:

- `QR-based reporting`
  1. Open the QR scan page from the issue page.
  2. Scan a machine QR code live, upload a QR image, or enter the code manually.
  3. The system redirects to the issue form with the machine pre-selected.

- `Manual reporting`
  1. Open `Maintenance -> Issues`.
  2. Click `Report issue`.
  3. Select the machine manually.
  4. Enter issue description.
  5. Submit.

System result:

- a `MaintenanceIssue` is created
- status becomes `New`
- timestamp is recorded
- relevant users receive notifications

### 4. Issue validation and prioritization

Used by: `Supervisor`, `Manager`, `Super Admin`

1. Open `Issues`.
2. Review issues in `New` status.
3. Click `Validate`.
4. Set priority:
   - `Critical`
   - `High`
   - `Medium`
   - `Low`
5. Save validation.

System result:

- issue status becomes `Validated`
- priority is stored
- issue is ready for work-order creation

### 5. Work order creation

Used by: `Manager`, `Super Admin`

1. Open a validated issue.
2. Click `Create WO`.

System result:

- a work order is created automatically
- work order is linked to the original issue and machine
- work order status becomes `Approved`
- issue status becomes `Converted to work order`

### 6. Work order assignment

Used by: `Manager`, `Super Admin`

1. Open `Work orders`.
2. Open the work-order detail page.
3. Select technician in the action panel.
4. Click `Assign / reassign technician`.

System result:

- technician is linked to the work order
- work order status becomes `Assigned`
- technician receives notification
- work order enters technician queue

### 7. Technician queue and execution

Used by: `Technician`, `Super Admin`

1. Technician opens `Work orders`.
2. The queue is shown with important jobs surfaced first:
   - in-progress jobs first
   - emergency jobs
   - higher-priority jobs
3. Technician opens a work order and clicks `Start work`.

System result:

- status becomes `In progress`
- labor timer starts
- downtime tracking begins if not already started

During execution, technician can:

- `Pause`
- set `Waiting for parts`
- set `Waiting for vendor`
- enter root cause
- enter action taken
- add notes

When work is completed:

1. Technician fills completion details.
2. Clicks `Submit for review`.

System result:

- status becomes `Pending manager review`
- labor timer stops
- manager is notified

### 8. Manager review and closure

Used by: `Manager`, `Super Admin`

1. Open a work order in `Pending manager review`.
2. Review:
   - root cause
   - action taken
   - notes
   - parts used
   - state log
   - downtime window
3. Choose one action:
   - `Approve & close`
   - `Reject -> back to tech`

If approved:

- work order status becomes `Closed`
- downtime is finalized

If rejected:

- work order goes back to `In progress`
- technician resumes work

### 9. Emergency maintenance flow

Used by: `Manager`, `Super Admin`

1. Open `Maintenance -> Emergency WO`.
2. Select machine.
3. Enter title and details.
4. Submit.

System result:

- an emergency work order is created directly
- category is `Emergency`
- status starts at `Approved`
- emergency notifications are sent
- manager can assign technician immediately

### 10. Spare-parts issue to work order

Used by: `Manager`, `Super Admin`

1. Open work-order detail.
2. In `Issue spare part`, enter:
   - part
   - quantity
   - unit cost
   - invoice reference
   - optional supplier name
3. Click `Issue to WO`.

System behavior:

- if full stock exists: full quantity is issued
- if partial stock exists: available quantity is issued and purchase request is created for shortage
- if zero stock exists: purchase request is created for full shortage

System result:

- stock movement is recorded
- part issue line is recorded against the work order
- low-stock notifications can be triggered

### 11. Stock dashboard and stock-in

Used by: `Manager`, `Procurement`, `Super Admin`

1. Open `Inventory -> Stock`.
2. Review stock levels, minimum stock, and consumable flags.
3. Low-stock items are highlighted.
4. Click `Stock-in`.
5. Enter:
   - part
   - quantity
   - supplier
   - unit cost
   - invoice reference
   - note
6. Save.

System result:

- quantity on hand increases
- stock movement history is recorded
- supplier and invoice trace remain available

### 12. Consumables usage

Used by: `Operator`, `Supervisor`, `Technician`, `Manager`, `Super Admin`

1. Open `Inventory -> Consumables`.
2. Select consumable part.
3. Enter quantity.
4. Optionally enter machine ID.
5. Click `Log usage`.

System result:

- stock is deducted immediately
- no approval step is required
- usage is recorded for traceability

### 13. Purchase request creation

Used by: `Manager`, `Super Admin`

1. Open `Procurement -> Requests`.
2. Click `New request`, or create one from a work-order page.
3. Fill:
   - part
   - optional linked work order
   - quantity
   - urgency
   - emergency flag
   - notes
4. Submit.

System result:

- purchase request is created
- status becomes `Pending officer`
- procurement users are notified

### 14. Procurement officer processing

Used by: `Procurement Officer`, `Super Admin`

1. Open `Procurement -> Requests`.
2. Review requests.
3. Open a request for update.
4. Enter:
   - supplier
   - unit price
   - status
5. Save.

System statuses include:

- `Pending officer`
- `Ordered`
- `Received / stock updated`
- `Cancelled`

### 15. Procurement receiving

Used by: `Manager`, `Procurement Officer`, `Super Admin`

1. Find a request with status `Ordered`.
2. Click `Receive`.

System result:

- stock-in is performed automatically
- purchase request status becomes `Received / stock updated`

### 16. Preventive maintenance

Used by: `Manager`, `Super Admin`

1. Open `Assets -> Preventive`.
2. Click `New schedule`.
3. Define:
   - machine
   - task title
   - frequency in days
   - checklist
   - next due date/time
   - active status
4. Save schedule.
5. When needed, click `Create PM WO`.

System result:

- preventive work order is created
- PM appears in normal work-order execution flow
- overdue PM can appear in dashboard and notifications

### 17. Reusable tools

Assignment used by: `Manager`, `Super Admin`  
Return used by: `Operator`, `Technician`, `Manager`, `Super Admin`

Assignment flow:

1. Open `Assets -> Tools`.
2. Assign tool by:
   - selecting from list
   - or scanning / entering tool code
3. Choose assignee.
4. Click `Assign`.

System result:

- tool status changes to `In use`
- assignment record is created

Return flow:

1. Open the active assignment.
2. Click `Return`.
3. Select return condition:
   - `Good`
   - `Damaged`
   - `Lost`
4. Save.

System result:

- return timestamp is stored
- tool status updates:
  - `Good` -> `Available`
  - `Damaged` -> `Out of service`
  - `Lost` -> `Out of service`

### 18. External repairs

Create used by: `Manager`, `Super Admin`  
Officer update used by: `Procurement Officer`, `Super Admin`  
Acceptance used by: `Manager`, `Super Admin`

1. Open `Assets -> External repairs`.
2. Manager creates a repair request.
3. Enter:
   - title
   - description
   - optional linked work order
   - estimated cost
4. Save.
5. Procurement opens the repair order and updates:
   - vendor
   - actual cost
   - repair status
6. When vendor returns the item, status is set to `Returned`.
7. Manager opens acceptance page.
8. Manager verifies and closes it.

System result:

- external repair history is maintained
- vendor and cost data are stored
- manager acceptance is tracked

### 19. Quick maintenance log

Used by: `Operator`, `Supervisor`, `Technician`, `Manager`, `Super Admin`

1. Open `Quick log`.
2. Select machine.
3. Enter summary and optional details.
4. Save.

System result:

- ad hoc maintenance note is recorded
- no issue or work order is required

### 20. Notifications

Used by: all authenticated users

Notifications can include:

- new issue reported
- issue validated
- technician assignment
- emergency work order
- work order pending manager review
- low stock
- procurement request
- overdue preventive maintenance
- repair returned from vendor

Users can:

- open linked items
- mark one notification as read
- mark all notifications as read

### 21. Reports and KPIs

Used by: `Supervisor`, `Technician`, `Manager`, `Procurement Officer`, `Super Admin`

Reports include:

- machines by issue count
- low-stock items
- most used spare parts
- work-order status distribution
- technician throughput

KPI dashboard includes:

- repair time sample
- PM compliance indicator
- repeat failure visibility
- open emergency work-order count

### 22. User management

Used by: `Super Admin`, Django `superuser`

1. Open `System -> Users`.
2. View all users.
3. Create new user or edit existing user.
4. Update:
   - username
   - role
   - active status
   - staff flag
   - password
5. Delete or deactivate when appropriate.

Safeguards:

- user cannot delete self
- user cannot deactivate self
- last active super admin cannot be removed

## Status Reference

### Maintenance issue statuses

- `New`
- `Validated`
- `Converted to work order`

### Work order statuses

- `Approved`
- `Assigned`
- `In progress`
- `Paused`
- `Waiting for vendor`
- `Pending parts`
- `Pending manager review`
- `Closed`

### Purchase request statuses

- `Pending officer`
- `Ordered`
- `Received / stock updated`
- `Cancelled`

### Tool statuses

- `Available`
- `In use`
- `Out of service`

### External repair statuses

- `Draft`
- `Sent to vendor`
- `Returned`
- `Closed / accepted`
- `Rejected / re-repair`

## QR Scanning Notes

The system supports three ways to process QR codes:

- live camera scanning
- upload QR image
- manual code entry

If the browser supports native live QR reading, the page uses it directly. If not, the system falls back to server-assisted scanning using Django and OpenCV. This allows the QR page to work in more browsers even though the project uses standard Django HTML templates.

## Local Setup

### Requirements

- Python 3.x
- Django project dependencies from `requirements.txt`

### Run locally

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open:

- `http://127.0.0.1:8000/`

### Optional demo data

If you want to populate sample data for demonstration, use the existing management commands available in the project as needed.

## Client Summary

This system gives the client one connected maintenance platform where:

- every machine issue can be reported and tracked
- QR code intake speeds up reporting
- maintenance work orders follow approval and review flow
- technicians work from a visible queue
- spare parts are linked to actual maintenance jobs
- shortages automatically trigger procurement requests
- tools and external repairs are traceable
- PM, reports, KPIs, and notifications support management decisions

In short, it converts maintenance operations from manual coordination into a structured, auditable workflow.
