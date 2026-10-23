"""Known export health columns, shared with the browser; no account identifiers."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "dashboard" / "record-schema.json"
MAX_ROWS = 2_000_000
MAX_CELLS = 40_000_000


def _value(raw, kind):
    if raw is None:
        return None
    if kind == "text":
        if not isinstance(raw, str):
            return None
        if len(raw) > 1024:
            raise ValueError("record text limit")
        return raw
    if kind == "boolean":
        return True if raw in (True, 1, "1") else False if raw in (False, 0, "0") else None
    if kind == "stages":
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        if not isinstance(raw, list):
            return None
        if len(raw) > 10000:
            raise ValueError("stage limit")
        return [
            {
                "stage": item.get("stage") if item.get("stage") in
                ("deep", "light", "rem", "awake") else "unknown",
                "minutes": _value(item.get("minutes"), "number"),
            }
            for item in raw if isinstance(item, dict)
        ]
    if isinstance(raw, str):
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw.strip()):
            return None
        raw = float(raw)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw if math.isfinite(raw) else None


def read_archive(connection, tables, available):
    """Stream known columns in the caller's read-only transaction.

    Preserve source precision, nulls, signed altitude/offsets and duplicate rows.
    Bound the retained archive; never silently truncate a successful import.
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    archive = {"version": 1, "datasets": {}}
    row_count = cell_count = 0
    for source, table in tables.items():
        if table not in available:
            continue
        fields = [field for field in schema[source]["fields"] if field[0] in available[table]]
        # Every name comes from the bundled schema, never from an imported identifier.
        selected = ", ".join(f'"{field[0]}"' for field in fields) or "NULL"
        rows = []
        for row in connection.execute(f'SELECT {selected} FROM "{table}"'):
            row_count += 1
            cell_count += len(fields)
            if row_count > MAX_ROWS or cell_count > MAX_CELLS:
                raise ValueError("record limit")
            values = [_value(row[field[0]], field[3]) for field in fields]
            cell_count += sum(len(value) * 2 for value in values if isinstance(value, list))
            if cell_count > MAX_CELLS:
                raise ValueError("record limit")
            rows.append(values)
        archive["datasets"][source] = {"fields": [field[0] for field in fields], "rows": rows}
    return archive
