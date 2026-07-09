from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.conf import settings
from django.views.static import serve

from mms.health import health

admin.site.site_header = "Factory Maintenance & Spare Parts Management System"
admin.site.site_title = "MMS Admin"
admin.site.index_title = "Operations & master data"

urlpatterns = [
    # Health endpoint — must come early so compose healthcheck (and any uptime
    # monitor) hits a lightweight handler with no DB-heavy middleware.
    path("health/", health, name="health"),

    path("i18n/", include("django.conf.urls.i18n")),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("users/", include("accounts.urls")),
    path("procurement/", include("procurement.urls")),
    path("", include("maintenance.urls")),
    path("home/", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
    # Serve media files (works in both DEBUG and production)
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
