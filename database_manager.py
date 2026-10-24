"""Account-scoped cache inventory and confirmed deletion for the local dashboard.

Only managed SQLite caches are touched. Credentials, exports, and files already
loaded in a browser are independent copies and are never removed here. Public
methods acquire the same locks as synchronization and export. A deletion token
is short-lived, single-use, and bound to the complete database and WAL contents.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import desktop_backend as backend


# Categories are whole CSV datasets; related detail tables cannot be selected
# separately. Sleep belongs to its waking date, as in the dashboard.
CATEGORIES = (
    ("daily_activity", "日常活动"), ("heart_rate", "心率"),
    ("sleep", "睡眠"), ("body_measurements", "体重"),
    ("spo2", "血氧"), ("stress", "压力"), ("workouts", "运动记录"),
)
TABLES = {
    "daily_activity": (("daily_activity", "date"),),
    "heart_rate": (("heart_rate_samples", "timestamp"),
                   ("abnormal_heart_beat_events", "start_at")),
    "sleep": (("sleep_sessions", "end_at"),),
    "body_measurements": (("body_measurements", "timestamp"),),
    "spo2": (("spo2_samples", "timestamp"),),
    "stress": (("stress_samples", "timestamp"),),
    "workouts": (("workouts", "start_at"), ("workout_detail_samples", "timestamp"),
                 ("workout_gps_points", "timestamp")),
}
CHILD_TABLES = ("workout_detail_samples", "workout_gps_points")
TTL_SECONDS = 300
BUSY = "已有任务正在运行，请完成或取消后再管理数据库。"


@contextmanager
def _locked():
    if not backend.interface_lock.acquire(blocking=False):
        raise backend.DesktopError(BUSY)
    try:
        if not backend._operation_lock.acquire(blocking=False):
            raise backend.DesktopError(BUSY)
        try:
            yield
        finally:
            backend._operation_lock.release()
    finally:
        backend.interface_lock.release()


def _root() -> Path:
    intended = backend.DATA_DIR.absolute()
    if intended.resolve() != intended or not intended.is_relative_to(backend.ROOT.resolve()):
        raise backend.DesktopError("数据库目录不能重定向到其他位置。")
    return intended


def _path(key: str) -> Path:
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("必须选择一个本机账号，不能操作全部账号。")
    intended = _root() / key / "mi_fitness.db"
    if intended.resolve() != intended or not intended.is_file():
        raise backend.DesktopError("所选账号数据库不存在或路径已变化，请刷新列表。")
    # Reject redirects and hard links for both the database and SQLite sidecars.
    for candidate in (intended, *(Path(str(intended) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        if candidate.is_symlink() or candidate.resolve() != candidate:
            raise backend.DesktopError("数据库文件不能重定向到其他位置。")
        if candidate.exists() and (not candidate.is_file() or candidate.stat().st_nlink > 1):
            raise backend.DesktopError("数据库文件类型或链接状态不安全，已停止操作。")
    return intended


def _bytes(path: Path) -> dict:
    sizes = {}
    for name, suffix in (("db_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
        candidate = Path(str(path) + suffix)
        try:
            sizes[name] = candidate.stat().st_size
        except FileNotFoundError:
            sizes[name] = 0
    sizes["database_bytes"] = sum(sizes.values())
    return sizes


def _fingerprint(path: Path) -> tuple:
    """Include WAL pages, not just the main-file mtime or SQLite change counter.

    SHM only coordinates reader locks and can change during read-only inspection;
    it contains no authoritative database pages and is excluded from the digest.
    """
    values = []
    for suffix in ("", "-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            before = candidate.stat()
            # SQLite can create an empty WAL while opening a fully checkpointed
            # database, even for a reader. It carries no changed database pages.
            if suffix and before.st_size == 0:
                values.append((suffix, None))
                continue
            digest = hashlib.sha256()
            with candidate.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            after = candidate.stat()
            def identity(stat):
                return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if identity(before) != identity(after):
                raise backend.DesktopError("数据库正在变化，请稍后重新确认。")
            values.append((suffix, identity(after), digest.digest()))
        except FileNotFoundError:
            if not suffix:
                raise backend.DesktopError("数据库已变化，请刷新列表后重新确认。") from None
            values.append((suffix, None))
    return tuple(values)


def _day(value):
    try:
        return backend._export_local_date(value)
    except (ValueError, TypeError):
        return None


def _connect(path: Path, *, write=False):
    connection = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if write else 'ro'}", uri=True, timeout=0.2)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.create_function("mifit_day", 1, _day, deterministic=True)
    return connection


def _schema(connection) -> set[str]:
    entries = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    known = {table for entries in TABLES.values() for table, _ in entries} | {"sync_state"}
    tables = {name for kind, name in entries if kind == "table"}
    if tables - known or any(kind in ("view", "trigger") for kind, _ in entries):
        raise backend.DesktopError("数据库结构不受支持，未执行删除。")
    if not tables.intersection(known - {"sync_state"}):
        raise backend.DesktopError("数据库不包含可识别的健康记录表。")
    for entries in TABLES.values():
        for table, column in entries:
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            required = {"user_id", column}
            if table in ("workouts", *CHILD_TABLES):
                required.add("workout_id")
            if not required.issubset(columns):
                raise backend.DesktopError("数据库结构不完整，未执行删除。")
    return tables


def _identity(connection, key, tables):
    users = set()
    for entries in TABLES.values():
        for table, _ in entries:
            if table in tables:
                users.update(row[0] for row in connection.execute(f'SELECT DISTINCT user_id FROM "{table}" LIMIT 2'))
                if len(users) > 1:
                    raise backend.DesktopError("数据库中存在多个账号，无法安全执行账号级管理。")
    if not users:
        return {"account_key": key, "label": f"空数据库 · {key[:12]}",
                "account_masked": "", "region": ""}
    user = next(iter(users))
    if not isinstance(user, str) or not re.fullmatch(r"[0-9]{1,20}", user):
        raise backend.DesktopError("数据库账号标识无效，未执行删除。")
    region = next((region for region in sorted(backend.REGIONS) if backend._account_key(user, region) == key), None)
    if region is None:
        raise backend.DesktopError("数据库账号与存储目录不匹配，未执行删除。")
    masked = backend._mask(user)
    return {"account_key": key, "label": f"账号 {masked} · {region} · {key[:8]}",
            "account_masked": masked, "region": region}


def _date_expression(table, column, tables, alias="r"):
    if table in CHILD_TABLES and "workouts" in tables:
        return (f'COALESCE((SELECT p.start_at FROM workouts p WHERE p.user_id={alias}.user_id '
                f'AND p.workout_id={alias}.workout_id LIMIT 1), {alias}."{column}")')
    return f'{alias}."{column}"'


def _stats(connection, tables):
    categories = []
    for key, label in CATEGORIES:
        count = invalid = 0
        starts, ends = [], []
        for table, column in TABLES[key]:
            if table not in tables:
                continue
            expression = _date_expression(table, column, tables)
            row = connection.execute(
                f'SELECT COUNT(*), MIN(mifit_day({expression})), MAX(mifit_day({expression})), '
                f'COUNT(*)-COUNT(mifit_day({expression})) FROM "{table}" r'
            ).fetchone()
            count += row[0]
            invalid += row[3]
            if row[1]:
                starts.append(row[1])
                ends.append(row[2])
        categories.append({"key": key, "label": label, "count": count,
                           "start": min(starts, default=None), "end": max(ends, default=None),
                           "invalid_time_rows": invalid})
    starts = [item["start"] for item in categories if item["start"]]
    ends = [item["end"] for item in categories if item["end"]]
    return {"categories": categories, "total_rows": sum(item["count"] for item in categories),
            "start": min(starts, default=None), "end": max(ends, default=None)}


def _filters(start, end, categories):
    if start is not None:
        backend.validate_dates(start, start)
    if end is not None:
        backend.validate_dates(end, end)
    if start is not None and end is not None:
        backend.validate_dates(start, end)
    if categories is not None and (not isinstance(categories, list) or
                                  any(not isinstance(key, str) or key not in TABLES for key in categories)):
        raise ValueError("只能选择七个完整数据类别。")
    return start, end, tuple(key for key, _ in CATEGORIES if not categories or key in categories)


def _range(expression, start, end):
    clauses, values = [], []
    if start:
        clauses.append(f"mifit_day({expression}) >= ?")
        values.append(start)
    if end:
        clauses.append(f"mifit_day({expression}) <= ?")
        values.append(end)
    return " AND ".join(clauses) or "1", values


def _predicate(table, column, tables, start, end):
    if table not in CHILD_TABLES or not (start or end):
        return _range(f'r."{column}"', start, end)
    # Select an entire workout by its start date. Orphaned detail groups use any
    # sample's date, but all samples and GPS points of that group are removed.
    same = "p.user_id=r.user_id AND p.workout_id=r.workout_id"
    parent = f"EXISTS (SELECT 1 FROM workouts p WHERE {same})" if "workouts" in tables else "0"
    clauses, values = [], []
    if "workouts" in tables:
        date_clause, date_values = _range("p.start_at", start, end)
        clauses.append(f"EXISTS (SELECT 1 FROM workouts p WHERE {same} AND {date_clause})")
        values.extend(date_values)
    orphans, orphan_values = [], []
    for child in CHILD_TABLES:
        if child in tables:
            date_clause, date_values = _range("p.timestamp", start, end)
            orphans.append(f'EXISTS (SELECT 1 FROM "{child}" p WHERE {same} AND {date_clause})')
            orphan_values.extend(date_values)
    clauses.append(f"(NOT ({parent}) AND ({' OR '.join(orphans)}))")
    values.extend(orphan_values)
    return " OR ".join(f"({clause})" for clause in clauses), values


def _selection(connection, tables, start, end, categories):
    statements, counts = [], []
    for key, label in CATEGORIES:
        if key not in categories:
            continue
        count = 0
        for table, column in TABLES[key]:
            if table not in tables:
                continue
            clause, values = _predicate(table, column, tables, start, end)
            count += connection.execute(f'SELECT COUNT(*) FROM "{table}" r WHERE {clause}', values).fetchone()[0]
            statements.append((table, clause, values))
        counts.append({"key": key, "label": label, "count": count})
    return statements, counts


@dataclass(frozen=True)
class _Plan:
    account_key: str
    start: str | None
    end: str | None
    categories: tuple[str, ...]
    fingerprint: tuple
    all_data: bool
    total_rows: int
    ready_at: float
    expires_at: float


class DatabaseManager:
    def __init__(self):
        self._plans = {}

    def inventory(self):
        """Describe all managed accounts using read-only WAL-aware connections."""
        with _locked():
            accounts = []
            root = _root()
            for directory in sorted(root.iterdir()) if root.exists() else []:
                if not re.fullmatch(r"[0-9a-f]{64}", directory.name):
                    continue
                key = directory.name
                account = {"account_key": key, "label": f"数据库 · {key[:12]}",
                           "current": key == backend.account_scope(), "database_bytes": 0,
                           "total_rows": 0, "start": None, "end": None, "categories": []}
                try:
                    # An empty account directory left after deletion is not a cache.
                    if directory.resolve() == directory and not (directory / "mi_fitness.db").exists():
                        continue
                    path = _path(key)
                    account.update(_bytes(path))
                    with closing(_connect(path)) as connection:
                        connection.execute("BEGIN")
                        tables = _schema(connection)
                        account.update(_identity(connection, key, tables))
                        account.update(_stats(connection, tables))
                except (OSError, sqlite3.Error, backend.DesktopError):
                    account["error"] = "此数据库无法安全读取，请检查文件权限、账号归属或数据库完整性。"
                accounts.append(account)
            return {"accounts": accounts, "total_bytes": sum(item["database_bytes"] for item in accounts),
                    "categories": [{"key": key, "label": label} for key, label in CATEGORIES]}

    def preview(self, account_key, start=None, end=None, categories=None):
        """Prepare a reviewable deletion; no data is modified by this method."""
        start, end, selected = _filters(start, end, categories)
        with _locked():
            path = _path(account_key)
            try:
                before = _fingerprint(path)
                with closing(_connect(path)) as connection:
                    connection.execute("BEGIN")
                    tables = _schema(connection)
                    identity = _identity(connection, account_key, tables)
                    stats = _stats(connection, tables)
                    if (start or end) and any(item["invalid_time_rows"] for item in stats["categories"] if item["key"] in selected):
                        raise backend.DesktopError("所选类别包含无效时间，无法安全按日期筛选；可取消日期筛选后重新确认。")
                    _, counts = _selection(connection, tables, start, end, selected)
                    if _fingerprint(path) != before:
                        raise backend.DesktopError("数据库已变化，请重新预览删除范围。")
                # Closing a read-only WAL connection can remove an unused sidecar.
                fingerprint = _fingerprint(path)
                total = sum(item["count"] for item in counts)
                all_data = total == stats["total_rows"]
                wait = 3 if all_data else 0
                now = time.monotonic()
                self._plans = {token: plan for token, plan in self._plans.items() if plan.expires_at > now}
                # Bound memory when a client repeatedly requests previews.
                if len(self._plans) >= 64:
                    self._plans.pop(next(iter(self._plans)))
                token = secrets.token_urlsafe(32)
                self._plans[token] = _Plan(account_key, start, end, selected, fingerprint,
                                          all_data, total, now + wait, now + TTL_SECONDS)
                return {"token": token, "account_key": account_key, "account_label": identity["label"],
                        "start": start, "end": end, "categories": counts,
                        "total_rows": total, "all_data": all_data, "wait_seconds": wait}
            except (OSError, sqlite3.Error):
                raise backend.DesktopError("数据库预览失败，未删除任何数据；请刷新列表后重试。") from None

    def delete(self, token):
        """Consume one confirmed preview; a full-account deletion waits 3 seconds."""
        if not isinstance(token, str):
            raise ValueError("删除确认无效，请重新预览。")
        with _locked():
            plan = self._plans.get(token)
            now = time.monotonic()
            if plan is None or now >= plan.expires_at:
                self._plans.pop(token, None)
                raise backend.DesktopError("删除确认已过期，请重新预览并确认。")
            if now < plan.ready_at:
                raise backend.DesktopError("删除该账号全部数据需要等待 3 秒后再次确认。")
            self._plans.pop(token)
            path = _path(plan.account_key)
            try:
                if _fingerprint(path) != plan.fingerprint:
                    raise backend.DesktopError("数据库已变化，原删除确认失效；请重新预览。")
                with closing(_connect(path, write=True)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if _fingerprint(path) != plan.fingerprint:
                        raise backend.DesktopError("数据库已变化，原删除确认失效；请重新预览。")
                    tables = _schema(connection)
                    _identity(connection, plan.account_key, tables)
                    statements, counts = _selection(connection, tables, plan.start, plan.end, plan.categories)
                    if sum(item["count"] for item in counts) != plan.total_rows:
                        raise backend.DesktopError("删除范围已变化，请重新预览。")
                    if plan.all_data:
                        # Do not unlink a live WAL. SQLite itself checkpoints it and
                        # removes sidecars when switching back to rollback mode.
                        connection.rollback()
                        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                        if mode.lower() != "delete":
                            raise backend.DesktopError("数据库仍被其他程序占用，未删除；请关闭占用程序后重试。")
                        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
                        connection.execute("BEGIN EXCLUSIVE")
                        connection.commit()
                    else:
                        # Materialize all matching rowids before any table changes:
                        # parent/child and orphan predicates must see one snapshot.
                        connection.execute("CREATE TEMP TABLE mifit_delete_rows (table_name TEXT, record_rowid INTEGER)")
                        for table, clause, values in statements:
                            connection.execute(
                                f'INSERT INTO mifit_delete_rows SELECT ?, r.rowid FROM "{table}" r WHERE {clause}',
                                [table, *values],
                            )
                        for table, _, _ in sorted(statements, key=lambda item: item[0] not in CHILD_TABLES):
                            connection.execute(
                                f'DELETE FROM "{table}" WHERE rowid IN '
                                '(SELECT record_rowid FROM mifit_delete_rows WHERE table_name=?)', (table,),
                            )
                        if "sync_state" in tables:
                            sync_types = set(plan.categories)
                            if "heart_rate" in sync_types:
                                sync_types.add("abnormal_heart_beat")
                            if "workouts" in sync_types:
                                sync_types.update(("workout_detail", "workout_gps"))
                            placeholders = ",".join("?" for _ in sync_types)
                            connection.execute(f"DELETE FROM sync_state WHERE data_type IN ({placeholders})", sorted(sync_types))
                        connection.commit()
                        compacted = True
                        try:
                            connection.execute("VACUUM")
                            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except sqlite3.Error:
                            compacted = False
                if plan.all_data:
                    # Only this verified main file is removed. Never delete a WAL,
                    # SHM, account directory, export directory, or sibling account.
                    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                        raise backend.DesktopError("数据库仍被其他程序占用，未删除；请刷新列表后重试。")
                    if _path(plan.account_key) != path:
                        raise backend.DesktopError("数据库路径已变化，未删除。")
                    path.unlink()
                    compacted = True
                return {"status": "ok", "account_key": plan.account_key, "deleted_rows": plan.total_rows,
                        "all_data": plan.all_data, "database_removed": plan.all_data,
                        "database_bytes": 0 if plan.all_data else _bytes(path)["database_bytes"],
                        "compacted": compacted,
                        "message": "所选数据库记录已删除。已有导出文件及网页中载入的档案保留。" +
                        ("" if compacted else " 空间回收暂未完成，显示的是当前实际文件占用。")}
            except (OSError, sqlite3.Error):
                raise backend.DesktopError("数据库操作未完成，请刷新列表核实状态；未操作其他账号。") from None
