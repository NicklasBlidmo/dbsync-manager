"""
Content divergence base <-> modified (slice 15, A).

The counts in check.py miss an UPDATE: same number of rows, different
content. For every table whose counts agree, one statement joins base and
modified on the GeoPackage primary key (FULL OUTER JOIN) and reports the
rows that differ: only in base, only in modified, or in both with
different content, with the names of the differing columns. Attributes
are compared column by column with IS DISTINCT FROM; geometries as byte
equality of their EWKB, which is robust to how SRID and dimension are
represented. No row data comes back except a sample capped at SAMPLE_CAP;
the three totals ride along as window counts, so one query does it.

What a deviation means depends on the direction (check.evaluate): in a
one-way project nobody should write into modified, so it is a condition;
in a two-way project it is the normal state between push cycles.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

SAMPLE_CAP = 200
KINDS = ("only_base", "only_modified", "changed")
DEFAULT_KEY = "fid"
UUID_COLUMN = "uuid"


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def tables_to_compare(tables, base_count: dict, modified_count: dict) -> list:
    """The tables worth comparing: present in both schemas with equal counts
    (a count difference is covered by the count conditions already)."""
    return [t for t in tables if t in base_count and t in modified_count and base_count[t] == modified_count[t]]


def gpkg_primary_keys(gpkg_path, tables) -> dict:
    """{table: primary key column} from PRAGMA table_info; fid when none is declared."""
    keys = {}
    with closing(sqlite3.connect(f"file:{Path(gpkg_path).as_posix()}?mode=ro", uri=True)) as conn:
        for table in tables:
            rows = conn.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            pk = [row[1] for row in rows if row[5]]
            keys[table] = pk[0] if len(pk) == 1 else DEFAULT_KEY
    return keys


def gpkg_geometry_columns(gpkg_path) -> dict:
    """{table: geometry column} from gpkg_geometry_columns."""
    with closing(sqlite3.connect(f"file:{Path(gpkg_path).as_posix()}?mode=ro", uri=True)) as conn:
        try:
            rows = conn.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchall()
        except sqlite3.DatabaseError:
            return {}
    return {table: column for table, column in rows}


def schema_columns(cursor, schema: str) -> dict:
    """{table: [(column, udt_name), ...]} in column order, for every table of a schema."""
    cursor.execute(
        "SELECT table_name, column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        [schema],
    )
    columns = {}
    for table, column, udt in cursor.fetchall():
        columns.setdefault(table, []).append((column, udt))
    return columns


def plan(table: str, key: str, base_columns, modified_columns) -> dict | None:
    """The columns to compare for one table, or None when the key is missing."""
    base = dict(base_columns)
    modified = dict(modified_columns)
    common = [name for name, _ in base_columns if name in modified]
    if key not in common:
        return None
    geometry = [name for name in common if base[name] == "geometry" and modified[name] == "geometry"]
    uuid = next((name for name in common if name.lower() == UUID_COLUMN), None)
    return {"table": table, "key": key, "columns": [c for c in common if c != key], "geometry": geometry, "uuid": uuid}


def diff_sql(base_schema: str, modified_schema: str, spec: dict) -> str:
    """One statement for one table pair: the differing rows (capped) and the three totals."""
    k = quote(spec["key"])
    compare = []
    flags = []
    for column in spec["columns"]:
        q = quote(column)
        if column in spec["geometry"]:
            expr = f"ST_AsEWKB(b.{q}) IS DISTINCT FROM ST_AsEWKB(m.{q})"
        else:
            expr = f"b.{q} IS DISTINCT FROM m.{q}"
        compare.append(expr)
        flags.append(f"CASE WHEN {expr} THEN {_literal(column)} END")
    changed = " OR ".join(compare) if compare else "FALSE"
    columns_expr = f"ARRAY_REMOVE(ARRAY[{', '.join(flags)}]::text[], NULL)" if flags else "ARRAY[]::text[]"
    if spec["uuid"]:
        u = quote(spec["uuid"])
        uuid_expr = f"COALESCE(m.{u}, b.{u})::text"
    else:
        uuid_expr = "NULL::text"
    b = f"{quote(base_schema)}.{quote(spec['table'])}"
    m = f"{quote(modified_schema)}.{quote(spec['table'])}"
    return (
        "WITH d AS (\n"
        f"    SELECT CASE WHEN b.{k} IS NULL THEN 'only_modified' WHEN m.{k} IS NULL THEN 'only_base' ELSE 'changed' END AS kind,\n"
        f"           COALESCE(m.{k}, b.{k})::text AS key, {uuid_expr} AS uuid, {columns_expr} AS columns\n"
        f"    FROM {b} b FULL OUTER JOIN {m} m ON b.{k} = m.{k}\n"
        f"    WHERE b.{k} IS NULL OR m.{k} IS NULL OR ({changed})\n"
        ")\n"
        "SELECT kind, key, uuid, columns,\n"
        "       count(*) FILTER (WHERE kind = 'only_base') OVER () AS n_only_base,\n"
        "       count(*) FILTER (WHERE kind = 'only_modified') OVER () AS n_only_modified,\n"
        "       count(*) FILTER (WHERE kind = 'changed') OVER () AS n_changed\n"
        f"FROM d ORDER BY key LIMIT {SAMPLE_CAP}"
    )


def shape(rows) -> dict:
    """The stored result of one table: three counts and the capped sample."""
    result = {kind: 0 for kind in KINDS}
    result["sample"] = []
    for kind, key, uuid, columns, n_only_base, n_only_modified, n_changed in rows:
        result["only_base"], result["only_modified"], result["changed"] = n_only_base, n_only_modified, n_changed
        result["sample"].append({"key": key, "uuid": uuid, "kind": kind, "columns": list(columns or [])})
    return result


def total(result: dict) -> int:
    return sum(int(result.get(kind, 0) or 0) for kind in KINDS)


def compare_table(cursor, base_schema: str, modified_schema: str, spec: dict) -> dict:
    cursor.execute(diff_sql(base_schema, modified_schema, spec))
    return shape(cursor.fetchall())


def summarize(result: dict) -> str:
    return (f"{result.get('changed', 0)} changed, {result.get('only_modified', 0)} only in modified, "
            f"{result.get('only_base', 0)} only in base")
