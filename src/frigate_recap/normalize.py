"""Probe clips with ffprobe and build the normalizing ffmpeg arguments."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .config import OUT_FPS, OUT_HEIGHT, OUT_SAMPLE_RATE, OUT_WIDTH


class ProbeError(Exception):
    """A one-line probe failure."""


@dataclass(frozen=True)
class ClipInfo:
    path: str
    duration: float
    width: int
    height: int
    has_audio: bool


def probe_clip(ffprobe: str, path: str) -> ClipInfo:
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json", "-show_streams", "-show_format",
        str(path),
    ]
    try:
        # errors=replace: ffprobe stderr can carry arbitrary bytes from file paths
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, errors="replace")
    except OSError as exc:
        raise ProbeError(f"cannot run ffprobe {ffprobe!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out on {path}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown error"
        raise ProbeError(f"ffprobe failed on {path}: {detail}")

    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned unparsable output for {path}") from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError(f"no video stream in {path}")

    duration = None
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        duration = float(fmt["duration"])
    elif video.get("duration"):
        duration = float(video["duration"])
    if duration is None or duration <= 0:
        raise ProbeError(f"cannot determine duration of {path}")

    return ClipInfo(
        path=str(path),
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def video_filter(extra: str | None = None) -> str:
    """Normalize any input to 1080p30: fit inside, then pad. Never crops."""
    chain = (
        f"scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={OUT_WIDTH}:{OUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={OUT_FPS},setsar=1,format=yuv420p"
    )
    if extra:
        chain += "," + extra
    return chain


def audio_filter(has_audio: bool) -> str | None:
    if not has_audio:
        return None
    return (
        f"aresample={OUT_SAMPLE_RATE},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad"
    )


def normalize_command(
    ffmpeg: str,
    src: str,
    dst: str,
    duration: float,
    has_audio: bool,
    extra_video_filter: str | None = None,
) -> list[str]:
    """Build the argv for one clip -> uniform 1080p30 segment with AAC stereo audio.

    Sources without audio get a generated silent track, because concat filters
    and most players expect every segment to carry an audio stream.
    """
    cmd = [ffmpeg, "-y", "-nostdin", "-v", "error", "-i", str(src)]
    audio_map = "0:a:0"
    if not has_audio:
        cmd += [
            "-f", "lavfi", "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={OUT_SAMPLE_RATE}",
        ]
        audio_map = "1:a:0"
    vf = video_filter(extra_video_filter)
    af = audio_filter(has_audio)
    cmd += ["-vf", vf]
    if af:
        cmd += ["-af", af]
    cmd += [
        "-map", "0:v:0", "-map", audio_map,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(OUT_SAMPLE_RATE), "-ac", "2",
        "-movflags", "+faststart",
        str(dst),
    ]
    return cmd
