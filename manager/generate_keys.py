#!/usr/bin/env python3
"""
Generate DJANGO_SECRET_KEY and DBSYNC_MANAGER_SECRET_KEY for .env.

Standard library only and independent of Django on purpose: the keys are
needed *before* the settings can be imported (both are required there), and
before the container can start (its entrypoint runs migrate first). So this
must run on a bare host or with the image's entrypoint bypassed:

    python3 manager/generate_keys.py --write .env
    docker compose run --rm --no-deps --entrypoint python manager generate_keys.py

Both values are URL-safe base64. Django's own get_random_secret_key() is not
used because it emits `$` and `#`, which Compose interpolates or treats as a
comment in an env file, silently changing the key.
"""

import argparse
import base64
import os
import re
import secrets
import sys

KEYS = ("DJANGO_SECRET_KEY", "DBSYNC_MANAGER_SECRET_KEY")


def django_secret_key():
    return secrets.token_urlsafe(50)


def fernet_key():
    # Same format as cryptography.fernet.Fernet.generate_key().
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def new_values():
    return {"DJANGO_SECRET_KEY": django_secret_key(), "DBSYNC_MANAGER_SECRET_KEY": fernet_key()}


def fill_env_text(text, values, force=False):
    """Return (new_text, report) with each key set exactly once.

    An existing non-empty value is kept unless `force`: replacing
    DBSYNC_MANAGER_SECRET_KEY makes every stored credential unreadable.
    The value goes on the key's existing line, and any further lines for the
    same key are dropped. With duplicates the last one wins in an env file, so
    an empty template line left below a filled-in one yields an empty key.
    """
    lines = text.splitlines()
    report = {}
    for key, value in values.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$")
        hits = [(i, m.group(1).strip()) for i, line in enumerate(lines) if (m := pattern.match(line))]
        existing = next((v for _, v in hits if v), "")
        if existing and not force:
            chosen, report[key] = existing, "kept"
        else:
            chosen, report[key] = value, "replaced" if existing else "set"
        if hits:
            first = hits[0][0]
            lines[first] = f"{key}={chosen}"
            for i, _ in reversed(hits[1:]):
                del lines[i]
            if len(hits) > 1:
                report[key] += f", {len(hits) - 1} duplicate line(s) removed"
        else:
            lines.append(f"{key}={chosen}")
    return "\n".join(lines) + "\n", report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", metavar="ENV_FILE", help="fill the keys into this file in place")
    parser.add_argument("--force", action="store_true", help="with --write: replace existing values too")
    args = parser.parse_args(argv)

    values = new_values()
    if not args.write:
        for key in KEYS:
            print(f"{key}={values[key]}")
        return 0

    try:
        with open(args.write, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        print(f"{args.write} does not exist; copy .env.example to it first", file=sys.stderr)
        return 1
    new_text, report = fill_env_text(text, values, force=args.force)
    with open(args.write, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(new_text)
    for key in KEYS:
        print(f"{key}: {report[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
