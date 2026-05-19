from django.urls import path

from . import views

urlpatterns = [
    path("", views.purchase_list, name="purchase_list"),
    path("new/", views.purchase_request_create, name="purchase_create"),
    path("<int:pk>/", views.purchase_officer, name="purchase_officer"),
    path("<int:pk>/receive/", views.purchase_receive, name="purchase_receive"),
]
