from django.contrib import admin

from mms.admin_mixins import MMSAdminPermission

from .models import Inventory, PartIssueLine, SparePart, StockMovement


@admin.register(Inventory)
class InventoryAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "part",
        "site",
        "quantity_available",
        "quantity_reserved",
        "rack_location",
        "last_counted_at",
        "last_counted_by",
    )
    list_filter = ("site", "part__is_consumable")
    search_fields = ("part__sku", "part__name", "rack_location")
    readonly_fields = (
        "quantity_available",
        "quantity_reserved",
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
        ("Identification", {"fields": ("sku", "name", "description", "qr_code")}),
        ("Classification", {"fields": ("category", "unit", "is_consumable", "is_repairable", "status")}),
        ("Stock levels", {"fields": ("quantity_on_hand", "min_stock_level", "max_stock_level")}),
        ("Procurement", {"fields": ("supplier", "avg_cost", "last_purchase_cost")}),
        ("Audit", {"fields": ("created_at",), "classes": ("collapse",)}),
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
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("work_order", "part", "issued_by", "requested_by", "approved_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fieldsets = (
        ("Identification", {"fields": ("work_order", "part", "status")}),
        ("Quantities", {"fields": ("quantity", "unit_cost", "invoice_ref", "supplier_name")}),
        ("People", {"fields": ("requested_by", "issued_by", "approved_by", "approved_at")}),
        ("Decision", {"fields": ("rejection_reason", "is_emergency_auto_approved")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )