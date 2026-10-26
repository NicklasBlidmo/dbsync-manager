"""
UI tests (tagged `db`: they need users, groups and rows). Run with the
compose profile `test`.
"""

import tempfile
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase, override_settings, tag
from django.urls import reverse

from registry.models import Database, Event, MerginAccount, Operation, SyncConnection, SyncProject, SyncStatus
from registry.permissions import ensure_groups
from supervisor import layout


@tag("db")
class PagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_groups()
        cls.viewer = User.objects.create_user("viewer", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        cls.operator = User.objects.create_user("operator", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        account = MerginAccount(name="svc", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.project = SyncProject.objects.create(
            mergin_project="ak/bonsta", mergin_account=account, database=database, schema_prefix="p3922_test",
            health=SyncProject.Health.BEHIND, active_conditions={"db_behind:1-punkter.gpkg": "2026-09-22T12:00:00"},
        )
        conn = SyncConnection.objects.create(
            project=cls.project, sync_file="1-punkter.gpkg", modified_schema="p3922_test_s_punkter",
            base_schema="p3922_test_b_punkter",
        )
        SyncStatus.objects.create(
            project=cls.project, connection=conn, server_version=11, local_version=11, db_version=10,
            gpkg_count={"punkter": 5}, base_count={"punkter": 5}, modified_count={"punkter": 6},
            mergin_permission="reader", direction="one_way",
        )

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_home_shows_three_badges_and_versions(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.get(reverse("home"))
        self.assertContains(response, "ak/bonsta")
        self.assertContains(response, "1 alert")
        for text in ("running", "stopped", "behind"):
            self.assertContains(response, text)
        # One Version column: the server's, with a badge where the database differs.
        self.assertContains(response, "v11")
        self.assertContains(response, "db v10")
        self.assertNotContains(response, "checkout v")
        self.assertNotContains(response, "Pause")

    def test_status_bar_uses_labels_not_prefixes(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.get(reverse("project", args=[self.project.pk]))
        for label in ("Desired", "Process", "Health"):
            self.assertContains(response, f'<span class="label">{label}</span>')
        self.assertNotContains(response, "desired: ")
        self.assertNotContains(response, "health: ")
        self.assertContains(response, "Settings</a>")
        self.assertNotContains(response, "Settings (admin)")
        # Events and Hooks are collapsible sections; events open because the newest is a warning? none yet -> closed
        self.assertContains(response, '<details class="section">', count=2)

    def test_home_has_one_primary_action_and_one_current_nav_link(self):
        self.client.login(username="operator", password="pw")
        response = self.client.get(reverse("home"))
        self.assertContains(response, "button primary", count=1)
        self.assertContains(response, 'aria-current="page"', count=1)
        response = self.client.get(reverse("project", args=[self.project.pk]))
        self.assertContains(response, 'aria-current="page"', count=1)
        self.assertNotContains(response, "button primary")

    def test_admin_wears_the_manager_theme(self):
        User.objects.create_superuser("root2", password="pw")
        self.client.login(username="root2", password="pw")
        with self.settings(DEBUG=False):
            response = self.client.get(reverse("admin:index"))
        self.assertContains(response, 'href="/static/ui/app.css"')
        self.assertContains(response, 'href="/static/ui/admin-theme.css"')
        self.assertContains(response, "&larr; Projects")
        self.assertContains(response, "Settings</h1>")
        self.assertContains(response, "<title>Settings | dbsync-manager</title>")

    def test_admin_test_connection_action_reports_facts_or_the_error(self):
        from unittest import mock

        User.objects.create_superuser("root3", password="pw")
        self.client.login(username="root3", password="pw")
        database = Database.objects.get(name="gis")
        url = reverse("admin:registry_database_changelist")
        data = {"action": "test_connection", "_selected_action": [database.pk]}
        facts = {"server_version": "16.4", "postgis_version": "3.4.2", "schemas": 5}
        with mock.patch("registry.admin.dbinspect.server_facts", return_value=facts):
            response = self.client.post(url, data, follow=True)
        self.assertContains(response, "ok, PostgreSQL 16.4, PostGIS 3.4.2, 5 schemas besides the system ones")
        facts["postgis_version"] = None
        with mock.patch("registry.admin.dbinspect.server_facts", return_value=facts):
            response = self.client.post(url, data, follow=True)
        self.assertContains(response, "PostGIS is not installed")
        with mock.patch("registry.admin.dbinspect.server_facts", side_effect=OSError("connection refused")):
            response = self.client.post(url, data, follow=True)
        self.assertContains(response, "connection refused")

    def test_verify_button_starts_the_operation_once(self):
        from registry.models import Operation

        self.client.login(username="operator", password="pw")
        page = self.client.get(reverse("project", args=[self.project.pk]))
        self.assertContains(page, ">Verify</button>")
        self.assertContains(page, "Tables <span")
        self.assertContains(page, "/api/v1/projects/")
        response = self.client.post(reverse("project_verify", args=[self.project.pk]))
        self.assertRedirects(response, reverse("project", args=[self.project.pk]))
        operation = Operation.objects.get(project=self.project)
        self.assertEqual((operation.kind, operation.status), ("verify", "pending"))
        self.assertEqual(self.client.post(reverse("project_verify", args=[self.project.pk])).status_code, 409)
        self.client.login(username="viewer", password="pw")
        self.assertEqual(self.client.post(reverse("project_verify", args=[self.project.pk])).status_code, 403)

    def test_no_recipients_is_flagged_until_a_default_list_exists(self):
        from registry.models import InstallationSettings

        self.client.login(username="viewer", password="pw")
        self.assertContains(self.client.get(reverse("home")), "no recipients")
        self.assertContains(self.client.get(reverse("project", args=[self.project.pk])), "not mailed")
        InstallationSettings.objects.create(notify_emails=["ops@example.org"])
        self.assertNotContains(self.client.get(reverse("home")), "no recipients")
        self.assertNotContains(self.client.get(reverse("project", args=[self.project.pk])), "not mailed")

    def test_home_filter(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.get(reverse("projects_table"), {"health": "ok"})
        self.assertContains(response, "No projects match")

    def test_project_page_shows_interrupted_pull_and_events(self):
        self.client.login(username="operator", password="pw")
        response = self.client.get(reverse("project", args=[self.project.pk]))
        # Connection row: database column carries the badge, the per-table
        # detail is auto-expanded because a table row has a badge.
        self.assertContains(response, "behind checkout")
        self.assertContains(response, "interrupted pull, not user edits")
        self.assertContains(response, 'aria-expanded="true"')
        self.assertContains(response, "db_behind (1-punkter.gpkg)")
        # Project-level facts appear once in the status panel.
        self.assertContains(response, "Mergin permission: reader")
        self.assertContains(response, "superusers are not listed", count=1)
        self.assertContains(response, "Pause")
        self.assertContains(response, "(no log yet)")
        self.assertContains(response, 'href="/static/ui/app.css"')

    def test_viewer_cannot_pause(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.post(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertEqual(response.status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.desired_state, "running")

    def test_operator_pause_writes_desired_state_and_audit_event(self):
        self.client.login(username="operator", password="pw")
        response = self.client.post(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertRedirects(response, reverse("project", args=[self.project.pk]))
        self.project.refresh_from_db()
        self.assertEqual(self.project.desired_state, "paused")
        event = Event.objects.get(project=self.project, source="user")
        self.assertEqual(event.user, self.operator)
        self.assertIn("paused", event.message)
        # Pausing again is a no-op without a second event.
        self.client.post(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertEqual(Event.objects.filter(project=self.project, source="user").count(), 1)

    def test_get_on_action_is_refused(self):
        self.client.login(username="operator", password="pw")
        response = self.client.get(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertEqual(response.status_code, 405)


@tag("db")
class ConnectionTableTests(TestCase):
    """The connections table (slice 8 brief, part B): in-phase rows are muted
    and collapsed, deviating rows sort first with badges and open detail."""

    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone

        ensure_groups()
        cls.viewer = User.objects.create_user("viewer8", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        account = MerginAccount(name="svc8", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis8", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.project = SyncProject.objects.create(
            mergin_project="ak/table", mergin_account=account, database=database, schema_prefix="t",
            health=SyncProject.Health.OK,
        )
        now = timezone.now()

        def conn(name, **status):
            c = SyncConnection.objects.create(project=cls.project, sync_file=name, modified_schema=f"t_s_{name[2:-5]}",
                                              base_schema=f"t_b_{name[2:-5]}")
            SyncStatus.objects.create(project=cls.project, connection=c, server_version=20, checked_at=now,
                                      last_ok_check_at=now, mergin_permission="reader", direction="one_way", **status)
            return c

        # a: in phase, single table
        conn("1-a.gpkg", local_version=20, db_version=20, gpkg_count={"a": 3}, base_count={"a": 3}, modified_count={"a": 3})
        # b: checkout and database behind the server, single table
        conn("1-b.gpkg", local_version=19, db_version=19, gpkg_count={"b": 3}, base_count={"b": 3}, modified_count={"b": 3})
        # c: two tables, in phase
        conn("1-c.gpkg", local_version=20, db_version=20, gpkg_count={"c1": 1, "c2": 2},
             base_count={"c1": 1, "c2": 2}, modified_count={"c1": 1, "c2": 2})
        # d: two tables, one with base behind for two checks
        conn("1-d.gpkg", local_version=20, db_version=20, gpkg_count={"d1": 5, "d2": 2},
             base_count={"d1": 4, "d2": 2}, modified_count={"d1": 4, "d2": 2}, mismatch_streak={"d1": 2})

    def setUp(self):
        self.client.login(username="viewer8", password="pw")

    def test_rows_facts_and_sorting(self):
        response = self.client.get(reverse("project", args=[self.project.pk]))
        html = response.content.decode()
        self.assertNotIn('class="grid"', html)                      # no card grid
        self.assertContains(response, 'class="table connections"')
        # deviating rows first (b, d), then the in-phase ones in file order (a, c)
        order = [html.index(f"1-{x}.gpkg") for x in "bdac"]
        self.assertEqual(order, sorted(order))
        self.assertContains(response, "behind server", count=2)      # checkout and database of b
        self.assertContains(response, "base behind ×2")
        self.assertContains(response, "2 tables", count=2)           # c and d
        self.assertContains(response, "<dt>Server</dt><dd>v20", count=1)   # server version once, in the panel
        self.assertContains(response, "server versions differ", count=0)
        self.assertContains(response, "none found")
        self.assertContains(response, "superusers are not listed", count=1)

    def test_expansion_follows_the_data(self):
        response = self.client.get(reverse("project_status", args=[self.project.pk]))
        html = response.content.decode()
        import re

        def toggle_state(file_name):
            match = re.search(rf'aria-expanded="(true|false)"[^>]*data-conn="(\d+)"[^>]*></button>\s*{re.escape(file_name)}', html)
            self.assertIsNotNone(match, file_name)
            return match.group(1), match.group(2)

        self.assertEqual(toggle_state("1-c.gpkg")[0], "false")       # two tables, in phase: collapsed
        expanded, pk = toggle_state("1-d.gpkg")
        self.assertEqual(expanded, "true")                            # streak >= 2: opened by the server
        self.assertNotIn(f'id="detail-{pk}" hidden', html)
        self.assertIn(f'id="detail-{toggle_state("1-c.gpkg")[1]}" hidden', html)


@tag("db")
class WizardViewTests(TestCase):
    """The wizard end to end with the Mergin API and the target database mocked."""

    INFO = {
        "id": "0b9f1b2e-0000-4000-8000-000000000001", "version": "v6", "role": "owner",
        "files": [
            {"path": "1-punkter.gpkg", "size": 1234}, {"path": "d-foto.gpkg", "size": 99},
            {"path": "1-punkter (edit conflict, anna v3).gpkg", "size": 5}, {"path": "readme.txt", "size": 1},
        ],
    }

    @classmethod
    def setUpTestData(cls):
        ensure_groups()
        cls.operator = User.objects.create_user("op", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        cls.viewer = User.objects.create_user("v", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        cls.account = MerginAccount(name="svc", username="svc")
        cls.account.secret = "pw"
        cls.account.save()
        cls.database = Database(name="gis", host="db", dbname="ak", user="ak_dbsync")
        cls.database.password = "pw"
        cls.database.save()

    def setUp(self):
        from unittest import mock

        self.client.login(username="op", password="pw")
        patches = {
            "project_info": mock.patch("ui.views.mergin_api.project_info", return_value=self.INFO),
            "workspaces": mock.patch("ui.views.mergin_api.workspaces", return_value=[{"name": "ak", "role": "owner"}]),
            "search": mock.patch("ui.views.mergin_api.search_projects", return_value=[
                {"path": "ak/bonsta", "name": "bonsta", "version": 6, "updated": "2026-09-22T10:00:00", "permission": "owner"},
            ]),
            "inspect": mock.patch("ui.wizard.inspect_targets"),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        from ui.wizard import TargetCheck

        self.mocks["inspect"].return_value = ([
            TargetCheck("1-punkter.gpkg", "bonsta_s_punkter", "bonsta_b_punkter", "new", "will be created"),
            TargetCheck("d-foto.gpkg", "bonsta_s_foto", "bonsta_b_foto", "new", "will be created"),
        ], "")

    def form(self, **overrides):
        data = {
            "account": self.account.pk, "database": self.database.pk, "project": "ak/bonsta",
            "prefix": "bonsta", "init_from": "gpkg", "sleep_time": "10",
            "pick": ["1-punkter.gpkg", "d-foto.gpkg"],
            "file": ["1-punkter.gpkg", "d-foto.gpkg"],
            "modified": ["bonsta_s_punkter", "bonsta_s_foto"], "base": ["bonsta_b_punkter", "bonsta_b_foto"],
            "use_default_recipients": "1",
        }
        data.update(overrides)
        return data

    def test_recipients_are_addresses_or_the_default_never_nothing(self):
        # 2026-10-20d, finding 2: a project added without recipients mailed nobody.
        response = self.client.post(reverse("add_preview"), self.form(use_default_recipients=""))
        self.assertContains(response, "no alert recipients")
        self.assertNotContains(response, "Add sync</button>")
        response = self.client.post(reverse("add_preview"), self.form(use_default_recipients="", notify_emails="ops@example.org"))
        self.assertContains(response, "ops@example.org")
        self.assertContains(response, "Add sync</button>")
        response = self.client.post(reverse("add_preview"), self.form())
        self.assertContains(response, "default recipients")
        self.assertContains(response, 'name="use_default_recipients" value="1"')

    def test_viewer_cannot_open_the_wizard(self):
        self.client.login(username="v", password="pw")
        self.assertEqual(self.client.get(reverse("add_start")).status_code, 403)

    def test_start_search_and_project_step(self):
        self.assertContains(self.client.get(reverse("add_start")), "step 1 of 3")
        self.assertContains(self.client.get(reverse("add_workspaces"), {"account": self.account.pk}), "ak (owner)")
        response = self.client.get(reverse("add_search"), {"account": self.account.pk, "workspace": "ak", "q": "bon", "database": self.database.pk})
        self.assertContains(response, "ak/bonsta")
        # Choose submits the pick form, so the selects are read at click time.
        self.assertContains(response, 'form="pick" name="project" value="ak/bonsta"')
        response = self.client.get(reverse("add_project"), {"account": self.account.pk, "database": self.database.pk, "project": "ak/bonsta"})
        self.assertContains(response, "step 2 of 3")
        self.assertContains(response, "ak_dbsync@db:5432/ak")      # the chosen database, visible before Preview
        self.assertContains(response, 'value="bonsta"')                 # default prefix
        self.assertContains(response, "bonsta_s_punkter")
        self.assertContains(response, "bonsta_b_foto")
        self.assertContains(response, "edit-conflict copy")
        self.assertContains(response, "has an edit-conflict sibling")
        self.assertNotContains(response, "readme.txt")

    def test_names_partial_follows_the_ticked_files_only(self):
        # Derived from the prefix for the picked files.
        response = self.client.post(reverse("add_names"), {"prefix": "P3922 Test", "pick": ["1-punkter.gpkg"]})
        self.assertContains(response, "p3922_test_s_punkter")
        self.assertContains(response, 'name="names_prefix" value="p3922_test"')
        # Nothing picked is an empty table, even if the previous table's rows come along.
        response = self.client.post(reverse("add_names"), {"prefix": "p3922_test", "names_prefix": "p3922_test",
                                                           "file": ["1-punkter.gpkg"], "modified": ["x_s"], "base": ["x_b"]})
        self.assertContains(response, "Select at least one GeoPackage")
        self.assertNotContains(response, "1-punkter.gpkg<input")
        # Typed names survive for files still picked; rows for unpicked files are dropped.
        response = self.client.post(reverse("add_names"), {
            "prefix": "p3922_test", "names_prefix": "p3922_test", "pick": ["1-punkter.gpkg"],
            "file": ["1-punkter.gpkg", "d-foto.gpkg"], "modified": ["custom_s", "foto_s"], "base": ["custom_b", "foto_b"],
        })
        self.assertContains(response, 'value="custom_s"')
        self.assertNotContains(response, "foto_s")
        # A changed prefix re-derives every row.
        response = self.client.post(reverse("add_names"), {
            "prefix": "other", "names_prefix": "p3922_test", "pick": ["1-punkter.gpkg"],
            "file": ["1-punkter.gpkg"], "modified": ["custom_s"], "base": ["custom_b"],
        })
        self.assertContains(response, 'value="other_s_punkter"')
        self.assertNotContains(response, "custom_s")

    def test_names_partial_is_a_post_and_takes_a_large_project(self):
        # The whole step-2 form rides along (hx-include="#add"). As a GET, 18
        # files already passed gunicorn's 4094-byte request line and the
        # table stopped refreshing (2026-09-28, 3448_brasan9); so it is a POST.
        self.assertEqual(self.client.get(reverse("add_names"), {"prefix": "p", "pick": ["a.gpkg"]}).status_code, 405)
        files = [f"survey-area-{i:02d}-a-rather-long-geopackage-file-name-with-many-words.gpkg" for i in range(40)]
        data = {
            "prefix": "p3448_brasan9", "names_prefix": "p3448_brasan9", "pick": files,
            "file": files, "modified": [f"custom_s_{i}" for i in range(40)], "base": [f"custom_b_{i}" for i in range(40)],
        }
        self.assertGreater(len(urlencode(data, doseq=True)), 4094)
        response = self.client.post(reverse("add_names"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count('name="file"'), 40)
        self.assertContains(response, 'value="custom_s_39"')

    def test_preview_with_nothing_picked_is_refused(self):
        response = self.client.post(reverse("add_preview"), self.form(pick=[]))
        self.assertContains(response, "Select at least one GeoPackage")
        self.assertNotContains(response, "Add sync</button>")
        response = self.client.post(reverse("add_create"), self.form(pick=[]))
        self.assertContains(response, "Select at least one GeoPackage")
        self.assertFalse(SyncProject.objects.exists())

    def test_create_uses_the_ticked_files_not_the_name_rows(self):
        # Two ticked, names for four: exactly two connections, with the typed names.
        response = self.client.post(reverse("add_create"), self.form(
            pick=["1-punkter.gpkg", "d-foto.gpkg"],
            file=["1-punkter.gpkg", "d-foto.gpkg", "x.gpkg", "y.gpkg"],
            modified=["bonsta_s_punkter", "bonsta_s_foto", "x_s", "y_s"],
            base=["bonsta_b_punkter", "bonsta_b_foto", "x_b", "y_b"],
        ))
        self.assertEqual(response.status_code, 302)
        project = SyncProject.objects.get(mergin_project="ak/bonsta")
        self.assertEqual(sorted(project.connections.values_list("sync_file", flat=True)), ["1-punkter.gpkg", "d-foto.gpkg"])
        self.assertEqual(project.connections.get(sync_file="d-foto.gpkg").modified_schema, "bonsta_s_foto")

    def test_preview_then_create(self):
        response = self.client.post(reverse("add_preview"), self.form())
        self.assertContains(response, "step 3 of 3")
        self.assertContains(response, "two_way")
        self.assertContains(response, "Add sync")
        self.assertNotContains(response, "I understand")
        response = self.client.post(reverse("add_create"), self.form())
        project = SyncProject.objects.get(mergin_project="ak/bonsta")
        self.assertRedirects(response, reverse("project", args=[project.pk]))
        self.assertEqual(project.desired_state, "running")
        self.assertEqual(str(project.mergin_project_id), self.INFO["id"])
        self.assertEqual(project.connections.count(), 2)
        self.assertEqual(project.connections.get(sync_file="d-foto.gpkg").modified_schema, "bonsta_s_foto")
        self.assertEqual(Event.objects.get(project=project, source="user").user, self.operator)

    def test_validation_errors_block_create(self):
        response = self.client.post(reverse("add_create"), self.form(prefix="Bad-Prefix", modified=["x", "x"]))
        self.assertContains(response, "prefix must be a PostgreSQL identifier")
        self.assertContains(response, "used twice")
        self.assertFalse(SyncProject.objects.exists())

    def test_rejected_target_blocks_create(self):
        from ui.wizard import TargetCheck

        self.mocks["inspect"].return_value = ([
            TargetCheck("1-punkter.gpkg", "bonsta_s_punkter", "bonsta_b_punkter", "reject", "belong to Mergin project 'ak/other'"),
            TargetCheck("d-foto.gpkg", "bonsta_s_foto", "bonsta_b_foto", "new", ""),
        ], "")
        response = self.client.post(reverse("add_create"), self.form())
        self.assertContains(response, "ak/other")
        self.assertFalse(SyncProject.objects.exists())

    def test_adoption_and_reader_with_writers_need_acknowledgement(self):
        from ui.wizard import TargetCheck

        self.mocks["project_info"].return_value = dict(self.INFO, role="reader")
        self.mocks["inspect"].return_value = ([
            TargetCheck("1-punkter.gpkg", "bonsta_s_punkter", "bonsta_b_punkter", "adopt", "exist for this project", ["qgis_editor"]),
            TargetCheck("d-foto.gpkg", "bonsta_s_foto", "bonsta_b_foto", "new", "", None),
        ], "")
        response = self.client.post(reverse("add_preview"), self.form())
        self.assertContains(response, "I understand")
        self.assertContains(response, "db_edits_cannot_push")
        self.assertContains(response, "must be stopped first")
        response = self.client.post(reverse("add_create"), self.form())
        self.assertContains(response, "tick the acknowledgement")
        self.assertFalse(SyncProject.objects.exists())
        response = self.client.post(reverse("add_create"), self.form(ack="1"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SyncProject.objects.filter(mergin_project="ak/bonsta").exists())

    def test_same_project_twice_into_one_database_is_refused(self):
        self.client.post(reverse("add_create"), self.form())
        response = self.client.post(reverse("add_create"), self.form(prefix="other", modified=["o_s_a", "o_s_b"], base=["o_b_a", "o_b_b"]))
        self.assertContains(response, "already synced into database")
        self.assertEqual(SyncProject.objects.count(), 1)


@tag("db")
class BulkActionTests(TestCase):
    """Pause all / Resume all and the events filter (review note 2026-10-20b, 5 and 6)."""

    @classmethod
    def setUpTestData(cls):
        ensure_groups()
        cls.operator = User.objects.create_user("op4", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        cls.viewer = User.objects.create_user("v4", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        account = MerginAccount(name="svc4", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis4", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.projects = {}
        for name, desired in (("ak/a", "running"), ("ak/b", "running"), ("ak/c", "paused")):
            cls.projects[name] = SyncProject.objects.create(
                mergin_project=name, mergin_account=account, database=database, schema_prefix=name[-1],
                desired_state=desired,
            )

    def test_pause_all_then_resume_all_touch_only_the_batch(self):
        self.client.login(username="op4", password="pw")
        page = self.client.get(reverse("pause_all"))
        self.assertContains(page, "2 projects will be paused")
        self.assertContains(page, "ak/c")                    # listed as not touched
        response = self.client.post(reverse("pause_all"))
        self.assertRedirects(response, reverse("home"))
        self.assertEqual(sorted(SyncProject.objects.filter(desired_state="paused").values_list("mergin_project", flat=True)),
                         ["ak/a", "ak/b", "ak/c"])
        batch = Event.objects.filter(condition__startswith="pause_all:").values_list("condition", flat=True).distinct()
        self.assertEqual(len(batch), 1)
        self.assertEqual(Event.objects.filter(condition=batch[0]).count(), 2)
        # Someone resumes ak/a by hand meanwhile; Resume all then only resumes ak/b.
        SyncProject.objects.filter(mergin_project="ak/a").update(desired_state="running")
        page = self.client.get(reverse("resume_all"))
        self.assertContains(page, "1 project will be resumed")
        self.assertContains(page, "ak/b")
        self.assertContains(page, "Not touched: ak/c")
        self.client.post(reverse("resume_all"))
        self.assertEqual(SyncProject.objects.get(mergin_project="ak/b").desired_state, "running")
        self.assertEqual(SyncProject.objects.get(mergin_project="ak/c").desired_state, "paused")
        self.assertTrue(Event.objects.filter(condition__startswith="resume_all:", project__mergin_project="ak/b").exists())
        # Nothing left: the page says so instead of offering a button.
        self.assertContains(self.client.get(reverse("resume_all")), "Nothing to resume")
        self.client.login(username="v4", password="pw")
        self.assertEqual(self.client.get(reverse("pause_all")).status_code, 403)

    def test_migrate_checkouts_notice_page_and_command(self):
        # Slice 16, C10: a project with the 2.3.0 checkout tree.
        a = self.projects["ak/a"]
        self.client.login(username="op4", password="pw")
        self.assertNotContains(self.client.get(reverse("home")), "Checkout migration pending")
        with tempfile.TemporaryDirectory() as tmp, override_settings(DBSYNC_DATA_DIR=Path(tmp)):
            (layout.legacy_checkout_tree(a.work_dir, a.mergin_project) / "a_s_x").mkdir(parents=True)
            home = self.client.get(reverse("home"))
            self.assertContains(home, "Checkout migration pending")
            self.assertContains(home, "1 project")
            self.assertContains(home, "still has the old layout")
            self.assertContains(home, reverse("migrate_checkouts"))
            page = self.client.get(reverse("migrate_checkouts"))
            self.assertContains(page, "1 project will be migrated")
            self.assertContains(page, "Run Verify on every project first")
            out = StringIO()
            call_command("migrate_checkouts", "--list", stdout=out)
            self.assertIn("ak/a", out.getvalue())
            self.assertFalse(Operation.objects.exists())            # --list creates nothing
            response = self.client.post(reverse("migrate_checkouts"))
            self.assertRedirects(response, reverse("home"))
            op = Operation.objects.get(project=a)
            self.assertEqual((op.kind, op.status, op.requested_by), ("migrate", "pending", self.operator))
            self.assertTrue(Event.objects.filter(project=a, message__contains="checkout layout migration").exists())
            # While that operation is open the page lists the project as not touched.
            self.assertContains(self.client.get(reverse("migrate_checkouts")), "Not touched: ak/a")
            out = StringIO()
            call_command("migrate_checkouts", stdout=out)
            self.assertIn("skipped ak/a", out.getvalue())
            # A viewer sees the notice but no button, and cannot open the page.
            self.client.login(username="v4", password="pw")
            home = self.client.get(reverse("home"))
            self.assertContains(home, "Checkout migration pending")
            self.assertNotContains(home, reverse("migrate_checkouts"))
            self.assertEqual(self.client.get(reverse("migrate_checkouts")).status_code, 403)

    def test_home_shows_the_buttons_to_operators_only(self):
        self.client.login(username="op4", password="pw")
        self.assertContains(self.client.get(reverse("home")), "Pause all")
        self.client.login(username="v4", password="pw")
        self.assertNotContains(self.client.get(reverse("home")), "Pause all")

    def test_events_filter_and_paging(self):
        from datetime import timedelta

        from django.utils import timezone

        project = self.projects["ak/a"]
        base = timezone.make_aware(timezone.datetime(2026, 10, 20, 13, 0))
        for i in range(60):
            Event.objects.create(project=project, at=base + timedelta(minutes=i), level="info" if i % 10 else "error",
                                 source="supervisor", message=f"event {i}")
        self.client.login(username="v4", password="pw")
        url = reverse("project", args=[project.pk])
        page = self.client.get(url)
        self.assertContains(page, "event 59")
        self.assertNotContains(page, "event 5<")
        self.assertContains(page, "page 1 of 2")
        self.assertContains(page, "older")
        page = self.client.get(url, {"page": 2})
        self.assertContains(page, "event 0<")
        page = self.client.get(url, {"since": "2026-10-20T13:30", "until": "2026-10-20T13:40", "level": "info"})
        self.assertContains(page, "event 39")
        self.assertNotContains(page, "event 30<")               # error, filtered out
        self.assertNotContains(page, "event 40<")               # at the until bound
        self.assertContains(page, 'id="events" open')
        self.assertContains(page, "Reset")

    def test_events_command(self):
        from io import StringIO

        from django.core.management import call_command

        project = self.projects["ak/b"]
        Event.objects.create(project=project, level="error", source="dbsync", message="line one\nline two", condition="process_crashed")
        Event.objects.create(project=project, level="info", source="supervisor", message="fine")
        out, err = StringIO(), StringIO()
        call_command("events", project="ak/b", since="2026-01-01", level="error", stdout=out, stderr=err)
        text = out.getvalue()
        self.assertIn("error", text)
        self.assertIn("ak/b", text)
        self.assertIn("line one", text)
        self.assertIn("    line two", text)
        self.assertIn("[process_crashed]", text)
        self.assertNotIn("fine", text)
        self.assertIn("1 event(s)", err.getvalue())


@tag("db")
class ScheduledPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import time as dtime

        from registry.models import Hook

        ensure_groups()
        cls.operator = User.objects.create_user("op3", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        cls.viewer = User.objects.create_user("v3", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        database = Database(name="gis3", host="postgis", dbname="gis", user="backup")
        database.password = "pw"
        database.save()
        cls.hook = Hook.objects.create(
            name="nightly backup", kind="scheduled", command="pg_dump -Fc", database=database,
            run_at=dtime(2, 15), notify_emails=["ops@example.org"], last_status="failed", timeout_s=3600,
        )

    def test_home_lists_scheduled_hooks_and_opens_the_section_on_failure(self):
        self.client.login(username="op3", password="pw")
        response = self.client.get(reverse("home"))
        self.assertContains(response, '<details class="section mt-3" open>')
        self.assertContains(response, "nightly backup")
        self.assertContains(response, "02:15")
        self.assertContains(response, "backup (gis3)")
        self.assertContains(response, '<span class="badge bad">failed</span>')
        self.assertContains(response, "Run now")
        self.client.login(username="v3", password="pw")
        self.assertNotContains(self.client.get(reverse("home")), "Run now")

    def test_run_now_creates_a_request_and_the_page_shows_it(self):
        from registry.models import SyncRun

        self.client.login(username="op3", password="pw")
        response = self.client.post(reverse("scheduled_run", args=[self.hook.pk]))
        self.assertRedirects(response, reverse("scheduled", args=[self.hook.pk]))
        run = SyncRun.objects.get(hook=self.hook)
        self.assertIsNone(run.project)
        self.assertEqual(run.summary["status"], "requested")
        self.assertEqual(run.summary["requested_by"], "op3")
        self.assertTrue(Event.objects.filter(project__isnull=True, source="user", message__contains="nightly backup").exists())
        page = self.client.get(reverse("scheduled", args=[self.hook.pk]))
        self.assertContains(page, "scheduled hook")
        self.assertContains(page, "requested by op3")
        self.assertContains(page, '<span class="badge info">requested</span>')
        self.assertContains(page, "pg_dump -Fc")
        # A second request while one is pending is refused.
        self.assertEqual(self.client.post(reverse("scheduled_run", args=[self.hook.pk])).status_code, 409)
        self.client.login(username="v3", password="pw")
        self.assertEqual(self.client.post(reverse("scheduled_run", args=[self.hook.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("scheduled", args=[self.hook.pk])).status_code, 200)


@tag("db")
class HookPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from registry.models import Hook

        ensure_groups()
        cls.operator = User.objects.create_user("op2", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        cls.viewer = User.objects.create_user("v2", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        account = MerginAccount(name="svc2", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis2", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.hook = Hook.objects.create(name="grants", kind="post_init", command="true")
        cls.project = SyncProject.objects.create(
            mergin_project="ak/hooked", mergin_account=account, database=database, schema_prefix="h",
            post_init_hook=cls.hook,
        )

    def test_operator_can_request_a_run(self):
        from registry.models import SyncRun

        self.client.login(username="op2", password="pw")
        page = self.client.get(reverse("project", args=[self.project.pk]))
        self.assertContains(page, "Run post_init now")
        response = self.client.post(reverse("project_run_hook", args=[self.project.pk]), {"kind": "post_init"})
        self.assertRedirects(response, reverse("project", args=[self.project.pk]))
        run = SyncRun.objects.get(project=self.project, kind="hook")
        self.assertEqual(run.summary["status"], "requested")
        self.assertEqual(run.summary["requested_by"], "op2")
        self.assertTrue(Event.objects.filter(project=self.project, source="user", message__contains="requested the post_init hook").exists())
        partial = self.client.get(reverse("project_hooks", args=[self.project.pk]))
        self.assertContains(partial, "requested")

    def test_unconfigured_kind_is_refused_and_viewer_forbidden(self):
        self.client.login(username="op2", password="pw")
        self.assertEqual(self.client.post(reverse("project_run_hook", args=[self.project.pk]), {"kind": "post_pull"}).status_code, 400)
        self.client.login(username="v2", password="pw")
        self.assertEqual(self.client.post(reverse("project_run_hook", args=[self.project.pk]), {"kind": "post_init"}).status_code, 403)
