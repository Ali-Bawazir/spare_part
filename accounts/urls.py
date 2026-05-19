from django.urls import path

from . import views

urlpatterns = [
    path("", views.mms_user_list, name="mms_user_list"),
    path("create/", views.mms_user_create, name="mms_user_create"),
    path("<int:pk>/edit/", views.mms_user_edit, name="mms_user_edit"),
    path("<int:pk>/delete/", views.mms_user_delete, name="mms_user_delete"),
    path("<int:pk>/deactivate/", views.mms_user_deactivate, name="mms_user_deactivate"),
]
