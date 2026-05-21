from django.contrib import admin

from mms.admin_mixins import MMSAdminPermission

from .models import PartIssueLine, SparePart, StockMovement


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
        "created_at",
    )
    list_filter = ("is_consumable", "is_repairable")
    search_fields = ("sku", "name", "description")
    readonly_fields = ("created_at",)
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
    list_display = ("work_order", "part", "quantity", "unit_cost", "issued_by", "created_at")
    list_filter = ("part",)
    search_fields = ("work_order__number", "part__sku", "part__name", "issued_by__username")
    readonly_fields = ("created_at",)
    raw_id_fields = ("work_order", "part", "issued_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)