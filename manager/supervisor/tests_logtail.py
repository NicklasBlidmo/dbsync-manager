"""
Log classification (design §6, §13 unit): parsing, the rules, following a
rotated file. Pure; the registry writes are tested with the compose
profile (tests_db_logtail.py).
"""

import tempfile
from pathlib import Path

from datetime import datetime, timezone as tz
from unittest import mock

from django.test import SimpleTestCase

from registry.models import Event

from . import logtail
from .logtail import TailState, apply, classify, parse, read_new

# Verbatim lines from the 2026-09-16 POC log (docs/review-notes, design §15).
POC_SEGMENT = """2026-09-16 10:35:26,521 -INFO - --- pull finished -- at version v4177
2026-09-16 10:38:48,747 -INFO - --- applying pull actions [PullAction(type=<PullActionType.APPLY_DIFF_NO_REBASE: 'apply_diff_no_rebase'>, path='1-punkter.gpkg')]
2026-09-16 10:38:48,748 -INFO - updating file without rebase: 1-punkter.gpkg
2026-09-16 10:38:48,762 -INFO - --- pull finished -- at version v4178
2026-09-16 10:38:48,857 -ERROR - GEODIFF: NOTICE:  AK-VALIDERING 1-punkter: valideringen kunde inte köras (relation "m-huvudtyp-kontexter" does not exist)
2026-09-16 10:50:00,000 -INFO - Going to log in user svc
2026-09-16 10:50:01,000 -INFO - User svc successfully logged in.
2026-09-16 10:51:00,000 -ERROR - The push failed: HTTP 403
2026-09-16 10:52:00,000 -ERROR - The push failed: HTTP 403
2026-09-16 10:53:00,000 -INFO - --- push finished - new project version v4179
2026-09-16 10:54:00,000 -ERROR - GEODIFF: ERROR: constraint violation while applying changeset
2026-09-16 10:55:00,000 -ERROR - Error while pulling data: timeout
2026-09-16 10:55:00,100 -INFO - --- pull aborted
2026-09-16 10:56:00,000 -ERROR - Traceback (most recent call last):
  File "dbsync_daemon.py", line 200, in main
    dbsync.dbsync_pull(mc)
KeyError: 'version'
"""


LOST_UPDATE_LINE = ("2026-10-26 15:20:01,000 -WARNING - field update lost: table punkter row (column 0 = 631) was deleted "
                    "in the database; the update from Mergin Maps (column 3: 'ny text') is dropped with the row\n")


class ParseTests(SimpleTestCase):
    def test_lines_and_continuations(self):
        records = parse(POC_SEGMENT)
        self.assertEqual(len(records), 14)
        self.assertEqual(records[0].level, "INFO")
        self.assertEqual(records[0].message, "--- pull finished -- at version v4177")
        self.assertEqual(records[0].at.strftime("%Y-%m-%d %H:%M"), "2026-09-16 10:35")
        traceback = records[-1]
        self.assertEqual(traceback.level, "ERROR")
        self.assertIn("KeyError: 'version'", traceback.message)
        self.assertEqual(traceback.message.count("\n"), 3)


class ClassifyTests(SimpleTestCase):
    def kinds(self, text):
        return [(a.kind, a.version) for a in (classify(r) for r in parse(text)) if a is not None]

    def test_lost_field_update_is_a_warning_other_warnings_stay_errors(self):
        # Patch 0003 (slice 17b): the one WARNING line that is informational.
        self.assertEqual(self.kinds(LOST_UPDATE_LINE), [("warning", None)])
        self.assertEqual(self.kinds("2026-10-26 15:20:01,000 -WARNING - Mergin Maps auth token was rejected\n"),
                         [("error", None)])

    def test_poc_segment(self):
        self.assertEqual(self.kinds(POC_SEGMENT), [
            ("pull_open", 4177),
            ("pull_open", 4178),
            ("info", None),            # GEODIFF NOTICE is not an error
            ("error", None),           # 403
            ("error", None),           # 403 again (de-duplicated later, in apply)
            ("push_ok", 4179),
            ("error", None),           # GEODIFF ERROR
            ("error", None),           # Error while pulling data
            ("pull_failed", None),
            ("error", None),           # Traceback
        ])

    def test_noise_is_ignored(self):
        for line in ("Going to log in user x", "User x successfully logged in.", "--- version: DB-sync/2.3.0",
                     "--- start pull ak/bonsta", "got project versions: local version v1 / server version v1",
                     "--- pull - nothing to do (already at server version)", "--- push ak/bonsta - nothing to do",
                     "will download 3 chunks, total size 12", "--- applying pull actions [...]",
                     "updating file without rebase: 1-punkter.gpkg"):
            self.assertIsNone(classify(parse(f"2026-09-16 10:00:00,000 -INFO - {line}\n")[0]), line)

    def test_pull_finished_is_not_a_success(self):
        action = classify(parse("2026-09-16 10:00:00,000 -INFO - --- pull finished -- at version v7\n")[0])
        self.assertEqual(action.kind, "pull_open")   # ok stays null until the check confirms


SERVER_MOVED = "2026-10-20 08:00:00,000 -ERROR - There are pending changes on server - need to pull them first.\n"
PUSH_OK = "2026-10-20 08:01:00,000 -INFO - --- push finished - new project version v12\n"
T = datetime(2026, 10, 20, 8, 0, tzinfo=tz.utc)


class ServerMovedTests(SimpleTestCase):
    """Review note 2026-10-20: not an error, a warning only when it keeps
    happening while local edits wait and could be pushed."""

    def setUp(self):
        self.events = mock.patch("supervisor.logtail.events").start()
        self.runs = mock.patch("supervisor.logtail.SyncRun").start()
        self.facts = mock.patch("supervisor.logtail.project_facts", return_value=(["punkter"], False)).start()
        self.addCleanup(mock.patch.stopall)
        self.project = object()

    def levels(self):
        return [call.args[1] for call in self.events.record.call_args_list]

    def feed(self, text, state=None):
        state = state or TailState()
        actions = [a for a in (classify(r) for r in parse(text)) if a is not None]
        apply(self.project, actions, state, now=T)
        return state

    def test_classified_as_server_moved(self):
        action = classify(parse(SERVER_MOVED)[0])
        self.assertEqual(action.kind, "server_moved")

    def test_one_is_info(self):
        state = self.feed(SERVER_MOVED)
        self.assertEqual(self.levels(), [Event.Level.INFO])
        self.assertIn("server moved during the cycle; retried next cycle", self.events.record.call_args.args[2])
        self.assertEqual(state.server_moved_streak, 1)

    def test_five_in_a_row_with_pending_changes_warn_once(self):
        self.feed(SERVER_MOVED * 7)
        self.assertEqual(self.levels(), [Event.Level.INFO, Event.Level.WARNING])   # repeats collapsed, one warning
        warning = self.events.record.call_args_list[1].args[2]
        self.assertIn("5 consecutive cycles", warning)
        self.assertIn("punkter", warning)

    def test_one_way_project_stays_info(self):
        self.facts.return_value = (["punkter"], True)
        self.feed(SERVER_MOVED * 5)
        self.assertEqual(self.levels(), [Event.Level.INFO])

    def test_no_pending_changes_stays_info(self):
        self.facts.return_value = ([], False)
        self.feed(SERVER_MOVED * 5)
        self.assertEqual(self.levels(), [Event.Level.INFO])

    def test_a_push_resets_the_streak(self):
        state = self.feed(SERVER_MOVED * 4)
        state = self.feed(PUSH_OK, state)
        self.assertEqual(state.server_moved_streak, 0)
        self.feed(SERVER_MOVED * 4, state)
        self.assertEqual(self.levels(), [Event.Level.INFO])
        self.facts.assert_not_called()

    def test_repeats_are_collapsed_across_levels(self):
        state = self.feed(SERVER_MOVED * 3)
        self.assertEqual(state.suppressed, 2)
        self.feed("2026-10-20 08:05:00,000 -ERROR - something else\n", state)
        self.assertEqual(self.levels(), [Event.Level.INFO, Event.Level.ERROR])
        self.assertIn("(previous message repeated 2 more times)", self.events.record.call_args.args[2])


class ReadNewTests(SimpleTestCase):
    def test_incremental_reads_partial_lines_and_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dbsync.log"
            state = TailState()
            self.assertEqual(read_new(path, state), "")           # missing file
            path.write_bytes(b"a\nb\npart")                        # bytes: no CRLF translation on Windows
            self.assertEqual(read_new(path, state), "a\nb\n")
            self.assertEqual(state.partial, "part")
            with open(path, "ab") as handle:
                handle.write(b"ial\nc\n")
            self.assertEqual(read_new(path, state), "partial\nc\n")
            self.assertEqual(read_new(path, state), "")
            # copy-and-truncate rotation: the file shrinks, reading restarts at 0
            path.write_bytes(b"d\n")
            self.assertEqual(read_new(path, state), "d\n")
            # state round-trips through its json file
            state_path = Path(tmp) / logtail.STATE_NAME
            state.save(state_path)
            loaded = TailState.load(state_path)
            self.assertEqual((loaded.offset, loaded.partial), (state.offset, ""))
