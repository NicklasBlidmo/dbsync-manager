from django.urls import path

from . import views

urlpatterns = [
    path("projects/", views.projects, name="api_projects"),
    path("projects/<int:pk>/", views.project, name="api_project"),
    path("projects/<int:pk>/verify/", views.verify, name="api_project_verify"),
    path("operations/<int:pk>/", views.operation, name="api_operation"),
]
