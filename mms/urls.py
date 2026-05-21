from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static

admin.site.site_header = "Factory Maintenance & Spare Parts Management System"
admin.site.site_title = "MMS Admin"
admin.site.index_title = "Operations & master data"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("users/", include("accounts.urls")),
    path("procurement/", include("procurement.urls")),
    path("", include("maintenance.urls")),
    path("home/", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
]

# Serve media files in development
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
