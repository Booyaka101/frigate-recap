"""Fixture events and generated fixture clips.

Event dicts mirror the shape returned by a live Frigate (recorded from
demo.frigate.video), including the full key set around `data`. Times are
defined as wall-clock on the recap day and converted with the local zone, so
the `HH:MM` lower thirds are stable no matter which timezone the suite runs in.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

DAY = date(2026, 10, 4)

PERSON_ID = "1791382353.012345-abc123"
CAR_ID = "1791386810.654321-def456"
CAT_ID = "1791434042.111222-ghi789"


def epoch(hour: int, minute: int, second: int, day: date = DAY) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, second).timestamp()


def _event(event_id: str, camera: str, label: str, start: float, end: float | None,
           score: float, zones: list[str], has_clip: bool = True) -> dict:
    return {
        "id": event_id,
        "camera": camera,
        "end_time": end,
        "false_positive": False,
        "has_clip": has_clip,
        "has_snapshot": True,
        "label": label,
        "start_time": start,
        "plus_id": None,
        "retain_indefinitely": False,
        "sub_label": None,
        "top_score": score,
        "zones": zones,
        "box": [],
        "data": {
            "box": [0.1, 0.2, 0.3, 0.4],
            "region": [0.0, 0.0, 0.5, 0.9],
            "score": score,
            "type": "object",
            "attributes": [],
        },
        "thumbnail": None,
    }


def worked_example_events() -> list[dict]:
    """The three events from the brief: 7.2s + 9.8s + 5.5s, the cat clip silent."""
    return [
        _event(PERSON_ID, "front_door", "person", epoch(8, 12, 33),
               epoch(8, 12, 33) + 7.2, 0.91, ["front_door_steps"]),
        _event(CAR_ID, "driveway", "car", epoch(9, 40, 10),
               epoch(9, 40, 10) + 9.8, 0.87, ["driveway"]),
        _event(CAT_ID, "backyard", "cat", epoch(23, 14, 2),
               epoch(23, 14, 2) + 5.5, 0.78, ["backyard"]),
    ]


def no_clip_event() -> dict:
    return _event("1791380000.000001-skip01", "front_door", "person",
                  epoch(7, 30, 0), epoch(7, 30, 0) + 6.0, 0.83, [], has_clip=False)


def in_progress_event() -> dict:
    return _event("1791435000.000002-live01", "front_door", "person",
                  epoch(23, 30, 0), None, 0.80, [])


def midnight_spanning_event() -> dict:
    """Starts 23:59:30, ends after midnight: belongs to DAY by start_time."""
    return _event("1791439170.000003-midn01", "driveway", "car",
                  epoch(23, 59, 30), epoch(0, 5, 12, day=date(2026, 10, 5)), 0.9, [])


def duplicate_events() -> list[dict]:
    """Two events with the same start_time; (start_time, id) must order them."""
    return [
        _event("1791390000.000005-dup002", "driveway", "car", epoch(10, 0, 0),
               epoch(10, 0, 0) + 4.0, 0.8, []),
        _event("1791390000.000004-dup001", "front_door", "person", epoch(10, 0, 0),
               epoch(10, 0, 0) + 5.0, 0.8, []),
    ]


def low_score_event() -> dict:
    return _event("1791381000.000006-lowsc1", "backyard", "cat",
                  epoch(7, 45, 0), epoch(7, 45, 0) + 6.0, 0.42, [])


def short_event() -> dict:
    return _event("1791390100.000007-short1", "side_gate", "person",
                  epoch(11, 0, 0), epoch(11, 0, 0) + 0.3, 0.9, [])


def summary_rows() -> list[dict]:
    """Derived from the worked-example events, like a real Frigate where the
    summary and the event list come from the same database."""
    rows: dict[tuple[str, str], dict] = {}
    for event in worked_example_events():
        key = (event["camera"], event["label"])
        if key in rows:
            rows[key]["count"] += 1
        else:
            rows[key] = {
                "camera": event["camera"],
                "label": event["label"],
                "sub_label": None,
                "data": {"type": "object"},
                "day": DAY.isoformat(),
                "zones": event["zones"] or None,
                "count": 1,
            }
    # a row on a day no test window can reach
    result = sorted(rows.values(), key=lambda row: row["camera"])
    result.append({"camera": "front_door", "label": "person", "sub_label": None,
                   "data": {"type": "object"}, "day": "2026-09-01", "zones": None, "count": 9})
    return result


def summary_extra_rows() -> list[dict]:
    """Rows used only by summary-filter tests: another day and another camera."""
    return [
        {"camera": "patio", "label": "person", "sub_label": None,
         "data": {"type": "object"}, "day": DAY.isoformat(), "zones": None, "count": 3},
        {"camera": "front_door", "label": "person", "sub_label": None,
         "data": {"type": "object"}, "day": "2026-09-01", "zones": None, "count": 9},
    ]


CLIP_SPECS = {
    PERSON_ID: {"seconds": 7.2, "size": "1280x720", "audio": True},
    CAR_ID: {"seconds": 9.8, "size": "640x480", "audio": True},
    CAT_ID: {"seconds": 5.5, "size": "1280x720", "audio": False},
}


def generate_clips(directory: Path) -> dict[str, Path]:
    """Render the fixture clips with ffmpeg; returns event_id -> path."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for event_id, spec in CLIP_SPECS.items():
        path = directory / f"clip-{event_id}.mp4"
        cmd = [
            ffmpeg, "-y", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=duration={spec['seconds']}:size={spec['size']}:rate=30",
        ]
        if spec["audio"]:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={spec['seconds']}"]
        cmd += ["-t", f"{spec['seconds']:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p"]
        if spec["audio"]:
            cmd += ["-c:a", "aac", "-b:a", "96k"]
        else:
            cmd += ["-an"]
        cmd += [str(path)]
        subprocess.run(cmd, check=True, capture_output=True)
        paths[event_id] = path
    return paths
