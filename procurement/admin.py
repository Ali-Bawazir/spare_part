from django.contrib import admin
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from mms.admin_mixins import MMSAdminPermission

from .models import PurchaseRequest, Supplier


@admin.register(Supplier)
class SupplierAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "contact_person",
        "phone",
        "email",
        "is_active",
        "supplier_type",
        "created_at",
    )
    list_filter = ("is_active", "supplier_type")
    search_fields = ("code", "name", "contact_person", "phone", "email", "notes")
    readonly_fields = ("created_at", "is_repair_vendor", "qr_code_preview")
    ordering = ("name",)
    fieldsets = (
        (_("Identification"), {
            "fields": ("code", "name", "is_active"),
        }),
        (_("Contact"), {
            "fields": ("contact_person", "phone", "email", "address"),
        }),
        (_("Vendor type"), {
            "fields": ("supplier_type", "is_repair_vendor"),
            "description": _(
                "supplier_type is the canonical field (Parts supplier / "
                "Repair vendor). is_repair_vendor is auto-synced."
            ),
        }),
        (_("Notes"), {
            "fields": ("notes",),
        }),
        (_("Audit"), {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )

    def qr_code_preview(self, obj):
        if obj and obj.code:
            from inventory.qr_utils import get_supplier_qr_url
            url = get_supplier_qr_url(obj.code)
            return mark_safe(f'<img src="{url}" width="120" height="120" style="border:1px solid #ccc;border-radius:8px"/>')
        return "-"
    qr_code_preview.short_description = _("QR Code")


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(MMSAdminPermission, admin.ModelAdmin):
    list_display = (
        "id",
        "part",
        "quantity",
        "status",
        "work_order",
        "supplier",
        "unit_price",
        "created_by",
        "handled_by",
        "created_at",
    )
    list_filter = ("status",)
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
            _("Request"),
            {
                "fields": (
                    "part",
                    "quantity",
                    "notes",
                    "work_order",
                    "status",
                )
            },
        ),
        (_("Procurement"), {"fields": ("supplier", "unit_price", "handled_by")}),
        (_("Audit"), {"fields": ("created_by", "created_at", "updated_at")}),
    )