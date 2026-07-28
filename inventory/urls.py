from django.urls import path

from . import views

# No `app_name` set: the urls are included from mms.urls without a
# `namespace=` argument, so existing templates that use
# `{% url 'stock_in' %}` (without an "inventory:" prefix) keep working.
# If we ever want to namespace these, every template must add the
# `inventory:` prefix in the same commit.

urlpatterns = [
    path("stock/in/", views.stock_in_view, name="stock_in"),
    path("stock/<int:pk>/stock-in/", views.part_stock_in, name="part_stock_in"),
]