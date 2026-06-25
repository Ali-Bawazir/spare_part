from django.urls import path

from . import views

urlpatterns = [
    # Purchase Requests (PR)
    path("", views.purchase_list, name="purchase_list"),                    # /procurement/ → PR list
    path("new/", views.purchase_request_create, name="purchase_create"),    # /procurement/new/ → Create PR
    path("pr/<int:pk>/", views.purchase_request_detail, name="pr_detail"),  # /procurement/pr/1/ → PR detail
    path("pr/<int:pk>/officer/", views.purchase_officer, name="purchase_officer"),  # /procurement/pr/1/officer/
    path("pr/<int:pk>/add-voice/", views.purchase_request_add_voice, name="purchase_request_add_voice"),  # /procurement/pr/1/add-voice/


    # Purchase Orders (PO) — separate section
    path("purchase-orders/", views.purchase_order_list, name="purchase_order_list"),   # /procurement/purchase-orders/
    path("purchase-orders/by-supplier/", views.purchase_order_by_supplier, name="purchase_order_by_supplier"),  # /procurement/purchase-orders/by-supplier/
    path("purchase-orders/supplier/<int:supplier_id>/csv/", views.purchase_order_supplier_csv, name="purchase_order_supplier_csv"),  # /procurement/purchase-orders/supplier/1/csv/
    path("purchase-orders/new/", views.purchase_order_create, name="purchase_order_create"),  # /procurement/purchase-orders/new/
    path("purchase-orders/from-pr/<int:pr_pk>/", views.purchase_order_create_from_pr, name="purchase_order_create_from_pr"),
    path("purchase-orders/<int:pk>/", views.purchase_order_detail, name="purchase_order_detail"),
    path("purchase-orders/<int:pk>/receive/", views.purchase_order_receive, name="purchase_order_receive"),
    path("purchase-orders/<int:pk>/close-short/", views.purchase_order_close_short, name="purchase_order_close_short"),
    path("purchase-orders/<int:pk>/pdf/", views.purchase_order_pdf, name="purchase_order_pdf"),
    path("supplier-analytics/", views.supplier_analytics, name="supplier_analytics"),
    path("supplier/quick-create/", views.supplier_quick_create, name="supplier_quick_create"),
]