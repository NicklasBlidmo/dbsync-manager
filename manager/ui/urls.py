from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("projects/table/", views.projects_table, name="projects_table"),
    path("projects/pause-all/", views.pause_all, name="pause_all"),
    path("projects/resume-all/", views.resume_all, name="resume_all"),
    path("projects/<int:pk>/", views.project_detail, name="project"),
    path("projects/<int:pk>/status/", views.project_status, name="project_status"),
    path("projects/<int:pk>/log/", views.project_log, name="project_log"),
    path("projects/<int:pk>/action/<slug:action>/", views.project_action, name="project_action"),
    path("projects/<int:pk>/hooks/", views.project_hooks, name="project_hooks"),
    path("projects/<int:pk>/runs/", views.project_runs, name="project_runs"),
    path("projects/<int:pk>/hooks/run/", views.project_run_hook, name="project_run_hook"),
    path("projects/<int:pk>/operation/<slug:kind>/", views.project_operation, name="project_operation"),
    path("projects/<int:pk>/operation/", views.project_operation_panel, name="project_operation_panel"),
    path("projects/<int:pk>/verify/", views.project_verify, name="project_verify"),
    path("operations/<int:pk>/retry/", views.operation_retry, name="operation_retry"),
    path("operations/<int:pk>/cancel/", views.operation_cancel, name="operation_cancel"),
    path("scheduled/table/", views.scheduled_table, name="scheduled_table"),
    path("scheduled/<int:pk>/", views.scheduled_detail, name="scheduled"),
    path("scheduled/<int:pk>/runs/", views.scheduled_runs, name="scheduled_runs"),
    path("scheduled/<int:pk>/run/", views.scheduled_run_now, name="scheduled_run"),
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
