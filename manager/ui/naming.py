"""
Schema names from a prefix and a GeoPackage file name (design §9, §15).

    <prefix>_s_<stem>   modified schema
    <prefix>_b_<stem>   base schema

`stem` is the file name without extension and without a leading ordering
token ("1-punkter.gpkg" → "punkter", "d-foto.gpkg" → "foto"), lower-cased
with `_` for anything outside [a-z0-9_]. Swedish letters are transliterated
rather than dropped. The prefix defaults to the Mergin project name made
into an identifier the same way; a leading digit gets a "p" in front, so
"3922-bonsta" becomes "p3922_bonsta". Everything is cut to PostgreSQL's 63
characters. All of it is editable in the wizard; this is only the default.
"""

import re
import unicodedata
from pathlib import PurePosixPath

MAX_IDENTIFIER = 63
_TRANSLIT = str.maketrans({"å": "a", "ä": "a", "ö": "o", "é": "e", "ü": "u", "ø": "o", "æ": "ae"})
_ORDERING_TOKEN = re.compile(r"^(\d{1,3}|[a-zA-Z])-")
_VALID = re.compile(r"^[a-z_][a-z0-9_]*$")


def sanitize_identifier(text: str) -> str:
    text = (text or "").strip().lower().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if text and text[0].isdigit():
        text = "p" + text
    return text[:MAX_IDENTIFIER]


def is_valid_identifier(text: str) -> bool:
    return bool(text) and len(text) <= MAX_IDENTIFIER and _VALID.match(text) is not None


def default_prefix(project_path: str) -> str:
    name = project_path.split("/", 1)[-1]
    return sanitize_identifier(name) or "project"


def stem_of(sync_file: str) -> str:
    name = PurePosixPath(sync_file).name
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = _ORDERING_TOKEN.sub("", name, count=1)
    return sanitize_identifier(name) or "data"


def derive_schema_names(prefix: str, sync_file: str) -> tuple[str, str]:
    stem = stem_of(sync_file)
    modified = f"{prefix}_s_{stem}"[:MAX_IDENTIFIER]
    base = f"{prefix}_b_{stem}"[:MAX_IDENTIFIER]
    return modified, base


def is_conflict_copy(path: str) -> bool:
    """Mergin writes edit conflicts as '<name> (edit conflict, <user> v<N>).gpkg'."""
    return "(edit conflict" in PurePosixPath(path).name


def conflict_original(path: str) -> str:
    """'a/1-punkter (edit conflict, x v3).gpkg' -> 'a/1-punkter.gpkg'."""
    p = PurePosixPath(path)
    stem = p.name.split(" (edit conflict", 1)[0]
    suffix = p.suffix
    return str(p.with_name(stem + suffix))
