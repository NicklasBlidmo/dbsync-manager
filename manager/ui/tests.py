"""
UI tests (tagged `db`: they need users, groups and rows). Run with the
compose profile `test`.
"""

from django.contrib.auth.models import Group, User
from django.test import TestCase, tag
from django.urls import reverse

from registry.models import Database, Event, MerginAccount, SyncConnection, SyncProject, SyncStatus
from registry.permissions import ensure_groups


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
        for text in ("running", "stopped", "behind", "v11", "v10"):
            self.assertContains(response, text)
        self.assertNotContains(response, "Pause")

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
            "file": ["1-punkter.gpkg", "d-foto.gpkg"],
            "modified": ["bonsta_s_punkter", "bonsta_s_foto"], "base": ["bonsta_b_punkter", "bonsta_b_foto"],
        }
        data.update(overrides)
        return data

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
        self.assertContains(response, 'value="bonsta"')                 # default prefix
        self.assertContains(response, "bonsta_s_punkter")
        self.assertContains(response, "bonsta_b_foto")
        self.assertContains(response, "edit-conflict copy")
        self.assertContains(response, "has an edit-conflict sibling")
        self.assertNotContains(response, "readme.txt")

    def test_names_partial_rederives_from_prefix(self):
        response = self.client.get(reverse("add_names"), {"prefix": "P3922 Test", "file": ["1-punkter.gpkg"]})
        self.assertContains(response, "p3922_test_s_punkter")

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
