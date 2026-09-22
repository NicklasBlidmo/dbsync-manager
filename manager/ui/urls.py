from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("projects/table/", views.projects_table, name="projects_table"),
    path("projects/<int:pk>/", views.project_detail, name="project"),
    path("projects/<int:pk>/status/", views.project_status, name="project_status"),
    path("projects/<int:pk>/log/", views.project_log, name="project_log"),
    path("projects/<int:pk>/action/<slug:action>/", views.project_action, name="project_action"),
    path("add/", views.add_start, name="add_start"),
    path("add/workspaces/", views.add_workspaces, name="add_workspaces"),
    path("add/search/", views.add_search, name="add_search"),
    path("add/project/", views.add_project, name="add_project"),
    path("add/names/", views.add_names, name="add_names"),
    path("add/preview/", views.add_preview, name="add_preview"),
    path("add/create/", views.add_create, name="add_create"),
    path("login/", auth_views.LoginView.as_view(template_name="ui/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]
