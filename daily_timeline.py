"""Local calendar-day counts for the existing Personal World timeline."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime
import json
from math import isfinite
from pathlib import Path
import re
import sqlite3
import subprocess

from app_config import AppError, read_private_json
from world_model import date_bounds


CONFIG_NAME = "daily-timeline-sources.json"
SOURCES = ("wechat", "magpie", "screen_memory", "dsh_recorder")
SCREEN_DB = "screen_db"
RECORDER_INDEX = "recorder_index"


def source_config(state):
    path = state / CONFIG_NAME
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    config = read_private_json(path)
    if config.keys() - {"magpie", "magpie_limit", SCREEN_DB, RECORDER_INDEX}:
        raise ValueError("Unexpected daily source setting")
    limit = config.get("magpie_limit", 200)
    if type(config.get("magpie", False)) is not bool or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid Magpie settings")
    for key in (SCREEN_DB, RECORDER_INDEX):
        if key in config and (not isinstance(config[key], str) or not Path(config[key]).is_absolute()):
            raise ValueError("Daily source needs an absolute local path")
    return config


def unix_time(value):
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        raise ValueError("Missing UTC offset")
    stamp = parsed.timestamp()
    if stamp <= 0:
        raise ValueError("Invalid timestamp")
    return stamp


def identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing source identifier")
    return value


def magpie_interval(row):
    return (identity(row["agent"]), identity(row["id"])), unix_time(row["start"]), unix_time(row["last"])


def recorder_interval(row):
    start = unix_time(row["createdAt"])
    duration = row["durationMs"]
    if type(duration) not in (int, float) or not isfinite(duration) or duration < 0:
        raise ValueError("Invalid recording duration")
    return (identity(row["id"]),), start, start + duration / 1000


def interval_counts(rows, adapt, lo, hi):
    if not isinstance(rows, list):
        raise ValueError("Expected source rows")
    unique = {}
    conflicts = set()
    invalid = 0
    for row in rows:
        try:
            key, start, end = adapt(row)
            if not isfinite(end) or end < start:
                raise ValueError("Invalid time range")
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        span = (start, end)
        if key not in unique:
            unique[key] = span
        elif unique[key] != span:
            conflicts.add(key)

    hours = Counter()
    interval_only = 0
    for key, (start, end) in unique.items():
        if key in conflicts or start >= hi or (start < lo and end <= lo):
            continue
        if start < lo and end >= hi:
            interval_only += 1
        else:
            anchor = start if start >= lo else end
            hours[int((anchor - lo) // 3600)] += 1
    return hours, {
        "status": "partial" if invalid or conflicts else "ok",
        "interval_only": interval_only,
        "sampled_rows": len(rows),
        "invalid_rows": invalid,
        "conflicting_ids": len(conflicts),
    }


def read_magpie(limit):
    result = subprocess.run(
        ["magpie", "sessions", "--limit", str(limit), "--json"],
        check=True, timeout=10, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if not result.stdout.strip():
        raise ValueError("Empty Magpie response")
    return json.loads(result.stdout)


def read_recorder(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid recorder index")
    return data.get("sessions")


def count_wechat(world, lo, hi):
    (world.state / "index.sqlite").stat()
    with world.index() as db:
        hours = dict(db.execute(
            """SELECT CAST((timestamp - ?) / 3600 AS INTEGER), count(*) FROM messages
               WHERE timestamp >= ? AND timestamp < ? GROUP BY 1""", (lo, lo, hi),
        ))
        row = db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        manifest = json.loads(row[0]) if row else {}
        if not isinstance(manifest, dict):
            raise ValueError("Invalid archive manifest")
        synthetic = manifest.get("synthetic") is True
    return hours, {"status": "ok", "synthetic": synthetic}


def count_screen(path, lo, hi):
    path = Path(path)
    path.stat()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        hours = dict(db.execute(
            """SELECT CAST((ts - ?) / 3600 AS INTEGER), count(*) FROM frames
               WHERE ts >= ? AND ts < ? AND blacklisted=0
                 AND app IS NOT NULL AND trim(app)<>'' GROUP BY 1""", (lo, lo, hi),
        ))
    return hours, {"status": "ok"}


def read_source(read):
    try:
        return read()
    except FileNotFoundError:
        return {}, {"status": "not_found"}
    except (OSError, sqlite3.Error, TypeError, ValueError, subprocess.SubprocessError):
        return {}, {"status": "read_failed"}


def snapshot(state, world, day):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        raise ValueError("Expected YYYY-MM-DD")
    lo, hi = date_bounds(day, day)
    batches = {name: ({}, {"status": "disabled"}) for name in SOURCES}
    batches["wechat"] = read_source(lambda: count_wechat(world, lo, hi))
    try:
        config = source_config(state)
    except (AppError, OSError, ValueError):
        for name in SOURCES[1:]:
            batches[name] = {}, {"status": "config_error"}
    else:
        if config.get("magpie"):
            limit = config.get("magpie_limit", 200)
            hours, stats = read_source(lambda: interval_counts(read_magpie(limit), magpie_interval, lo, hi))
            batches["magpie"] = hours, {**stats, "sample_limit": limit}
        if SCREEN_DB in config:
            batches["screen_memory"] = read_source(lambda: count_screen(config[SCREEN_DB], lo, hi))
        if RECORDER_INDEX in config:
            batches["dsh_recorder"] = read_source(
                lambda: interval_counts(read_recorder(config[RECORDER_INDEX]), recorder_interval, lo, hi)
            )

    return {
        "date": day,
        "timezone": "Asia/Shanghai",
        "sources": {
            name: {**stats, "observations": sum(hours.values()) + stats.get("interval_only", 0)}
            for name, (hours, stats) in batches.items()
        },
        "hours": [
            {"hour": hour, "counts": {
                name: counts[hour] for name, (counts, _) in batches.items() if hour in counts
            }}
            for hour in sorted({hour for counts, _ in batches.values() for hour in counts})
        ],
    }
