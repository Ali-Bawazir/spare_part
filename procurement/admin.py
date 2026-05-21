from django.contrib import admin

from mms.admin_mixins import MMSAdminPermission

from .models import PurchaseRequest, Supplier


@admin.register(Supplier)
class SupplierAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = ("name", "is_repair_vendor", "contact", "created_at")
    list_filter = ("is_repair_vendor",)
    search_fields = ("name", "contact", "notes")
    readonly_fields = ("created_at",)
    ordering = ("name",)


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "id",
        "part",
        "quantity",
        "status",
        "is_emergency",
        "urgency",
        "work_order",
        "supplier",
        "unit_price",
        "created_by",
        "handled_by",
        "created_at",
    )
    list_filter = ("status", "is_emergency", "urgency")
    search_fields = (
        "notes",
        "part__sku",
        "part__name",
        "created_by__username",
        "handled_by__username",
    )
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("part", "work_order", "created_by", "handled_by")
    autocomplete_fields = ("supplier",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    fieldsets = (
        (
            "Request",
            {
                "fields": (
                    "part",
                    "quantity",
                    "urgency",
                    "is_emergency",
                    "notes",
                    "work_order",
                    "status",
                )
            },
        ),
        ("Procurement", {"fields": ("supplier", "unit_price", "handled_by")}),
        ("Audit", {"fields": ("created_by", "created_at", "updated_at")}),
    )