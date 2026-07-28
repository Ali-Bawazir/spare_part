from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from mms.admin_mixins import MMSAdminPermission

from .models import Inventory, PartIssueLine, ReusableToolInstance, SparePart, StockMovement
from .models_tools import ToolAssignment, ToolDamageReport, ToolMovement


@admin.register(Inventory)
class InventoryAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "part",
        "site",
        "quantity_available",
        "rack_location",
        "last_counted_at",
        "last_counted_by",
    )
    list_filter = ("site", "part__is_consumable")
    search_fields = ("part__sku", "part__name", "rack_location")
    readonly_fields = (
        "quantity_available",
        "last_counted_by",
        "updated_at",
    )
    raw_id_fields = ("part", "site", "last_counted_by")
    ordering = ("part__name", "site__name")

    def has_add_permission(self, request):
        return True

    def save_model(self, request, obj, form, change):
        if obj.last_counted_at and not obj.last_counted_by:
            obj.last_counted_by = request.user
        super().save_model(request, obj, form, change)


class StockMovementInline(admin.TabularInline):
    model = StockMovement
    fk_name = "part"
    extra = 0
    can_delete = False
    ordering = ("-created_at",)

    readonly_fields = (
        "movement_type",
        "quantity",
        "work_order",
        "performed_by",
        "supplier_name",
        "unit_cost",
        "invoice_ref",
        "note",
        "created_at",
    )
    raw_id_fields = ("work_order", "performed_by")
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SparePart)
class SparePartAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "sku",
        "name",
        "quantity_on_hand",
        "min_stock_level",
        "is_consumable",
        "is_repairable",
        "category",
        "unit",
        "supplier",
        "avg_cost",
        "status",
        "created_at",
    )
    list_filter = ("is_consumable", "is_repairable", "supplier", "status")
    search_fields = ("sku", "name", "description")
    fieldsets = (
        (_("Identification"), {"fields": ("sku", "name", "description", "qr_code")}),
        (_("Classification"), {"fields": ("category", "unit", "is_consumable", "is_repairable", "status")}),
        (_("Stock levels"), {"fields": ("quantity_on_hand", "min_stock_level", "max_stock_level")}),
        (_("Procurement"), {"fields": ("supplier", "avg_cost", "last_purchase_cost")}),
        (_("Audit"), {"fields": ("created_at",), "classes": ("collapse",)}),
    )
    readonly_fields = ("created_at", "qr_code", "quantity_on_hand")
    ordering = ("name",)
    inlines = (StockMovementInline,)

    def get_inline_instances(self, request, obj=None):
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)


@admin.register(StockMovement)
class StockMovementAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "part",
        "movement_type",
        "quantity",
        "work_order",
        "performed_by",
        "supplier_name",
        "unit_cost",
        "created_at",
    )
    list_filter = ("movement_type", "part")
    search_fields = (
        "part__sku",
        "part__name",
        "note",
        "invoice_ref",
        "supplier_name",
        "performed_by__username",
    )
    readonly_fields = ("created_at",)
    raw_id_fields = ("part", "work_order", "performed_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)


@admin.register(PartIssueLine)
class PartIssueLineAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "work_order", "part", "quantity", "status", "unit_cost",
        "requested_by", "issued_by", "approved_by", "is_emergency_auto_approved",
        "created_at",
    )
    list_filter = ("status", "is_emergency_auto_approved", "part")
    search_fields = (
        "work_order__number", "part__sku", "part__name",
        "issued_by__username", "requested_by__username", "approved_by__username",
    )
    readonly_fields = ("created_at", "updated_at", "status")
    raw_id_fields = ("work_order", "part", "issued_by", "requested_by", "approved_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fieldsets = (
        (_("Identification"), {"fields": ("work_order", "part", "status")}),
        (_("Quantities"), {"fields": ("quantity", "unit_cost", "invoice_ref", "supplier_name")}),
        (_("People"), {"fields": ("requested_by", "issued_by", "approved_by", "approved_at")}),
        (_("Decision"), {"fields": ("rejection_reason", "is_emergency_auto_approved")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )


@admin.register(ReusableToolInstance)
class ReusableToolInstanceAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("display_name", "status", "is_active", "created_at")
    list_filter = ("status", "is_active", "part")
    search_fields = ("part__sku", "part__name", "tool_number")
    raw_id_fields = ("part", "source_stock_movement")
    readonly_fields = ("created_at", "status")
    ordering = ("part__name", "tool_number")


@admin.register(ToolAssignment)
class ToolAssignmentAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("instance", "operator", "machine", "checkout_at", "return_at", "condition_in")
    list_filter = ("condition_out", "condition_in")
    search_fields = ("instance__part__name", "instance__tool_number", "operator__username")
    raw_id_fields = ("instance", "operator", "machine")
    date_hierarchy = "checkout_at"
    ordering = ("-checkout_at",)


@admin.register(ToolDamageReport)
class ToolDamageReportAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("id", "instance", "status", "reported_by", "damage_date", "repair_cost", "resolved_by")
    list_filter = ("status",)
    search_fields = ("instance__part__name", "instance__tool_number", "reason", "reported_by__username")
    raw_id_fields = ("instance", "reported_by", "machine", "assignment", "resolved_by")
    date_hierarchy = "damage_date"
    ordering = ("-damage_date",)


@admin.register(ToolMovement)
class ToolMovementAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("instance", "movement_type", "actor", "machine", "created_at")
    list_filter = ("movement_type",)
    search_fields = ("instance__part__name", "instance__tool_number", "actor__username", "note")
    raw_id_fields = ("instance", "actor", "machine", "assignment", "damage_report")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    def has_add_permission(self, request):
        return False