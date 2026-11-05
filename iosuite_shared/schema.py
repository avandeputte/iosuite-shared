"""Schema helpers every app's database code uses: one-time renames that keep every row, and the schema_version table.

The suite's naming convention (tables `<app prefix>_<name>`, `*_at` timestamps, `name`/`value` key-value columns,
`idx_`/`uq_` index names) came in with iosuite-shared 0.8.0. An install from before it has the old names; each app
calls rename() at start with the renames it needs, which does what is still to do and nothing more. RENAME TABLE,
RENAME COLUMN and RENAME INDEX are metadata changes on MariaDB: instant and lossless even on a big table.

The functions take a PyMySQL cursor (tuple or dict rows) and run on the cursor's database.
"""


def _rows(cur, sql: str, args=None) -> list:
    cur.execute(sql, args)
    return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in cur.fetchall()]


def tables(cur) -> set:
    return {r[0] for r in _rows(cur, "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")}


def columns(cur, table: str) -> set:
    return {r[0] for r in _rows(cur, "SELECT column_name FROM information_schema.columns "
                                     "WHERE table_schema = DATABASE() AND table_name = %s", (table,))}


def indexes(cur, table: str) -> set:
    return {r[0] for r in _rows(cur, "SELECT DISTINCT index_name FROM information_schema.statistics "
                                     "WHERE table_schema = DATABASE() AND table_name = %s", (table,))}


def rename(cur, tables_: dict = None, columns_: dict = None, indexes_: dict = None) -> list:
    """Rename what still has its old name, in this order: tables {old: new}, then columns {table: {old: new}} and
    indexes {table: {old: new}}, both keyed by the NEW table name. A table is renamed only when the old one exists and
    the new one does not; a column or index only when the old name exists and the new one does not. Returns what was
    done, as "old -> new" lines (empty on a database already up to date)."""
    done = []
    have = tables(cur)
    for old, new in (tables_ or {}).items():
        if old in have and new not in have:
            cur.execute(f"RENAME TABLE `{old}` TO `{new}`")
            have.discard(old)
            have.add(new)
            done.append(f"{old} -> {new}")
    for table, pairs in (columns_ or {}).items():
        if table not in have:
            continue
        cols = columns(cur, table)
        for old, new in pairs.items():
            if old in cols and new not in cols:
                cur.execute(f"ALTER TABLE `{table}` RENAME COLUMN `{old}` TO `{new}`")
                cols.discard(old)
                cols.add(new)
                done.append(f"{table}.{old} -> {new}")
    for table, pairs in (indexes_ or {}).items():
        if table not in have:
            continue
        idx = indexes(cur, table)
        for old, new in pairs.items():
            if old in idx and new not in idx:
                cur.execute(f"ALTER TABLE `{table}` RENAME INDEX `{old}` TO `{new}`")
                idx.discard(old)
                idx.add(new)
                done.append(f"{table} index {old} -> {new}")
    return done


def version_table(prefix: str) -> str:
    return f"{prefix}_schema_version"


def ensure_version_table(cur, prefix: str, options: str = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"):
    cur.execute(f"CREATE TABLE IF NOT EXISTS {version_table(prefix)} (version INT PRIMARY KEY, applied_at DATETIME(3) NOT NULL)" + options)


def current_version(cur, prefix: str) -> int:
    rows = _rows(cur, f"SELECT COALESCE(MAX(version), 0) FROM {version_table(prefix)}")
    return int(rows[0][0]) if rows else 0


def record_version(cur, prefix: str, version: int):
    """Record that the schema is at `version` (idempotent)."""
    cur.execute(f"INSERT IGNORE INTO {version_table(prefix)} (version, applied_at) VALUES (%s, UTC_TIMESTAMP(3))", (version,))
