"""
Log classification (design §6, §13 unit): parsing, the rules, following a
rotated file. Pure; the registry writes are tested with the compose
profile (tests_db_logtail.py).
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from . import logtail
from .logtail import TailState, classify, parse, read_new

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
