"""Local service orchestration for the separately installed Mi Fitness bridge.

Importing this module does not read credentials, create files, or access the network.
Only ``sync`` accesses Xiaomi; tests and ``smoke_demo`` use synthetic records.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sync_coverage import SyncCoverage

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EXPORTS_DIR = ROOT / "exports"
DEMO_DIR = ROOT / "demo"
SETTINGS_PATH = DATA_DIR / "settings.json"
CREDENTIAL_SERVICE = "MiFit-Desktop-Bridge"
LOCAL_TIMEZONE = "Asia/Shanghai"
_LOCAL_OFFSET = timezone(timedelta(hours=8))
REGIONS = {"cn", "de", "i2", "ru", "sg", "us"}
DATA_TYPES = {
    "daily_activity": "日常活动",
    "sleep": "睡眠",
    "workouts": "运动记录",
    "body_measurements": "身体测量",
    "heart_rate": "心率",
    "spo2": "血氧",
    "stress": "压力",
    "abnormal_heart_beat": "异常心搏",
    "workout_detail": "运动明细及轨迹",
}
_operation_lock = threading.Lock()
# Serializes whole user actions across browser tabs of the local web interface,
# including login and cache reads. Backend sync/export retain their own lock.
interface_lock = threading.Lock()
Emit = Callable[[str], None]


class DesktopError(RuntimeError):
    """A fixed, non-sensitive message that the desktop UI may display."""


def _quiet_libraries() -> None:
    # Upstream exception messages can contain server responses. Never propagate them
    # to console/file handlers, even when a caller has configured root logging.
    for name in ("mi_fitness_mcp", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False


def _vault():
    """Select Windows Credential Manager explicitly; never allow fallback storage."""
    try:
        import keyring
        from keyring.backends.Windows import WinVaultKeyring
        from mi_fitness_mcp import auth

        backend = WinVaultKeyring()
        if backend.priority <= 0:
            raise RuntimeError
        keyring.set_keyring(backend)
        auth.SERVICE_NAME = CREDENTIAL_SERVICE
        return backend
    except Exception:
        raise DesktopError("Windows 凭据管理器不可用，未使用明文存储。") from None


def _validate_credentials(user_id: str, pass_token: str) -> None:
    if not isinstance(user_id, str) or not re.fullmatch(r"[0-9]{1,20}", user_id):
        raise ValueError("用户 ID 必须为 1 至 20 位数字，不能包含空格或其他字符。")
    # RFC 6265 cookie-octet: forbid whitespace, Unicode, control bytes and cookie
    # separators rather than silently trimming potentially unsafe pasted content.
    if (
        not isinstance(pass_token, str)
        or not 1 <= len(pass_token) <= 8192
        or not re.fullmatch(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+", pass_token)
    ):
        raise ValueError("passToken 格式不正确，请仅粘贴其值，去掉引号及空格。")


def _mask(user_id: str) -> str:
    return "******" + (user_id[-2:] if len(user_id) > 2 else "")


def _account_key(user_id: str, region: str) -> str:
    return hashlib.sha256(f"MiFit-Desktop-Bridge:{region}:{user_id}".encode()).hexdigest()


def _inside(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise DesktopError("本地存储路径不在安装目录内，已停止操作。")
    return resolved


def _settings() -> dict:
    path = _inside(SETTINGS_PATH)
    if not path.exists():
        return {"region": "cn", "timezone": LOCAL_TIMEZONE}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(settings, dict)
            or settings.get("region") not in REGIONS
            or settings.get("timezone") != LOCAL_TIMEZONE
            or (
                settings.get("account_key") is not None
                and not re.fullmatch(r"[0-9a-f]{64}", settings["account_key"])
            )
        ):
            raise ValueError
        return settings
    except Exception:
        raise DesktopError("本地设置文件无效，请重新保存账号设置。") from None


def _write_settings(settings: dict) -> None:
    path = _inside(SETTINGS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".settings-{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_credentials() -> tuple[str, str, dict]:
    _quiet_libraries()
    vault = _vault()
    settings = _settings()
    try:
        user_id = vault.get_password(CREDENTIAL_SERVICE, "mi_fitness_auth_user_id")
        token = vault.get_password(CREDENTIAL_SERVICE, "mi_fitness_auth_pass_token")
        _validate_credentials(user_id, token)
        if settings.get("account_key") != _account_key(user_id, settings["region"]):
            raise ValueError
        return user_id, token, settings
    except Exception:
        raise DesktopError("尚未保存有效凭据，或账号设置不匹配，请重新保存凭据。") from None


def credentials_status() -> dict:
    """Return booleans and an account mask only; never expose stored credentials."""
    try:
        _vault()
    except DesktopError:
        return {"configured": False, "keyring_available": False, "account_masked": ""}
    try:
        user_id, _, _ = _read_credentials()
        return {
            "configured": True,
            "keyring_available": True,
            "account_masked": _mask(user_id),
        }
    except DesktopError:
        return {"configured": False, "keyring_available": True, "account_masked": ""}


def save_credentials(user_id: str, pass_token: str, region: str = "cn") -> dict:
    """Save locally through Windows Credential Manager without a network request."""
    _validate_credentials(user_id, pass_token)
    if region not in REGIONS:
        raise ValueError("不支持此账号地区。")
    if not _operation_lock.acquire(blocking=False):
        raise DesktopError("已有任务正在运行，请完成或取消后再保存凭据。")
    try:
        _quiet_libraries()
        vault = _vault()
        from keyring.errors import PasswordDeleteError

        # Invalidate the old pair before replacing it. An interrupted/failed write
        # can never combine the old account ID with a different account's token.
        with suppress(PasswordDeleteError):
            vault.delete_password(CREDENTIAL_SERVICE, "mi_fitness_auth_user_id")
        vault.set_password(CREDENTIAL_SERVICE, "mi_fitness_auth_pass_token", pass_token)
        vault.set_password(CREDENTIAL_SERVICE, "mi_fitness_auth_user_id", user_id)
        _write_settings(
            {
                "region": region,
                "timezone": LOCAL_TIMEZONE,
                "account_key": _account_key(user_id, region),
            }
        )
        return {"configured": True, "account_masked": _mask(user_id)}
    except DesktopError:
        raise
    except Exception:
        raise DesktopError("凭据未能完整保存，请重试；不会改用明文文件。") from None
    finally:
        _operation_lock.release()


def validate_dates(start: str, end: str) -> tuple[str, str]:
    try:
        if not all(
            isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            for value in (start, end)
        ):
            raise ValueError
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first > last:
            raise ValueError
        return first.isoformat(), last.isoformat()
    except (ValueError, TypeError):
        raise ValueError("请输入有效的 YYYY-MM-DD 日期，且开始日期不能晚于结束日期。") from None


def _chunks(start: str, end: str):
    cursor, last = date.fromisoformat(start), date.fromisoformat(end)
    while cursor <= last:
        chunk_end = cursor + timedelta(days=min(6, (last - cursor).days))
        yield cursor.isoformat(), chunk_end.isoformat()
        if chunk_end == last:
            break
        cursor = chunk_end + timedelta(days=1)


class _AccountDatabase:
    """Guard against an upstream reauthentication unexpectedly changing identity."""

    def __init__(self, database, user_id: str):
        self.database = database
        self.user_id = user_id

    def __getattr__(self, name):
        method = getattr(self.database, name)
        if not name.startswith("insert_"):
            return method

        def checked(records, *args, **kwargs):
            items = records if isinstance(records, (list, tuple)) else [records]
            if any(getattr(item, "user_id", None) != self.user_id for item in items):
                raise DesktopError("服务器返回的账号不匹配，已阻止写入。")
            return method(records, *args, **kwargs)

        return checked


def _new_adapter(user_id: str, token: str, region: str):
    from mi_fitness_mcp.adapters.mi_fitness_cloud import MiFitnessCloudAdapter

    return MiFitnessCloudAdapter(user_id=user_id, pass_token=token, region=region)


def _type_result() -> dict:
    return {
        "status": "pending", "added": 0, "updated": 0, "skipped": 0,
        "failed_chunks": 0, "completed_chunks": 0, "skipped_days": 0,
    }


def _summarize(types: dict) -> dict:
    all_ok = all(item["status"] == "ok" for item in types.values())
    useful = any(
        item["completed_chunks"] or item["added"] or item["updated"] or item["skipped_days"]
        for item in types.values()
    )
    status = "ok" if all_ok else ("partial" if useful else "error")
    return {
        "status": status,
        "all_ok": all_ok,
        "partial": status == "partial",
        **{key: sum(item[key] for item in types.values()) for key in ("added", "updated", "skipped", "skipped_days")},
        "types": types,
    }


def account_scope() -> str | None:
    """Opaque local account key, including region; never a credential."""
    try:
        return _settings().get("account_key")
    except DesktopError:
        return None


def _account_directory(root: Path, key: str) -> Path:
    intended = root.resolve() / key
    resolved = _inside(intended)
    if resolved != intended:
        raise DesktopError("账号目录不能重定向到其他位置。")
    return resolved


def _managed_child(base: Path, name: str) -> Path:
    intended = base / name
    resolved = _inside(intended)
    if resolved != intended:
        raise DesktopError("数据路径不能重定向到其他位置。")
    return resolved


def _replace_database(source_path: Path, target_path: Path) -> None:
    """SQLite backup publishes all staged pages in one destination transaction."""
    with (
        closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(target_path)) as target,
    ):
        source.backup(target)


def _enable_cache_wal(path: Path) -> None:
    """Keep committed cache pages readable after an interrupted writer.

    WAL mode persists for later upstream connections, whose SQLite default is
    synchronous=FULL. SQLite owns recovery/checkpointing; never remove its logs.
    """
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise DesktopError("无法启用数据库写入保护，请关闭其他占用数据库的程序后重试。")


async def sync(start: str, end: str, emit: Emit, *, mode="merge", expected_account=None) -> dict:
    """Skip completed days by default; upsert missing or still-open dates.

    Replacement is bound to the account confirmed by the caller. Failures and
    cancellation discard staging and leave that account's existing cache intact.
    """
    start, end = validate_dates(start, end)
    if mode not in {"merge", "replace"}:
        raise ValueError("不支持的更新方式。")
    if not _operation_lock.acquire(blocking=False):
        raise DesktopError("已有任务正在运行，请稍后再试。")
    staging = None
    try:
        credentials = _read_credentials()
        key = credentials[2]["account_key"]
        if mode == "replace" and (not expected_account or expected_account != key):
            raise DesktopError("账号已变化或尚未确认，未覆盖任何历史数据。")
        target = _managed_child(_account_directory(DATA_DIR, key), "mi_fitness.db")
        if mode == "replace":
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = _inside(target.parent / f".replace-{uuid4().hex}.db")
            emit("覆盖模式：先获取所选日期，全部成功后才替换当前账号历史。")
        result = await _sync_account(start, end, emit, credentials, staging or target,
                                     protect_writes=staging is None)
        result["mode"] = mode
        result["committed"] = mode == "merge"
        if staging:
            if result["all_ok"]:
                _replace_database(staging, target)
                result["committed"] = True
                emit("当前账号历史已替换为本次所选范围；其他账号不变。")
            else:
                emit("覆盖同步未全部成功，原有历史保持不变；本次暂存结果未发布。")
        return result
    except asyncio.CancelledError:
        if staging:
            emit("覆盖已取消：暂存结果未发布，原有历史保持不变。")
        raise
    except DesktopError:
        if staging:
            emit("覆盖未完成：暂存结果未发布，原有历史保持不变。")
        raise
    except Exception:
        raise DesktopError("同步未完成；覆盖模式下原有历史未被替换。") from None
    finally:
        try:
            if staging:
                # Cleanup must not turn an already committed replacement into a
                # reported failure (for example if antivirus holds the file).
                with suppress(OSError):
                    staging.unlink(missing_ok=True)
        finally:
            _operation_lock.release()


def _workout_details_pending(db_path, user_id, start, end):
    """Use the upstream per-workout cache rule before opening a cloud session."""
    if not db_path.exists():
        return False, 0
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT w.fds_sid, w.report_time, "
            "EXISTS(SELECT 1 FROM workout_detail_samples s WHERE s.user_id=w.user_id AND s.workout_id=w.workout_id), "
            "EXISTS(SELECT 1 FROM workout_gps_points g WHERE g.user_id=w.user_id AND g.workout_id=w.workout_id) "
            "FROM workouts w WHERE w.user_id=? AND substr(w.start_at,1,10) BETWEEN ? AND ?",
            (user_id, start, end),
        ).fetchall()
    return any(sid and report and not (samples or gps) for sid, report, samples, gps in rows), len(rows)


async def _sync_account(start, end, emit, credentials, db_path, *, protect_writes=True) -> dict:
    """Fetch into one account database; caller owns the operation lock."""
    adapter = None
    try:
        from mi_fitness_mcp.services.sync_service import SyncService
        from mi_fitness_mcp.storage import Database

        user_id, token, settings = credentials
        types = {name: _type_result() for name in DATA_TYPES}
        coverage = SyncCoverage(db_path, datetime.now(_LOCAL_OFFSET).date())
        service = None
        emit("正在检查本地已完整同步的日期。")
        for name, label in DATA_TYPES.items():
            item = types[name]
            if name == "workout_detail":
                pending, cached = _workout_details_pending(db_path, user_id, start, end)
                ranges = list(_chunks(start, end)) if pending else []
                if not pending:
                    item["skipped"] = cached
            else:
                ranges, item["skipped_days"] = coverage.pending(name, start, end)
                if item["skipped_days"]:
                    emit(f"{label}：已完整缓存 {item['skipped_days']} 天，直接跳过。")
            for number, (chunk_start, chunk_end) in enumerate(ranges, 1):
                if service is None:
                    adapter = _new_adapter(user_id, token, settings["region"])
                    emit("正在连接小米云端。")
                    try:
                        connected = await asyncio.wait_for(adapter.connect(), timeout=90)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        connected = False
                    if not connected or adapter.get_user_id() != user_id:
                        for remaining in types.values():
                            if remaining["status"] == "pending":
                                remaining["status"] = "error"
                        emit("连接失败或账号不匹配，请检查凭据、地区及网络后重试。")
                        return _summarize(types)
                    database = _AccountDatabase(Database(db_path), user_id)
                    # Replacement writes only a disposable staging database;
                    # keep it self-contained until the atomic backup publishes it.
                    if protect_writes:
                        _enable_cache_wal(db_path)
                    service = SyncService(adapter, database, chunk_days=7)
                emit(f"正在同步{label}：第 {number}/{len(ranges)} 块。")
                try:
                    result = await asyncio.wait_for(
                        service.sync_data_type(
                            name, start_date=chunk_start, end_date=chunk_end,
                            force_full=name != "workout_detail",
                        ),
                        timeout=180,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result = {"status": "error"}
                for key in ("added", "updated", "skipped"):
                    item[key] += max(0, int(result.get(key, 0)))
                bad_records = bool(result.get("bad_records")) or any(
                    bool(chunk.get("bad_records")) for chunk in result.get("chunks", [])
                )
                failed = result.get("status") != "ok" or bad_records
                item["failed_chunks" if failed else "completed_chunks"] += 1
                if adapter.get_user_id() != user_id:
                    item["status"] = "partial" if item["completed_chunks"] else "error"
                    emit("同步期间账号发生变化，已停止；请重新保存凭据。")
                    return _summarize(types)
                if failed:
                    emit(f"{label}有一块未完整同步；已保留此前写入的数据。")
                elif name != "workout_detail":
                    coverage.complete(name, chunk_start, chunk_end)
                await asyncio.sleep(0)
            item["status"] = (
                "ok" if not item["failed_chunks"]
                else "partial" if item["completed_chunks"] or item["added"] or item["updated"] or item["skipped_days"]
                else "error"
            )
            emit(
                f"{label}：新增 {item['added']}，更新 {item['updated']}，"
                f"跳过 {item['skipped']}，失败块 {item['failed_chunks']}。"
            )
        summary = _summarize(types)
        emit("同步完成。" if summary["all_ok"] else "本次同步未全部成功，请查看各类型状态。")
        return summary
    except asyncio.CancelledError:
        emit("同步已取消；已保存的数据保留在本机。")
        raise
    except DesktopError:
        raise
    except Exception:
        raise DesktopError("同步发生异常，已保存的数据仍保留在本机。") from None
    finally:
        if adapter is not None:
            try:
                await asyncio.wait_for(adapter.close(), timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception:
                emit("云端连接清理未完成；请关闭程序后重新打开。")


def _unique_directory(base: Path) -> Path:
    stamp = datetime.now(_LOCAL_OFFSET).strftime("%Y%m%d_%H%M%S_%f")
    directory = _inside(base / f"{stamp}_{uuid4().hex[:8]}")
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _export_local_date(value: str) -> str:
    """Use Shanghai calendar dates; legacy naive timestamps already mean local time.

    Offset-bearing timestamps are converted solely for filtering. The stored/exported
    timestamp value is preserved verbatim. Invalid values fail the whole export.
    """
    if not isinstance(value, str):
        raise ValueError("本地记录的时间格式无效。")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_LOCAL_OFFSET)
        return parsed.astimezone(_LOCAL_OFFSET).date().isoformat()
    except (ValueError, OverflowError):
        raise ValueError("本地记录的时间格式无效。") from None


def _export_database(db_path: Path, directory: Path, start: str | None, end: str | None, emit: Emit) -> dict:
    from mi_fitness_mcp.export import DATASETS, export_database

    snapshot = directory / ".export-snapshot.db"
    invalid_date = False

    def local_date(value):
        nonlocal invalid_date
        try:
            return _export_local_date(value)
        except ValueError:
            invalid_date = True
            raise

    try:
        # Both formats read the exact same SQLite snapshot. The temporary copy is
        # removed, so date-filtered exports do not include an unfiltered database.
        with (
            closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as target,
        ):
            source.backup(target)
            target.create_function("desktop_local_date", 1, local_date)
            try:
                with target:
                    for table, date_column in DATASETS.values():
                        table_sql = '"' + table.replace('"', '""') + '"'
                        column_sql = '"' + date_column.replace('"', '""') + '"'
                        target.execute(
                            f"DELETE FROM {table_sql} "
                            f"WHERE desktop_local_date({column_sql}) < ? "
                            f"OR desktop_local_date({column_sql}) > ?",
                            (start or "0001-01-01", end or "9999-12-31"),
                        )
            except sqlite3.Error:
                if invalid_date:
                    raise DesktopError(
                        "本地记录包含无效时间，已停止导出；原始缓存未改动。"
                    ) from None
                raise
        json_files = export_database(
            snapshot, directory / "mi_fitness.json", output_format="json",
        )
        csv_files = export_database(
            snapshot, directory / "csv", output_format="csv",
        )
        # Preserve the upstream envelope and original timestamp strings while
        # recording the local-calendar filter already applied to the snapshot.
        json_path = directory / "mi_fitness.json"
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["filters"].update(
            {"start_date": start, "end_date": end, "timezone": LOCAL_TIMEZONE,
             "scope": "all_cached" if start is None else "date_range"}
        )
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total_rows = sum(json_files.row_counts.values())
        emit(
            f"导出完成，共 {total_rows} 条记录，已生成 CSV 和 JSON。"
            if total_rows else
            ("当前账号没有本地记录；已生成空 CSV 和 JSON，请先同步。" if start is None else
             "指定日期范围内没有本地记录；已生成空 CSV 和 JSON，请先同步或调整日期。")
        )
        return {
            "directory": str(directory),
            "total_rows": total_rows,
            "empty": total_rows == 0,
            "row_counts": dict(json_files.row_counts),
            "files": [str(path) for path in [*json_files, *csv_files]],
        }
    finally:
        snapshot.unlink(missing_ok=True)


def _export_cumulative(db_path: Path, base: Path, emit: Emit) -> dict:
    """Publish a complete JSON/CSV generation with rollback and crash recovery.

    Only the owned current/staging/previous directories are moved or removed.
    Legacy timestamp exports are preserved. A crash between the two renames is
    recovered on the next export before building a new generation.
    """
    base.mkdir(parents=True, exist_ok=True)
    current = _managed_child(base, "current")
    previous = _managed_child(base, ".current-previous")
    if previous.exists():
        if current.exists():
            shutil.rmtree(previous)
        else:
            previous.replace(current)
    staging = _managed_child(base, f".export-{uuid4().hex}")
    staging.mkdir()
    try:
        messages = []
        result = _export_database(db_path, staging, None, None, messages.append)
        if current.exists():
            current.replace(previous)
        try:
            staging.replace(current)
        except Exception:
            if previous.exists() and not current.exists():
                previous.replace(current)
            raise
        result["files"] = [str(current / Path(path).relative_to(staging)) for path in result["files"]]
        result["directory"] = str(current)
        result["mode"] = "cumulative"
        if previous.exists():
            # A locked old file can wait for next-run cleanup; the new generation
            # has already been published successfully.
            with suppress(OSError):
                shutil.rmtree(previous)
        for message in messages:
            emit(message)
        emit("已更新当前账号的累计档案，包含全部已缓存历史。")
        return result
    finally:
        if staging.exists():
            shutil.rmtree(_managed_child(base, staging.name))


def export(start: str, end: str, emit: Emit, *, mode="cumulative") -> dict:
    """Refresh one account's cumulative archive; snapshots are explicit opt-in."""
    start, end = validate_dates(start, end)
    if mode not in {"cumulative", "snapshot"}:
        raise ValueError("不支持的导出方式。")
    if not _operation_lock.acquire(blocking=False):
        raise DesktopError("已有任务正在运行，请稍后再试。")
    try:
        settings = _settings()
        account_key = settings.get("account_key")
        if not account_key:
            raise DesktopError("尚未配置账号，请先保存凭据并同步。")
        db_path = _managed_child(_account_directory(DATA_DIR, account_key), "mi_fitness.db")
        if not db_path.is_file():
            raise DesktopError("当前账号尚无本地数据，请先执行同步。")
        base = _account_directory(EXPORTS_DIR, account_key)
        if mode == "snapshot":
            directory = _unique_directory(base)
            return _export_database(db_path, directory, start, end, emit)
        return _export_cumulative(db_path, base, emit)
    except DesktopError:
        raise
    except Exception:
        raise DesktopError("导出失败，请检查磁盘空间及目录权限；原始缓存未改动。") from None
    finally:
        _operation_lock.release()


def smoke_demo() -> dict:
    """Create synthetic data and real exports in demo/; no credentials or network."""
    from mi_fitness_mcp.models import DailyActivity, HeartRateSample
    from mi_fitness_mcp.storage import Database

    directory = _unique_directory(DEMO_DIR)
    db_path = directory / "synthetic.db"
    database = Database(db_path)
    base = {
        "provider": "mi_fitness", "source_type": "cloud_session",
        "user_id": "synthetic-demo-only", "timezone": LOCAL_TIMEZONE,
    }
    for day in (14, 15, 16):
        database.insert_daily_activity(DailyActivity(
            id=f"demo-day-{day}", date=f"2026-07-{day}", steps=day * 100,
            distance_m=day * 75, active_kcal=day * 10, **base,
        ))
    for index, timestamp in enumerate((
        datetime(2026, 7, 14, 23, 59, 59),
        datetime(2026, 7, 15, 0, 0, 0),
        datetime(2026, 7, 15, 23, 59, 59),
        datetime(2026, 7, 16, 0, 0, 0),
    )):
        database.insert_heart_rate_sample(HeartRateSample(
            id=f"demo-heart-{index}", timestamp=timestamp, bpm=70 + index, **base,
        ))
    export_dir = directory / "exports"
    export_dir.mkdir()
    return _export_database(db_path, export_dir, "2026-07-15", "2026-07-15", lambda _: None)
