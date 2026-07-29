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
    path("", include("inventory.urls")),  # stock-in routes (inventory app)
    path("", include("maintenance.urls")),
    path("home/", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
    # NOTE: Media files are NOT served by Django. In production they are
    # served by the configured CDN (AWS_S3_CUSTOM_DOMAIN) via the S3
    # storage backend (see mms/settings.py STORAGES). In dev with no
    # bucket env vars set, FileSystemStorage is used and the developer
    # is expected to run a local web server that serves /media/ from
    # MEDIA_ROOT, or to set MMS_MEDIA_BUCKET for local S3 testing.
]
