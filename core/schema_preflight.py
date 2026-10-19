"""Reusable, read-only metadata validation for Database-owned tables.

Only information_schema SELECTs are issued. Never create, alter or repair
schema from Coddy or the API.
"""
from collections import defaultdict
from collections.abc import Collection, Mapping


def check_schema_readonly(
    cursor,
    *,
    required_columns: Mapping[str, Collection[str]],
    required_unique_keys: Mapping[str, tuple[str, ...]],
    error_prefix: str,
    guidance: str,
) -> None:
    """Fail closed when a table, required column or exact unique key is missing."""
    if not required_columns:
        raise ValueError("required_columns must not be empty")
    if set(required_unique_keys) - set(required_columns):
        raise ValueError("unique keys must refer to checked tables")

    table_names = tuple(sorted(required_columns))
    placeholders = ", ".join("%s" for _ in table_names)
    cursor.execute(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        f"AND TABLE_NAME IN ({placeholders})",
        table_names,
    )
    columns: dict[str, set[str]] = defaultdict(set)
    for row in cursor.fetchall() or []:
        columns[str(row["TABLE_NAME"])].add(str(row["COLUMN_NAME"]))
    missing = [
        f"{table}.{column}"
        for table, required in sorted(required_columns.items())
        for column in sorted(set(required) - columns[table])
    ]
    if missing:
        raise RuntimeError(
            f"{error_prefix}_missing_columns:" + ",".join(missing) + "; " + guidance
        )

    cursor.execute(
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        f"AND TABLE_NAME IN ({placeholders}) "
        "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
        table_names,
    )
    indexes: dict[tuple[str, str], list[tuple[int, str, int]]] = defaultdict(list)
    for row in cursor.fetchall() or []:
        indexes[(str(row["TABLE_NAME"]), str(row["INDEX_NAME"]))].append(
            (int(row["SEQ_IN_INDEX"]), str(row["COLUMN_NAME"]), int(row["NON_UNIQUE"]))
        )
    unique_sets: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for (table, _index_name), segments in indexes.items():
        if segments and all(non_unique == 0 for _, _, non_unique in segments):
            unique_sets[table].add(tuple(col for _, col, _ in sorted(segments)))
    missing_keys = [
        table for table, expected in required_unique_keys.items()
        if expected not in unique_sets[table]
    ]
    if missing_keys:
        raise RuntimeError(
            f"{error_prefix}_missing_unique_keys:"
            + ",".join(sorted(missing_keys)) + "; " + guidance
        )
