"""
URL configuration.

The HTMX pages of design §9 live in the `ui` app; the Django admin stays
for credentials, hooks and users (Settings in §9) and as the add form until
the wizard (slice 4).
"""

from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("ui.urls")),
]

admin.site.site_header = "dbsync-manager"
admin.site.site_title = "dbsync-manager"
admin.site.index_title = "Registry"
