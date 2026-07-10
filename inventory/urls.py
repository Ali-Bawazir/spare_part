from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("stock/in/", views.stock_in_view, name="stock_in"),
    path("stock/<int:pk>/stock-in/", views.part_stock_in, name="part_stock_in"),
]