"""Streaming, local-only aggregation for the portable Mi Fitness dashboard.

This module uses only the standard library. Importing it performs no I/O. Health
records are read only by ``build_summary``; ``demo_summary`` uses an in-memory
synthetic database and never opens the user's settings, database, or credentials.
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from record_archive import read_archive

ROOT = Path(__file__).resolve().parent
TIMEZONE = "Asia/Shanghai"
_BEIJING = timezone(timedelta(hours=8))
_STAGES = ("deep", "light", "rem", "awake")
_BODY_FIELDS = ("weight_kg","bmi","body_fat_pct","muscle_mass_kg","water_pct","bone_mass_kg","visceral_fat_score","basal_metabolism_kcal","metabolic_age")
_TABLES = {
    "daily_activity": "daily_activity",
    "sleep": "sleep_sessions",
    "workouts": "workouts",
    "body_measurements": "body_measurements",
    "heart_rate": "heart_rate_samples",
    "spo2": "spo2_samples",
    "stress": "stress_samples",
    "abnormal_heart_beat": "abnormal_heart_beat_events",
    "workout_detail": "workout_detail_samples",
    "workout_gps": "workout_gps_points",
}
_NOTES = [
    "时间戳按北京时间归日；没有时区的历史时间按北京时间解释。日常活动保留上游日历日期。",
    "同一天的活动选步数最大的一条，步数相同时优先采用较新的采集记录；距离与热量取自同一条，不累加设备。",
    "主睡眠按醒来日期归日，每天只取时长最长的非小睡记录；睡眠分钟使用该记录的实际睡眠时间，阶段不跨记录拼接。",
    "小睡按醒来日期归日；重叠时段每组只取最长记录的实际睡眠时间，再合计互不重叠的组。",
    "心率与血氧忽略非正数；压力的零值有效。日均值按有效样本计算，静息心率只使用标记为 resting 的样本。",
    "身体成分日值取当天最新一次测量，缺失不跨记录补齐；导出明细保留每次测量。",
    "运动按缓存中的每条有效日期记录计数；分钟只合计已提供的时长，不推断合并跨设备运动，也不包含秒级明细和轨迹。",
    "导出明细保留已知健康字段、原始采样及 GPS 坐标；账号、设备、服务定位标识和未知字段不保留。",
]


class DashboardDataError(RuntimeError):
    """Fixed, non-sensitive error text suitable for showing in a local UI."""


def resolve_current_database() -> Path:
    """Resolve the selected local account without importing or accessing keyring."""
    root = ROOT.resolve()
    try:
        settings_path = (root / "data" / "settings.json").resolve()
        if not settings_path.is_relative_to(root) or settings_path.stat().st_size > 65536:
            raise ValueError
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        account_key = settings.get("account_key")
        if not isinstance(account_key, str) or not re.fullmatch(r"[0-9a-f]{64}", account_key):
            raise ValueError
        database = (root / "data" / account_key / "mi_fitness.db").resolve()
        if not database.is_relative_to(root) or not database.is_file():
            raise ValueError
        return database
    except (OSError, ValueError, TypeError, AttributeError):
        raise DashboardDataError("当前账号尚无可用的本地缓存，请先在桌面工具中保存账号并同步。") from None


def _number(value, *, positive=False, maximum=None, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        return None
    if maximum is not None and number > maximum:
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def _local_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_BEIJING)
        return parsed.astimezone(_BEIJING)
    except (ValueError, OverflowError):
        return None


def _calendar_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed.isoformat() if parsed.isoformat() == value else None
    except ValueError:
        return None


def _blank_day(day):
    return {
        "date": day,
        **dict.fromkeys((
            "steps", "distance_km", "active_kcal", "active_minutes",
            "sleep_minutes", "sleep_score", "nap_minutes",
            "heart_rate_avg", "heart_rate_min", "heart_rate_max", "resting_hr",
            "spo2_avg", "stress_avg", "weight_kg", "body_fat_pct",
            "workout_minutes", "bmi", "muscle_mass_kg", "water_pct",
            "bone_mass_kg", "visceral_fat_score", "basal_metabolism_kcal", "metabolic_age",
        )),
        "sleep_stages": dict.fromkeys(_STAGES),
        "workout_count": 0,
    }


class _Mean:
    __slots__ = ("count", "maximum", "mean", "minimum")

    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.minimum = None
        self.maximum = None

    def add(self, value):
        if value is None:
            return
        self.count += 1
        # Running mean avoids retaining individual samples or a growing float sum.
        self.mean += (value - self.mean) / self.count
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def result(self):
        return round(self.mean, 2) if self.count else None


def _sleep_stages(raw):
    result = dict.fromkeys(_STAGES)
    if raw is None:
        return result, False
    try:
        stages = json.loads(raw)
    except (TypeError, ValueError):
        return result, True
    if not isinstance(stages, list):
        return result, True
    invalid = False
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("stage") not in _STAGES:
            invalid = True
            continue
        minutes = _number(stage.get("minutes"))
        if minutes is None:
            invalid = True
            continue
        name = stage["stage"]
        result[name] = (result[name] or 0) + minutes
    return result, invalid


def _merge_nap(groups, start, end, rank, asleep):
    """Keep only disjoint interval groups, not all raw duplicate nap records."""
    remaining = []
    for old_start, old_end, old_rank, old_asleep in groups:
        if start < old_end and old_start < end:
            start, end = min(start, old_start), max(end, old_end)
            if old_rank > rank:
                rank, asleep = old_rank, old_asleep
        else:
            remaining.append((old_start, old_end, old_rank, old_asleep))
    remaining.append((start, end, rank, asleep))
    # Intervals are maintained in chronological order, so a later bridging record
    # merges every overlapping group in the same pass.
    remaining.sort(key=lambda group: group[0])
    return remaining


def _schema(connection):
    names = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {
        table: {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        for table in _TABLES.values() if table in names
    }


def _rows(connection, schema, table, columns):
    if table not in schema:
        return iter(())
    # Only summary columns are selected here. read_archive separately reads known
    # health fields for the detail viewer; account/device IDs are never selected.
    selected = ", ".join(
        f'"{name}"' if name in schema[table] else f'NULL AS "{name}"'
        for name in columns
    )
    return connection.execute(f'SELECT {selected} FROM "{table}"')


def _summarize(connection, generated_at=None):
    schema = _schema(connection)
    counts = dict.fromkeys(_TABLES, 0)
    days = {}
    bad_dates = 0
    bad_stages = 0

    def day_record(key):
        if key not in days:
            days[key] = _blank_day(key)
        return days[key]

    activity_ranks = {}
    for row in _rows(connection, schema, "daily_activity", (
        "date", "steps", "distance_m", "active_kcal", "active_minutes", "collected_at",
    )):
        counts["daily_activity"] += 1
        key = _calendar_date(row["date"])
        if key is None:
            bad_dates += 1
            continue
        steps = _number(row["steps"], integer=True)
        collected = _local_time(row["collected_at"])
        rank = (steps if steps is not None else -1, collected or datetime.min.replace(tzinfo=_BEIJING))
        if key in activity_ranks and rank <= activity_ranks[key]:
            continue
        activity_ranks[key] = rank
        distance = _number(row["distance_m"])
        day_record(key).update(
            steps=steps,
            distance_km=distance / 1000 if distance is not None else None,
            active_kcal=_number(row["active_kcal"]),
            active_minutes=_number(row["active_minutes"]),
        )

    sleep_ranks = {}
    naps = {}
    for row in _rows(connection, schema, "sleep_sessions", (
        "start_at", "end_at", "duration_minutes", "time_asleep_minutes",
        "sleep_score", "is_nap", "stages",
    )):
        counts["sleep"] += 1
        start, end = _local_time(row["start_at"]), _local_time(row["end_at"])
        if start is None or end is None or end < start:
            bad_dates += 1
            continue
        key = end.date().isoformat()
        day = day_record(key)
        duration = _number(row["duration_minutes"])
        asleep = _number(row["time_asleep_minutes"])
        rank = (
            duration if duration is not None else (end - start).total_seconds() / 60,
            asleep if asleep is not None else -1,
            end,
        )
        if row["is_nap"] in (1, True, "1"):
            naps[key] = _merge_nap(naps.get(key, []), start, end, rank, asleep)
            continue
        if key in sleep_ranks and rank <= sleep_ranks[key]:
            continue
        sleep_ranks[key] = rank
        stages, invalid = _sleep_stages(row["stages"])
        bad_stages += int(invalid)
        day.update(
            sleep_minutes=asleep,
            sleep_score=_number(row["sleep_score"], maximum=100),
            sleep_stages=stages,
        )
    for key, groups in naps.items():
        values = [group[3] for group in groups if group[3] is not None]
        days[key]["nap_minutes"] = sum(values) if values else None

    means = {}
    for source, table, column, output, positive, maximum in (
        ("heart_rate", "heart_rate_samples", "bpm", "heart_rate_avg", True, None),
        ("spo2", "spo2_samples", "spo2_pct", "spo2_avg", True, 100),
        ("stress", "stress_samples", "stress_score", "stress_avg", False, 100),
    ):
        columns = ("timestamp", column, "sample_type") if source == "heart_rate" else (
            "timestamp", column,
        )
        for row in _rows(connection, schema, table, columns):
            counts[source] += 1
            timestamp = _local_time(row["timestamp"])
            if timestamp is None:
                bad_dates += 1
                continue
            key = timestamp.date().isoformat()
            day_record(key)
            value = _number(row[column], positive=positive, maximum=maximum)
            means.setdefault((key, output), _Mean()).add(value)
            if source == "heart_rate" and row["sample_type"] == "resting":
                means.setdefault((key, "resting_hr"), _Mean()).add(value)
    for (key, output), statistic in means.items():
        days[key][output] = statistic.result()
        if output == "heart_rate_avg":
            days[key]["heart_rate_min"] = statistic.minimum
            days[key]["heart_rate_max"] = statistic.maximum

    body_times = {}
    for row in _rows(connection, schema, "body_measurements", (
        "timestamp", *_BODY_FIELDS,
    )):
        counts["body_measurements"] += 1
        timestamp = _local_time(row["timestamp"])
        if timestamp is None:
            bad_dates += 1
            continue
        key = timestamp.date().isoformat()
        if key in body_times and timestamp <= body_times[key]:
            continue
        body_times[key] = timestamp
        day_record(key).update({
            field: _number(
                row[field], positive=field in ("weight_kg", "bmi"),
                maximum=100 if field in ("body_fat_pct", "water_pct") else None,
            )
            for field in _BODY_FIELDS
        })

    workouts = []
    for row in _rows(connection, schema, "workouts", (
        "start_at", "activity_type", "duration_minutes", "distance_m",
        "calories_kcal", "avg_heart_rate_bpm", "avg_pace_sec_per_km",
    )):
        counts["workouts"] += 1
        timestamp = _local_time(row["start_at"])
        if timestamp is None:
            bad_dates += 1
            continue
        key = timestamp.date().isoformat()
        duration = _number(row["duration_minutes"])
        distance = _number(row["distance_m"])
        activity_type = row["activity_type"]
        if not isinstance(activity_type, str) or not activity_type.strip():
            activity_type = "other"
        workout = {
            "date": key, "start": timestamp.strftime("%H:%M"), "type": activity_type,
            "duration_minutes": duration,
            "distance_km": distance / 1000 if distance is not None else None,
            "calories_kcal": _number(row["calories_kcal"]),
            "avg_heart_rate_bpm": _number(row["avg_heart_rate_bpm"], positive=True),
            "avg_pace_sec_per_km": _number(row["avg_pace_sec_per_km"], positive=True),
        }
        workouts.append(workout)
        day = day_record(key)
        day["workout_count"] += 1
        if duration is not None:
            day["workout_minutes"] = (day["workout_minutes"] or 0) + duration

    # Count additional datasets; their known health columns are archived below.
    for source in ("abnormal_heart_beat", "workout_detail", "workout_gps"):
        table = _TABLES[source]
        if table in schema:
            counts[source] = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    notes = list(_NOTES)
    if bad_dates:
        notes.append(f"有 {bad_dates} 条记录的日期或时间无效，未纳入日历聚合；原始缓存未改动。")
    if bad_stages:
        notes.append(f"有 {bad_stages} 条主睡眠阶段信息不完整，仅保留可识别的有效阶段。")
    records = read_archive(connection, _TABLES, schema)
    # Include dates that only contain GPS, abnormal events or workout samples so
    # the detail date range can reach them even without a daily summary.
    for source, dataset in records["datasets"].items():
        date_key = "date" if source == "daily_activity" else (
            "end_at" if source == "sleep" else
            "start_at" if source in ("workouts", "abnormal_heart_beat") else "timestamp"
        )
        if date_key not in dataset["fields"]:
            continue
        index = dataset["fields"].index(date_key)
        for row in dataset["rows"]:
            if source == "daily_activity":
                key = _calendar_date(row[index])
            else:
                stamp = _local_time(row[index])
                key = stamp.date().isoformat() if stamp else None
            if key:
                day_record(key)
    ordered_days = [days[key] for key in sorted(days)]
    return {
        "schema_version": 1,
        "meta": {
            "generated_at": generated_at or datetime.now(_BEIJING).isoformat(timespec="seconds"),
            "timezone": TIMEZONE,
            "range": {
                "start": ordered_days[0]["date"] if ordered_days else None,
                "end": ordered_days[-1]["date"] if ordered_days else None,
            },
            "source_counts": counts,
            "notes": notes,
            "omitted": ["account_identifiers", "device_identifiers", "service_identifiers", "unknown_fields"],
        },
        "records": records,
        "days": ordered_days,
        "workouts": sorted(workouts, key=lambda item: (item["date"], item["start"], item["type"])),
    }


def build_summary(db_path: Path) -> dict:
    """Read a consistent, read-only SQLite snapshot with a bounded record archive."""
    try:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise DashboardDataError("找不到本地缓存，请先在桌面工具中同步数据。")
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            try:
                return _summarize(connection)
            finally:
                connection.rollback()
    except DashboardDataError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        raise DashboardDataError("无法读取本地缓存摘要，请检查缓存文件及权限；原始数据未改动。") from None


_DEMO_SCHEMA = """
CREATE TABLE daily_activity (
    date TEXT, steps INTEGER, distance_m REAL, active_kcal REAL,
    active_minutes INTEGER, collected_at TEXT
);
CREATE TABLE sleep_sessions (
    start_at TEXT, end_at TEXT, duration_minutes REAL, time_asleep_minutes REAL,
    sleep_score REAL, is_nap INTEGER, stages TEXT
);
CREATE TABLE heart_rate_samples (timestamp TEXT, bpm REAL, sample_type TEXT);
CREATE TABLE spo2_samples (timestamp TEXT, spo2_pct REAL);
CREATE TABLE stress_samples (timestamp TEXT, stress_score REAL);
CREATE TABLE body_measurements (timestamp TEXT, weight_kg REAL, body_fat_pct REAL);
CREATE TABLE workouts (
    start_at TEXT, activity_type TEXT, duration_minutes REAL, distance_m REAL,
    calories_kcal REAL, avg_heart_rate_bpm REAL, avg_pace_sec_per_km REAL
);
"""


def demo_summary() -> dict:
    """Generate a deterministic 365-day synthetic example entirely in memory."""
    rng = random.Random(20261021)
    first = date(2025, 10, 21)
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(_DEMO_SCHEMA)
        for field in _BODY_FIELDS:
            if field not in ("weight_kg", "body_fat_pct"):
                connection.execute(f'ALTER TABLE body_measurements ADD COLUMN "{field}" REAL')
        for index in range(365):
            if index not in (0, 364) and index % 47 in (19, 20, 21):
                continue
            day = first + timedelta(days=index)
            midnight = datetime.combine(day, datetime.min.time(), tzinfo=_BEIJING)
            weekend = day.weekday() >= 5
            wave = math.sin(index * math.pi / 45)
            steps = max(0, round(7200 + wave * 1600 + rng.gauss(0, 1800) + weekend * 1800))
            if index % 71 == 30:
                steps = 0
            if index % 13 != 5:
                values = (
                    day.isoformat(), steps, round(steps * 0.73, 1),
                    round(steps * 0.042, 1), round(steps / 130),
                    (midnight + timedelta(hours=23, minutes=55)).isoformat(),
                )
                connection.execute("INSERT INTO daily_activity VALUES (?,?,?,?,?,?)", values)
                if index % 23 == 4:
                    connection.execute(
                        "INSERT INTO daily_activity VALUES (?,?,?,?,?,?)",
                        (values[0], round(steps * 0.8), values[2] * 0.8, values[3] * 0.8,
                         round(values[4] * 0.8), (midnight + timedelta(hours=21)).isoformat()),
                    )
            if index % 17 != 3:
                asleep = max(270, min(540, round(435 + rng.gauss(0, 38) + weekend * 22)))
                awake = rng.randint(12, 35)
                end = midnight + timedelta(hours=7, minutes=rng.randint(-35, 45) + weekend * 40)
                start = end - timedelta(minutes=asleep + awake)
                deep, rem = round(asleep * rng.uniform(0.16, 0.24)), round(asleep * 0.22)
                stages = [
                    {"stage": "deep", "minutes": deep},
                    {"stage": "light", "minutes": asleep - deep - rem},
                    {"stage": "rem", "minutes": rem},
                    {"stage": "awake", "minutes": awake},
                ] if index % 19 != 6 else []
                connection.execute(
                    "INSERT INTO sleep_sessions VALUES (?,?,?,?,?,?,?)",
                    (start.isoformat(), end.isoformat(), asleep + awake, asleep,
                     max(45, min(97, round(84 + (asleep - 435) / 8 + rng.gauss(0, 4)))),
                     0, json.dumps(stages)),
                )
                if index % 11 == 0:
                    nap_start = midnight + timedelta(hours=13, minutes=10)
                    for delay, minutes in ((0, 28), (3, 23)):
                        connection.execute(
                            "INSERT INTO sleep_sessions VALUES (?,?,?,?,?,?,?)",
                            ((nap_start + timedelta(minutes=delay)).isoformat(),
                             (nap_start + timedelta(minutes=delay + minutes + 3)).isoformat(),
                             minutes + 3, minutes, None, 1, "[]"),
                        )
            if index % 29 != 9:
                resting = 62 - index / 365 * 3 + rng.gauss(0, 2)
                for hour in range(0, 24, 2):
                    timestamp = midnight + timedelta(hours=hour, minutes=10)
                    bpm = resting + (rng.uniform(0, 6) if hour < 6 else rng.uniform(4, 28))
                    connection.execute(
                        "INSERT INTO heart_rate_samples VALUES (?,?,?)",
                        (timestamp.isoformat(), round(bpm), "resting" if hour < 6 else "passive"),
                    )
                    if index % 7 != 2 and hour % 4 == 0:
                        connection.execute(
                            "INSERT INTO spo2_samples VALUES (?,?)",
                            (timestamp.isoformat(), max(94, min(100, round(rng.gauss(97.8, 0.8), 1)))),
                        )
                    if index % 9 != 4:
                        connection.execute(
                            "INSERT INTO stress_samples VALUES (?,?)",
                            (timestamp.isoformat(), max(0, min(100, round(33 + wave * 7 + rng.gauss(0, 9))))),
                        )
            if index % 3 == 0:
                connection.execute(
                    "INSERT INTO body_measurements VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ((midnight + timedelta(hours=7, minutes=30)).isoformat(),
                     round(78 - index / 365 * 4 + rng.gauss(0, 0.35), 1),
                     round(22 - index / 365 * 2.3 + rng.gauss(0, 0.3), 1),
                     round(24 - index / 365, 1), 52.4, 56.2, 2.8, 8, 1650, 30),
                )
            if day.weekday() in (1, 3, 5) and index % 31 != 10:
                kind = rng.choice(("running", "walking", "cycling", "strength", "swimming", "yoga"))
                duration = rng.randint(25, 65)
                km_per_minute = {"running": 0.16, "walking": 0.075, "cycling": 0.32, "swimming": 0.035}
                distance = duration * km_per_minute[kind] if kind in km_per_minute else None
                pace = duration * 60 / distance if distance else None
                connection.execute(
                    "INSERT INTO workouts VALUES (?,?,?,?,?,?,?)",
                    ((midnight + timedelta(hours=18, minutes=rng.randint(0, 45))).isoformat(),
                     kind, duration, round(distance * 1000) if distance else None,
                     round(duration * rng.uniform(4.5, 9)), rng.randint(105, 153),
                     round(pace, 1) if pace else None),
                )
        connection.commit()
        summary = _summarize(connection, generated_at="2026-10-21T00:00:00+08:00")
    summary["meta"]["notes"].insert(0, "这是固定种子生成的完全合成演示数据，不代表任何人的健康记录。")
    return summary
