"""
Pull/push history grouped per version (slice 10 brief, item 3).

One pull touches every connection, so the log yields one `sync_run` row
per file per pull; the page shows one row per pull instead. Rows of the
same kind and checkout version whose start times lie within `window`
seconds of each other form a group; the group's result is the worst of
its rows, and a group that is not uniform is expanded by default.
"""

STATE_ORDER = {"failed": 0, "checkout only": 1, "unknown": 1, "applied": 2, "ok": 2}


def run_state(run) -> str:
    if run.ok is True:
        return "applied" if run.kind == "pull" else "ok"
    if run.ok is False:
        return "failed"
    return "checkout only" if run.kind == "pull" else "unknown"


def state_class(state: str) -> str:
    if state in ("applied", "ok"):
        return "ok"
    if state == "failed":
        return "bad"
    return "info"


def group_runs(runs, window_seconds: int, total_connections: int, limit: int = 20) -> list:
    """`runs` newest first. Returns at most `limit` groups, newest first."""
    groups = []
    for run in runs:
        current = groups[-1] if groups else None
        if (
            current is not None
            and current["kind"] == run.kind
            and current["version"] == run.checkout_version
            and (current["oldest"] - run.started_at).total_seconds() <= window_seconds
        ):
            current["runs"].append(run)
            current["oldest"] = run.started_at
        else:
            if len(groups) >= limit:
                break
            groups.append({"kind": run.kind, "version": run.checkout_version, "runs": [run],
                           "oldest": run.started_at, "newest": run.started_at})

    for group in groups:
        rows = []
        for run in group["runs"]:
            state = run_state(run)
            summary = run.summary or {}
            rows.append({"run": run, "state": state, "state_class": state_class(state),
                         "detail": summary.get("failed") or summary.get("line", "")})
        states = [r["state"] for r in rows]
        worst = min(states, key=lambda s: STATE_ORDER.get(s, 1))
        group["rows"] = rows
        group["state"] = worst
        group["state_class"] = state_class(worst)
        group["started"] = group["oldest"]
        group["files"] = f"{len(rows)} of {total_connections}" if total_connections else str(len(rows))
        group["uniform"] = len(set(states)) == 1
        group["expanded"] = not group["uniform"]
        group["key"] = f"run-{group['kind']}-{group['version']}-{group['oldest'].strftime('%Y%m%d%H%M%S')}"
    return groups
