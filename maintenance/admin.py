from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from inventory.models import PartIssueLine
from mms.admin_mixins import MMSAdminPermission

from .models import (
    AuditEntry,
    ExternalRepairOrder,
    ExternalRepairRequest,
    FailureCategory,
    FailureMode,
    Incident,
    Machine,
    MaintenanceIssue,
    Notification,
    PMChecklistItem,
    PMExecution,
    PMSchedule,
    PMTemplate,
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
    list_display = ("name", "asset_code", "asset_level", "asset_type", "location", "is_active", "created_at")
    list_filter = ("is_active", "asset_level", "asset_type", "status")
    search_fields = ("name", "qr_code", "asset_code", "serial_number", "location")
    readonly_fields = ("created_at",)
    ordering = ("asset_code", "name")
    list_select_related = ("parent",)


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
        "lifecycle_status",
        "category",
        "is_emergency",
        "assigned_technician",
        "created_by",
        "created_at",
    )
    list_filter = ("lifecycle_status", "category", "is_emergency", "machine")
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
            _("Identification"),
            {"fields": ("number", "category", "is_emergency", "issue", "machine")},
        ),
        (_("Assignment"), {"fields": ("lifecycle_status", "assigned_technician", "created_by")}),
        (
            _("Execution"),
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
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
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
    list_display = ("template", "machine", "frequency_type", "interval", "next_due_at", "is_active", "created_at")
    list_filter = ("is_active", "machine", "frequency_type", "trigger_type")
    search_fields = ("template__code", "template__title", "machine__name")
    readonly_fields = ("created_at", "effective_priority", "effective_duration_minutes")
    raw_id_fields = ("template", "machine", "component", "created_by")


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
        (_("Vendor & cost"), {"fields": ("vendor_name", "estimated_cost", "actual_cost")}),
        (_("People & dates"), {"fields": ("created_by", "handled_by", "sent_at", "closed_at", "created_at")}),
    )


@admin.register(ExternalRepairRequest)
class ExternalRepairRequestAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "work_order",
        "status",
        "requested_by",
        "reviewed_by",
        "repair_order",
        "created_at",
        "reviewed_at",
    )
    list_filter = ("status",)
    search_fields = (
        "diagnosis_note",
        "part_description",
        "manager_note",
        "requested_by__username",
        "work_order__number",
    )
    readonly_fields = ("created_at", "reviewed_at")
    raw_id_fields = ("work_order", "requested_by", "reviewed_by", "repair_order")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    fieldsets = (
        (None, {"fields": ("work_order", "status", "repair_order")}),
        (_("Request"), {"fields": ("requested_by", "diagnosis_note", "part_description")}),
        (_("Review"), {"fields": ("reviewed_by", "reviewed_at", "manager_note")}),
        (_("Audit"), {"fields": ("created_at",)}),
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


class PMChecklistItemInline(admin.TabularInline):
    model = PMChecklistItem
    extra = 1
    fields = ("order", "text", "is_required")


@admin.register(PMTemplate)
class PMTemplateAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("code", "title", "priority", "estimated_duration_minutes", "is_active", "created_at")
    list_filter = ("is_active", "priority", "requires_manager_review")
    search_fields = ("code", "title", "description")
    readonly_fields = ("created_at",)
    inlines = (PMChecklistItemInline,)


@admin.register(PMExecution)
class PMExecutionAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("pm_schedule", "execution_sequence", "status", "scheduled_due_at", "completed_at", "approved_at")
    list_filter = ("status",)
    search_fields = ("pm_schedule__template__code", "pm_schedule__machine__name")
    readonly_fields = ("created_at", "template_snapshot_json", "scheduled_due_at", "execution_sequence")
    raw_id_fields = ("pm_schedule", "work_order", "completed_by", "approved_by")

    def has_add_permission(self, request):
        return False