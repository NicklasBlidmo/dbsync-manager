"""
Per-project log files (design §10 housekeeping).

db-sync opens its --log-file in append mode and never rotates it, and a
renamed file would keep receiving its writes. Rotation therefore copies the
content to <name>.1 (shifting older copies up) and truncates the file in
place; an O_APPEND writer continues at the new end without noticing.
"""

import os
import shutil
from collections import deque
from pathlib import Path


def rotate_if_needed(path, max_bytes: int, keep: int) -> bool:
    """Rotate `path` when it exceeds max_bytes. Returns True if it did."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= max_bytes:
        return False

    for index in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index + 1}")
        if older.exists():
            if index + 1 > keep - 1 and keep > 0:
                # would become .keep+1 -> drop it
                pass
            os.replace(older, newer)
    first = path.with_name(f"{path.name}.1")
    shutil.copyfile(path, first)
    with open(path, "r+b") as handle:
        handle.truncate(0)

    # Drop copies beyond `keep`.
    index = keep + 1
    while True:
        extra = path.with_name(f"{path.name}.{index}")
        if not extra.exists():
            break
        extra.unlink()
        index += 1
    return True


def tail(path, lines: int = 20, max_bytes: int = 64 * 1024) -> str:
    """Last `lines` of a text file, reading at most max_bytes from its end."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ""
    with open(path, "rb") as handle:
        handle.seek(max(0, size - max_bytes))
        chunk = handle.read()
    text = chunk.decode("utf-8", errors="replace")
    return "\n".join(deque(text.splitlines(), maxlen=lines))
