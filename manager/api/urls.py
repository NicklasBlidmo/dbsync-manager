from django.urls import path

from . import views

urlpatterns = [
    path("projects/", views.projects, name="api_projects"),
    path("projects/<int:pk>/", views.project, name="api_project"),
    path("projects/<int:pk>/verify/", views.verify, name="api_project_verify"),
    path("projects/<int:pk>/conflicts/", views.conflicts, name="api_project_conflicts"),
    path("projects/<int:pk>/conflicts/<str:name>/", views.conflict_file, name="api_project_conflict_file"),
    path("operations/<int:pk>/", views.operation, name="api_operation"),
]
