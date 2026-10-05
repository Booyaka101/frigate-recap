"""The machine-readable record of what a recap run included and skipped."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

from .config import RecapConfig
from .frigate import RecapEvent, SkippedEvent

SCHEMA_VERSION = 1


def day_keys_for_window(window: tuple[float, float]) -> set[str]:
    """UTC day strings the local window overlaps.

    /api/events/summary buckets by `day` in the server's timezone, which
    defaults to UTC, so filter its rows against these keys instead of
    re-deriving the local day server-side.
    """
    start, end = window
    first = datetime.fromtimestamp(start, tz=timezone.utc).date()
    last = datetime.fromtimestamp(end - 0.001, tz=timezone.utc).date()
    keys: set[str] = set()
    current = first
    while current <= last:
        keys.add(current.isoformat())
        current = date.fromordinal(current.toordinal() + 1)
    return keys


def filter_summary_rows(
    rows: list[dict],
    window: tuple[float, float],
    camera: str | None = None,
    labels: tuple[str, ...] = (),
) -> list[dict]:
    keys = day_keys_for_window(window)
    selected = []
    for row in rows:
        if not isinstance(row, dict) or row.get("day") not in keys:
            continue
        if camera and row.get("camera") != camera:
            continue
        if labels and row.get("label") not in labels:
            continue
        selected.append(row)
    return selected


def count_summary(rows: list[dict]) -> tuple[int, int]:
    events = sum(int(row.get("count") or 0) for row in rows)
    cameras = len({row.get("camera") for row in rows})
    return events, cameras


def build_manifest(
    cfg: RecapConfig,
    included: list[RecapEvent],
    skipped: list[SkippedEvent],
    stats: dict,
    output_file: str,
    output_duration: float | None,
    note: str | None = None,
    versions: dict | None = None,
) -> dict:
    per_camera: dict[str, dict[str, int]] = {}
    per_label: dict[str, int] = {}
    for event in included:
        cam = per_camera.setdefault(event.camera, {"included": 0, "skipped": 0})
        cam["included"] += 1
        per_label[event.label] = per_label.get(event.label, 0) + 1
    for skip in skipped:
        cam = per_camera.setdefault(skip.event.camera, {"included": 0, "skipped": 0})
        cam["skipped"] += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "frigate_url": cfg.base_url,
        "day": cfg.day.isoformat(),
        "timezone": cfg.tz_name or "local",
        "filters": {
            "camera": cfg.camera,
            "labels": list(cfg.labels),
            "zone": cfg.zone,
            "min_score": cfg.min_score,
            "max_clip_seconds": cfg.max_clip_seconds,
        },
        "stats": stats,
        "included": [
            {
                "id": event.id,
                "camera": event.camera,
                "label": event.label,
                "zones": list(event.zones),
                "start_time": event.start_time,
                "end_time": event.end_time,
                "duration_seconds": round(event.duration, 3) if event.duration is not None else None,
                "score": event.score,
            }
            for event in included
        ],
        "skipped": [
            {"id": skip.event.id, "camera": skip.event.camera, "label": skip.event.label, "reason": skip.reason}
            for skip in skipped
        ],
        "per_camera": {name: per_camera[name] for name in sorted(per_camera)},
        "per_label": {name: per_label[name] for name in sorted(per_label)},
        "output": {
            "file": output_file,
            "duration_seconds": round(output_duration, 3) if output_duration is not None else None,
            "width": 1920,
            "height": 1080,
            "fps": 30,
        },
        **({"versions": versions} if versions else {}),
        **({"note": note} if note else {}),
    }


def write(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
