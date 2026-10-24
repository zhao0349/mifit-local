"""Completed cloud-query days, stored inside each account's SQLite cache.

Record presence and the upstream timestamp watermark cannot prove completeness.
Only successful queries without rejected records establish coverage, including
empty results. The current Beijing date is still open and is never covered.
"""

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import sqlite3

TABLE = "mifit_sync_days"


class SyncCoverage:
    def __init__(self, path: Path, today: date):
        self.path = path
        self.today = today

    def pending(self, data_type: str, start: str, end: str):
        """Return missing contiguous ranges of at most seven days and days skipped."""
        covered = set()
        if self.path.exists():
            with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
                    covered = {row[0] for row in connection.execute(
                        f"SELECT date FROM {TABLE} WHERE data_type=? AND date BETWEEN ? AND ? AND date<?",
                        (data_type, start, end, self.today.isoformat()),
                    )}
        cursor, last = date.fromisoformat(start), date.fromisoformat(end)
        ranges, first, previous = [], None, None
        while cursor <= last:
            if cursor.isoformat() in covered:
                if first is not None:
                    ranges.append((first.isoformat(), previous.isoformat()))
                    first = None
            else:
                if first is None:
                    first = cursor
                previous = cursor
                if (cursor - first).days == 6:
                    ranges.append((first.isoformat(), cursor.isoformat()))
                    first = None
            cursor += timedelta(days=1)
        if first is not None:
            ranges.append((first.isoformat(), previous.isoformat()))
        return ranges, len(covered)

    def complete(self, data_type: str, start: str, end: str):
        """Persist only closed dates after all records in the query were saved."""
        first = date.fromisoformat(start)
        last = min(date.fromisoformat(end), self.today - timedelta(days=1))
        if first > last:
            return
        days = [(data_type, (first + timedelta(days=i)).isoformat())
                for i in range((last - first).days + 1)]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                               "data_type TEXT NOT NULL, date TEXT NOT NULL, PRIMARY KEY(data_type,date))")
            connection.executemany(f"INSERT OR IGNORE INTO {TABLE} (data_type,date) VALUES (?,?)", days)
