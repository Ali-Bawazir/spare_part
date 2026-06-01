from django.contrib import admin

from inventory.models import PartIssueLine
from mms.admin_mixins import MMSAdminPermission

from .models import (
    AuditEntry,
    ExternalRepairOrder,
    FailureCategory,
    FailureMode,
    Incident,
    Machine,
    MaintenanceIssue,
    Notification,
    PMSchedule,
    QuickMaintenanceLog,
    Tool,
    ToolAssignment,
    WorkOrder,
    WorkOrderStateLog,
)


class WorkOrderStateLogInline(admin.TabularInline):
    model = WorkOrderStateLog
    extra = 0
    can_delete = False
    ordering = ("created_at",)
    readonly_fields = ("from_status", "to_status", "actor", "note", "created_at")


class PartIssueLineInline(admin.TabularInline):
    model = PartIssueLine
    extra = 0
    can_delete = False
    readonly_fields = ("part", "quantity", "unit_cost", "invoice_ref", "supplier_name", "issued_by", "created_at")


@admin.register(FailureCategory)
class FailureCategoryAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    ordering = ("name",)


@admin.register(FailureMode)
class FailureModeAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("code", "name", "category", "is_active", "created_at")
    list_filter = ("is_active", "category")
    search_fields = ("code", "name", "description")
    ordering = ("code",)
    raw_id_fields = ("category",)


@admin.register(Machine)
class MachineAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("name", "qr_code", "location", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "qr_code", "location")
    readonly_fields = ("created_at",)
    ordering = ("name",)


@admin.register(MaintenanceIssue)
class MaintenanceIssueAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "id",
        "machine",
        "issue_type",
        "priority",
        "status",
        "reported_by",
        "validated_by",
        "created_at",
    )
    list_filter = ("issue_type", "priority", "status", "machine__site")
    search_fields = ("description", "machine__name", "machine__qr_code", "reported_by__username")
    readonly_fields = ("created_at", "validated_at")
    raw_id_fields = ("reported_by", "validated_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)


@admin.register(WorkOrder)
class WorkOrderAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "number",
        "machine",
        "status",
        "category",
        "is_emergency",
        "assigned_technician",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "category", "is_emergency", "machine")
    search_fields = (
        "number",
        "machine__name",
        "machine__qr_code",
        "assigned_technician__username",
        "created_by__username",
        "issue__id",
    )
    readonly_fields = (
        "number",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("issue", "machine", "assigned_technician", "created_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    inlines = (WorkOrderStateLogInline, PartIssueLineInline)

    fieldsets = (
        (
            "Identification",
            {"fields": ("number", "category", "is_emergency", "issue", "machine")},
        ),
        ("Assignment", {"fields": ("status", "assigned_technician", "created_by")}),
        (
            "Execution",
            {
                "fields": (
                    "root_cause",
                    "action_taken",
                    "notes",
                    "labor_started_at",
                    "labor_stopped_at",
                    "downtime_started_at",
                    "downtime_ended_at",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(WorkOrderStateLog)
class WorkOrderStateLogAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("work_order", "from_status", "to_status", "actor", "created_at")
    list_filter = ("to_status",)
    search_fields = ("work_order__number", "note", "actor__username")
    readonly_fields = ("work_order", "from_status", "to_status", "actor", "note", "created_at")
    raw_id_fields = ("work_order", "actor")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False


@admin.register(QuickMaintenanceLog)
class QuickMaintenanceLogAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("machine", "author", "summary", "created_at")
    list_filter = ("machine",)
    search_fields = ("summary", "details", "machine__name", "author__username")
    readonly_fields = ("created_at",)
    raw_id_fields = ("machine", "author")
    date_hierarchy = "created_at"


@admin.register(PMSchedule)
class PMScheduleAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("title", "machine", "frequency_days", "next_due_at", "is_active", "created_at")
    list_filter = ("is_active", "machine")
    search_fields = ("title", "machine__name", "checklist")
    readonly_fields = ("created_at",)
    raw_id_fields = ("machine",)


@admin.register(Tool)
class ToolAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("name", "code", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "code")
    readonly_fields = ("created_at",)


@admin.register(ToolAssignment)
class ToolAssignmentAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("tool", "user", "assigned_at", "returned_at", "return_condition")
    list_filter = ("return_condition",)
    search_fields = ("tool__name", "tool__code", "user__username")
    raw_id_fields = ("tool", "user", "assigned_by")


@admin.register(Incident)
class IncidentAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("title", "status", "reported_by", "tool", "created_at", "resolved_at")
    list_filter = ("status",)
    search_fields = ("title", "description", "reported_by__username")
    raw_id_fields = ("reported_by", "tool", "work_order")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)


@admin.register(Notification)
class NotificationAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("title", "kind", "recipient", "read_at", "created_at")
    list_filter = ("kind", "read_at")
    search_fields = ("title", "body", "recipient__username")
    readonly_fields = ("recipient", "kind", "title", "body", "link", "created_at")
    raw_id_fields = ("recipient",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False


@admin.register(ExternalRepairOrder)
class ExternalRepairOrderAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "title",
        "status",
        "vendor_name",
        "work_order",
        "estimated_cost",
        "actual_cost",
        "created_by",
        "handled_by",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("title", "description", "vendor_name", "created_by__username")
    readonly_fields = ("created_at",)
    raw_id_fields = ("work_order", "created_by", "handled_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    fieldsets = (
        (None, {"fields": ("title", "description", "status", "work_order")}),
        ("Vendor & cost", {"fields": ("vendor_name", "estimated_cost", "actual_cost")}),
        ("People & dates", {"fields": ("created_by", "handled_by", "sent_at", "closed_at", "created_at")}),
    )


@admin.register(AuditEntry)
class AuditEntryAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("action", "entity", "object_id", "actor", "created_at")
    list_filter = ("action", "entity")
    search_fields = ("action", "entity", "object_id", "actor__username")
    readonly_fields = ("actor", "action", "entity", "object_id", "payload", "created_at")
    raw_id_fields = ("actor",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser