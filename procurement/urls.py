from django.urls import path

from . import views

urlpatterns = [
    # Purchase Requests (PR)
    path("", views.purchase_list, name="purchase_list"),                    # /procurement/ → PR list
    path("new/", views.purchase_request_create, name="purchase_create"),    # /procurement/new/ → Create PR
    path("pr/<int:pk>/", views.purchase_request_detail, name="pr_detail"),  # /procurement/pr/1/ → PR detail
    path("pr/<int:pk>/officer/", views.purchase_officer, name="purchase_officer"),  # /procurement/pr/1/officer/
    path("pr/<int:pk>/receive/", views.purchase_receive, name="purchase_receive"),  # /procurement/pr/1/receive/

    # Purchase Orders (PO) — separate section
    path("purchase-orders/", views.purchase_order_list, name="purchase_order_list"),   # /procurement/purchase-orders/
    path("purchase-orders/new/", views.purchase_order_create, name="purchase_order_create"),  # /procurement/purchase-orders/new/
    path("purchase-orders/from-pr/<int:pr_pk>/", views.purchase_order_create_from_pr, name="purchase_order_create_from_pr"),
    path("purchase-orders/<int:pk>/", views.purchase_order_detail, name="purchase_order_detail"),
    path("purchase-orders/<int:pk>/receive/", views.purchase_order_receive, name="purchase_order_receive"),
    path("purchase-orders/<int:pk>/close-short/", views.purchase_order_close_short, name="purchase_order_close_short"),
]