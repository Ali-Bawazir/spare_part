from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.conf import settings
from django.views.static import serve

from mms.health import health

admin.site.site_header = "Factory Maintenance & Spare Parts Management System"
admin.site.site_title = "MMS Admin"
admin.site.index_title = "Operations & master data"


# Custom LoginView that surfaces django-axes' cool-off / perma-lock
# messages to the user. django-axes sets ``request.axes_locked_out``
# when an account/IP is blocked; Django's default LoginView ignores
# this flag and renders a generic "Invalid username or password"
# error, which hides the lockout state. We override form_invalid to
# append axes' cool-off message to the response when the flag is set.
class AxesAwareLoginView(auth_views.LoginView):
    def form_invalid(self, form):
        # If axes flagged this attempt as locked, append its cool-off
        # message to the form BEFORE rendering so the message survives
        # into the template. Setting it on the already-rendered response
        # is too late — the context dict has already been snapshotted.
        if getattr(self.request, "axes_locked_out", False):
            from axes.conf import settings as ax_settings
            form.add_error(None, ax_settings.AXES_COOLOFF_MESSAGE)
        return super().form_invalid(form)


# Explicit auth URL list. Intentionally minimal:
#   - login / logout only
#   - no password_reset / password_change (no SMTP; Super Admin manages
#     passwords via /users/<id>/edit/)
# No namespace: keeps name="login" and name="logout" so reverse('login')
# works everywhere (especially in the idle-timeout middleware).
auth_urlpatterns = [
    path("login/",  AxesAwareLoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]

urlpatterns = [
    # Health endpoint — must come early so compose healthcheck (and any uptime
    # monitor) hits a lightweight handler with no DB-heavy middleware.
    path("health/", health, name="health"),

    path("i18n/", include("django.conf.urls.i18n")),
    path("admin/", admin.site.urls),
    path("accounts/", include(auth_urlpatterns)),
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
