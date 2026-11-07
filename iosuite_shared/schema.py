"""Schema helpers every app's database code uses: what tables, columns and indexes exist, and the schema_version table.

The suite's naming convention: tables `<app prefix>_<name>`, `*_at` timestamps, `name`/`value` key-value columns,
`idx_`/`uq_` index names.

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
