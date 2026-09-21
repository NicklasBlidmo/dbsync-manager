"""
URL configuration.

Slice 1 (design §14): the Django admin is the whole UI. The HTMX pages of
§9 are added in later slices under their own app.
"""

from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("healthz", views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
]

admin.site.site_header = "dbsync-manager"
admin.site.site_title = "dbsync-manager"
admin.site.index_title = "Registry"
