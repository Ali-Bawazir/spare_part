# End-to-End Scenarios

Comprehensive scenario walkthroughs for the full MMS system.

## Scenarios

| Scenario | Module coverage | Doc |
|---|---|---|
| A: Issue → WO → close | Issues, Work Orders, Parts | [scenarios/full_e2e_scenarios.md](full_e2e_scenarios.md#scenario-a--operator-reports-an-issue--maintenance-cycle) |
| B: Procurement cycle | Stock, PR, PO, Receive | [scenarios/full_e2e_scenarios.md](full_e2e_scenarios.md#scenario-b--procurement-cycle) |
| C: Vendor repair | ERO request, ERO, accept | [scenarios/full_e2e_scenarios.md](full_e2e_scenarios.md#scenario-c--vendor-repair-external-repair-order) |
| D: Shortage flow | Shortage, PR, PO, blocker | [scenarios/full_e2e_scenarios.md](full_e2e_scenarios.md#scenario-d--shortage-flow-part-not-in-stock) |
| E: Grand tour (cross-functional) | All modules | [scenarios/full_e2e_scenarios.md](full_e2e_scenarios.md#scenario-e--cross-functional-full-issue--wo--shortage--pr--po--receive--issue--close) |
| F: Operator self-service consumable | Consumables, stock | [scenarios/full_e2e_scenarios.md](full_e2e_scenarios.md#scenario-f--operator-self-service-consumable) |
| PM cycle (7 stages) | PMs only | [scenarios/pm_e2e_responsibilities.md](pm_e2e_responsibilities.md) |
| PM Work Order page (technician) | PM execute + checklist | [scenarios/pm_work_order_page.md](pm_work_order_page.md) |
| PM how-to (action-oriented) | Create → spawn → execute → review | [scenarios/pm_how_to.md](pm_how_to.md) |
| PM walkthrough (setup + steps) | PMs only | [scenarios/pm_walkthrough.md](pm_walkthrough.md) |

## State Machines

See [full_e2e_scenarios.md](full_e2e_scenarios.md#state-machines) for:
- WorkOrder.LifecycleStatus
- PartIssueLine.Status
- ExternalRepairOrder.Status
- PurchaseOrder.Status
- PartShortageReport.Status
- PMExecution.Status

## Data Flow

See [full_e2e_scenarios.md](full_e2e_scenarios.md#data-flow-diagrams-textual) for object graph + cost aggregation.

## Quick Reset

Before running any scenario, reset transactional data:

```bash
cd /Users/alsmb/projects/sparepart/spare_part
DJANGO_SETTINGS_MODULE=mms.settings /Applications/Xcode.app/Contents/Developer/usr/bin/python3 manage.py shell -c "
from maintenance.models import *
from inventory.models import *
from procurement.models import *
MaintenanceIssue.objects.all().delete()
WorkOrder.objects.all().delete()
PartIssueLine.objects.all().delete()
InventoryReservation.objects.all().delete()
PartShortageReport.objects.all().delete()
ExternalRepairRequest.objects.all().delete()
ExternalRepairOrder.objects.all().delete()
PurchaseRequest.objects.all().delete()
PurchaseOrder.objects.all().delete()
StockMovement.objects.all().delete()
Notification.objects.all().delete()
print('Reset complete.')
"
```