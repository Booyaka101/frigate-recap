"""Runtime configuration and the local-day window a recap covers."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

OUT_WIDTH = 1920
OUT_HEIGHT = 1080
OUT_FPS = 30
OUT_SAMPLE_RATE = 48000
TITLE_SECONDS = 1.5
END_SECONDS = 1.5
QUIET_SECONDS = 5.0
XFADE_SECONDS = 0.4
DEFAULT_MAX_CLIP_SECONDS = 15.0
# A clip shorter than this cannot survive a crossfade at each end.
MIN_CLIP_SECONDS = XFADE_SECONDS + 0.1


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid --day {value!r} (expected YYYY-MM-DD)") from exc


def resolve_tz(name: str | None):
    """IANA name -> tzinfo. None means 'the machine's own zone'."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def day_window(day: date, tz=None) -> tuple[float, float]:
    """Epoch window [start, end) of the calendar day.

    Events are assigned to a day by start_time, so an event that begins at
    23:59 and ends after midnight belongs to the day it started in. The zone
    is the machine's own unless one was configured.
    """
    if tz is not None:
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    else:
        midnight = datetime(day.year, day.month, day.day)
    return midnight.timestamp(), (midnight + timedelta(days=1)).timestamp()


@dataclass(frozen=True)
class RecapConfig:
    base_url: str
    day: date
    api_key: str | None = None
    bearer_token: str | None = None
    camera: str | None = None
    labels: tuple[str, ...] = ()
    zone: str | None = None
    min_score: float | None = None
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS
    out_dir: str = "."
    font_path: str | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    title_seconds: float = TITLE_SECONDS
    end_seconds: float = END_SECONDS
    xfade_seconds: float = XFADE_SECONDS
    tz_name: str | None = None

    @property
    def tz(self):
        return resolve_tz(self.tz_name)

    @property
    def window(self) -> tuple[float, float]:
        return day_window(self.day, self.tz)

    @property
    def video_name(self) -> str:
        return f"recap-{self.day.isoformat()}.mp4"

    @property
    def manifest_name(self) -> str:
        return f"recap-{self.day.isoformat()}.json"


def base_url_from_env() -> str | None:
    value = os.environ.get("FRIGATE_URL", "").strip()
    return value.rstrip("/") or None


def api_key_from_env() -> str | None:
    return os.environ.get("FRIGATE_API_KEY", "").strip() or None


def bearer_from_env() -> str | None:
    return os.environ.get("FRIGATE_TOKEN", "").strip() or None
