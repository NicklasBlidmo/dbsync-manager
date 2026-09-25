"""
Log history (design §3 "history is parsed", §6 log classification).

The supervisor follows each project's `dbsync.log` (db-sync's --log-file
at INFO) and turns lines into `sync_run` rows and events. This is the only
place the log is interpreted, and nothing here decides state or raises an
alert: the consistency check does that from the sources of truth.

Rules, from what db-sync 2.3.0 and mergin-client 0.13.3 actually log:

| line                                              | meaning                                  | action                           |
|---------------------------------------------------|------------------------------------------|----------------------------------|
| `--- pull finished -- at version vN`              | checkout is at vN; nothing applied yet   | open pull run, checkout_version N |
| `--- pull aborted`                                 | file pull failed                          | close open pull run as failed     |
| `--- push finished - new project version vN`       | server accepted the push                  | push run, ok                      |
| `--- push aborted`                                 | push failed                               | push run, failed                  |
| `ERROR - GEODIFF: NOTICE: ...`                     | a trigger's NOTICE relayed by geodiff     | info event                        |
| `ERROR - GEODIFF: ...` (anything else)             | apply failed                              | error event, close pull as failed |
| `ERROR - ...` (DbSyncError text, download errors)  | the cycle failed                          | error event                       |
| `Traceback` block                                  | an exception escaped                      | error event                       |
| login / token / "nothing to do" / progress lines   | noise                                     | ignored                           |

A pull run becomes ok only when the check sees `db_version` reach its
`checkout_version` (core._maybe_check). Repeated identical errors within
an hour are counted, not written again.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from django.utils import timezone

from registry import events
from registry.models import Event, SyncRun

LINE_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ -(?P<level>[A-Z]+) - (?P<msg>.*)$")
PULL_FINISHED_RE = re.compile(r"--- pull finished -- at version v(\d+)")
PUSH_FINISHED_RE = re.compile(r"--- push finished - new project version v(\d+)")
STATE_NAME = "logtail.json"
DEDUP_WINDOW = timedelta(hours=1)
MAX_READ = 4 * 1024 * 1024   # per tick; a runaway log does not stall the loop
IGNORED_PREFIXES = (
    "Going to log in user", "User ", "Token has expired", "No token", "--- version:", "--- start ",
    "got project", "will download", "will upload", "--- pull - nothing to do", "--- push ", "-- Push changes --",
    "Finishing transaction", "Starting upload", "got transaction", "not uploading", "--- download",
    "Using v2", "missing base file", "Project version exists",
)


@dataclass
class Record:
    at: datetime | None
    level: str
    message: str


@dataclass
class Action:
    kind: str                 # pull_open | pull_failed | push_ok | push_failed | error | info
    message: str
    version: int | None = None
    at: datetime | None = None


# --- parsing (pure) --------------------------------------------------------------


def _aware(text: str):
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # db-sync logs the container's local time (TZ), which is Django's TIME_ZONE.
    return timezone.make_aware(naive, timezone.get_default_timezone())


def parse(text: str) -> list:
    """Complete lines only (the caller keeps the unterminated tail). Continuation
    lines without the log prefix (tracebacks, pprint output) join the previous record."""
    records = []
    for line in text.splitlines():
        match = LINE_RE.match(line)
        if match:
            records.append(Record(_aware(match["ts"]), match["level"], match["msg"]))
        elif records:
            records[-1].message += "\n" + line
        elif line.strip():
            records.append(Record(None, "INFO", line))
    return records


def classify(record: Record):
    """One Action or None for noise."""
    msg = record.message
    first = msg.split("\n", 1)[0]
    if "Traceback (most recent call last)" in msg:
        return Action("error", msg, at=record.at)
    match = PULL_FINISHED_RE.search(first)
    if match:
        return Action("pull_open", first, version=int(match.group(1)), at=record.at)
    match = PUSH_FINISHED_RE.search(first)
    if match:
        return Action("push_ok", first, version=int(match.group(1)), at=record.at)
    if first.startswith("--- pull aborted"):
        return Action("pull_failed", first, at=record.at)
    if first.startswith("--- push aborted"):
        return Action("push_failed", first, at=record.at)
    if record.level == "ERROR":
        if first.startswith("GEODIFF: NOTICE:") or first.startswith("GEODIFF:  NOTICE:"):
            return Action("info", msg, at=record.at)
        return Action("error", msg, at=record.at)
    if record.level in ("WARNING", "CRITICAL", "FATAL"):
        return Action("error", msg, at=record.at)
    if first.startswith(IGNORED_PREFIXES):
        return None
    return None


# --- following the file --------------------------------------------------------------


@dataclass
class TailState:
    offset: int = 0
    partial: str = ""
    open_run_id: int | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    suppressed: int = 0

    def to_json(self):
        return {"offset": self.offset, "partial": self.partial, "open_run_id": self.open_run_id}

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(offset=int(data.get("offset", 0)), partial=data.get("partial", ""),
                       open_run_id=data.get("open_run_id"))
        except (OSError, ValueError):
            return cls()

    def save(self, path):
        try:
            Path(path).write_text(json.dumps(self.to_json()), encoding="utf-8")
        except OSError:
            pass


def read_new(path, state: TailState) -> str:
    """New text since the last read; handles copy-and-truncate rotation."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ""
    if size < state.offset:
        state.offset = 0          # truncated by rotation (logs.rotate_if_needed)
        state.partial = ""
    if size == state.offset:
        return ""
    with open(path, "rb") as handle:
        handle.seek(state.offset)
        chunk = handle.read(min(size - state.offset, MAX_READ))
    state.offset += len(chunk)
    text = state.partial + chunk.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        state.partial = ""
        return text
    text, _, state.partial = text.rpartition("\n")
    return text + "\n" if text else ""


# --- applying to the registry -------------------------------------------------------------


def apply(project, actions, state: TailState, now=None) -> dict:
    """Write runs and events for `actions`; returns counts for logging."""
    now = now or timezone.now()
    counts = {"runs": 0, "events": 0, "suppressed": 0}

    def open_run():
        if state.open_run_id is None:
            return None
        return SyncRun.objects.filter(pk=state.open_run_id, ok__isnull=True).first()

    for action in actions:
        at = action.at or now
        if action.kind == "pull_open":
            previous = open_run()
            if previous is not None:
                # A new pull before the check confirmed the old one: leave it
                # unknown (ok null); the check closes whichever versions the
                # database reaches.
                pass
            run = SyncRun.objects.create(project=project, kind=SyncRun.Kind.PULL, started_at=at,
                                         checkout_version=action.version, summary={"line": action.message})
            state.open_run_id = run.pk
            counts["runs"] += 1
        elif action.kind == "pull_failed":
            run = open_run()
            if run is not None:
                run.ok = False
                run.finished_at = at
                run.summary = {**(run.summary or {}), "failed": action.message}
                run.save()
            state.open_run_id = None
        elif action.kind == "push_ok":
            SyncRun.objects.create(project=project, kind=SyncRun.Kind.PUSH, started_at=at, finished_at=at, ok=True,
                                   checkout_version=action.version, summary={"line": action.message})
            counts["runs"] += 1
        elif action.kind == "push_failed":
            SyncRun.objects.create(project=project, kind=SyncRun.Kind.PUSH, started_at=at, finished_at=at, ok=False,
                                   summary={"line": action.message})
            counts["runs"] += 1
        elif action.kind == "info":
            events.record(project, Event.Level.INFO, action.message, source=Event.Source.DBSYNC)
            counts["events"] += 1
        elif action.kind == "error":
            first = action.message.split("\n", 1)[0]
            if action.message.startswith("GEODIFF:"):
                run = open_run()
                if run is not None:
                    run.ok = False
                    run.finished_at = at
                    run.summary = {**(run.summary or {}), "failed": first}
                    run.save()
                    state.open_run_id = None
            same = state.last_error == action.message
            recent = state.last_error_at is not None and now - state.last_error_at < DEDUP_WINDOW
            if same and recent:
                state.suppressed += 1
                counts["suppressed"] += 1
                continue
            text = action.message
            if state.suppressed:
                text += f"\n(previous error repeated {state.suppressed} more times)"
            events.record(project, Event.Level.ERROR, text, source=Event.Source.DBSYNC)
            state.last_error, state.last_error_at, state.suppressed = action.message, now, 0
            counts["events"] += 1
    return counts


def pump(project, log_path, state_path, state: TailState) -> dict:
    """One tick's worth: read, parse, classify, apply, persist the offset."""
    text = read_new(log_path, state)
    if not text:
        return {}
    actions = [a for a in (classify(r) for r in parse(text)) if a is not None]
    counts = apply(project, actions, state) if actions else {}
    state.save(state_path)
    return counts


def close_confirmed_pulls(project, db_version) -> int:
    """The check saw the database at db_version: pulls up to it are confirmed."""
    if db_version is None:
        return 0
    return SyncRun.objects.filter(
        project=project, kind=SyncRun.Kind.PULL, ok__isnull=True, checkout_version__lte=db_version,
    ).update(ok=True, finished_at=timezone.now())
