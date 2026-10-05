"""Render pipeline: events -> normalized segments -> xfade concat -> recap MP4."""
from __future__ import annotations

import shlex
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__, cards
from . import manifest as manifest_mod
from .config import (
    END_SECONDS,
    MIN_CLIP_SECONDS,
    OUT_SAMPLE_RATE,
    QUIET_SECONDS,
    RecapConfig,
    TITLE_SECONDS,
)

from .frigate import (
    FrigateClient,
    FrigateError,
    RecapEvent,
    SkippedEvent,
    select_events,
    SKIP_DOWNLOAD_FAILED,
    SKIP_PROBE_FAILED,
    SKIP_TOO_SHORT,
)
from .manifest import count_summary, filter_summary_rows
from .normalize import ProbeError, normalize_command, probe_clip


class RenderError(Exception):
    """A one-line render failure."""


DOWNLOAD_WORKERS = 4


def _download_all(
    client: FrigateClient,
    included: list[RecapEvent],
    workdir: Path,
    log,
) -> dict[str, Path | None]:
    """Download every clip concurrently. Missing entries failed after retries."""
    if not included:
        return {}
    workers = min(DOWNLOAD_WORKERS, len(included))
    log(f"downloading {len(included)} clips, {workers} at a time")
    results: dict[str, Path | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                client.download_clip, event.id,
                str(workdir / f"clip_{index:04d}_{event.id}.mp4"),
            ): (index, event)
            for index, event in enumerate(included)
        }
        for done_count, future in enumerate(as_completed(futures), start=1):
            index, event = futures[future]
            ok = future.result()  # re-raises auth failures in this thread
            results[event.id] = workdir / f"clip_{index:04d}_{event.id}.mp4" if ok else None
            log(f"downloaded {done_count}/{len(included)}")
    return results


def _promote(staging_path: Path, out_path: Path) -> None:
    try:
        os.replace(staging_path, out_path)
    except OSError as exc:
        # the common Windows case is the previous recap still open in a player
        raise RenderError(f"cannot finalize {out_path.name} (file locked?): {exc}") from exc


def lower_third(event: RecapEvent, tz=None) -> str:
    if tz is not None:
        when = datetime.fromtimestamp(event.start_time, tz=tz)
    else:
        when = datetime.fromtimestamp(event.start_time)
    return f"{when:%H:%M} {event.camera} - {event.label}"


@dataclass
class Segment:
    kind: str  # title | clip | end | quiet
    duration: float
    duration_is_estimate: bool
    lower_third: str | None = None
    event: RecapEvent | None = None

    def to_dict(self) -> dict:
        data: dict = {
            "kind": self.kind,
            "duration": round(self.duration, 3),
            "duration_is_estimate": self.duration_is_estimate,
            "lower_third": self.lower_third,
        }
        if self.event is not None:
            data.update(
                {
                    "event_id": self.event.id,
                    "camera": self.event.camera,
                    "label": self.event.label,
                    "start_time": self.event.start_time,
                }
            )
        return data


def plan_segments(
    included: list[RecapEvent],
    max_clip_seconds: float,
    title_seconds: float = TITLE_SECONDS,
    end_seconds: float = END_SECONDS,
    tz=None,
) -> tuple[list[Segment], list[SkippedEvent]]:
    """Chronological segment list with estimated (event-clock) durations."""
    if not included:
        return [Segment("quiet", QUIET_SECONDS, duration_is_estimate=False)], []
    segments: list[Segment] = [
        Segment("title", title_seconds, duration_is_estimate=False),
    ]
    skipped: list[SkippedEvent] = []
    for event in included:
        duration = event.duration
        if duration is None:
            skipped.append(SkippedEvent(event, SKIP_PROBE_FAILED))
            continue
        clipped = min(duration, max_clip_seconds)
        if clipped < MIN_CLIP_SECONDS:
            skipped.append(SkippedEvent(event, SKIP_TOO_SHORT))
            continue
        segments.append(
            Segment("clip", clipped, duration_is_estimate=True,
                    lower_third=lower_third(event, tz), event=event)
        )
    segments.append(Segment("end", end_seconds, duration_is_estimate=False))
    return segments, skipped


def total_duration(durations: list[float], xfade: float) -> float:
    joins = max(0, len(durations) - 1)
    return max(0.0, sum(durations) - joins * xfade)


def xfade_graphs(durations: list[float], xfade: float) -> tuple[str, str]:
    if len(durations) < 2:
        raise RenderError("xfade needs at least two segments")
    video_parts: list[str] = []
    audio_parts: list[str] = []
    prev_v, prev_a = "[0:v]", "[0:a]"
    consumed = durations[0]
    for i in range(1, len(durations)):
        offset = consumed - xfade
        if offset <= 0:
            raise RenderError(f"crossfade offset {offset:.3f}s is not positive; segments too short")
        last = i == len(durations) - 1
        out_v = "[vout]" if last else f"[v{i}]"
        out_a = "[aout]" if last else f"[a{i}]"
        video_parts.append(
            f"{prev_v}[{i}:v]xfade=transition=fade:duration={xfade:.3f}:offset={offset:.3f}{out_v}"
        )
        audio_parts.append(f"{prev_a}[{i}:a]acrossfade=d={xfade:.3f}{out_a}")
        prev_v, prev_a = out_v, out_a
        consumed = consumed + durations[i] - xfade
    return ";".join(video_parts), ";".join(audio_parts)


def _escape_filter_path(path: str) -> str:
    # The graph parser splits filter options on ':' even inside single quotes,
    # so colons need a backslash as well (C\:/style paths); a quote itself is
    # written by closing the quote and escaping. Backslashes are normalised to
    # forward slashes first so they stay literal inside the quotes.
    normalized = str(path).replace("\\", "/")
    normalized = normalized.replace("'", "'\\''")
    return normalized.replace(":", "\\:")


def drawtext_filter(font_path: str, textfile_path: str) -> str:
    return (
        f"drawtext=fontfile='{_escape_filter_path(font_path)}':"
        f"textfile='{_escape_filter_path(textfile_path)}':"
        "fontcolor=white:fontsize=36:box=1:boxcolor=black@0.45:boxborderw=14:"
        "x=(w-text_w)/2:y=h-text_h-40"
    )


def _run(cmd: list[str], what: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, errors="replace")
    except OSError as exc:
        raise RenderError(f"cannot run {cmd[0]!r} for {what}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{what} timed out") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"exit {proc.returncode}"
        raise RenderError(f"{what} failed: {detail}")


def _display(cmd: list[str]) -> str:
    return shlex.join(cmd)


def ffmpeg_version(ffmpeg: str) -> str:
    try:
        proc = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True,
                              timeout=30, errors="replace")
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.splitlines()[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def fetch_stats(client: FrigateClient, cfg: RecapConfig, included: list[RecapEvent]) -> dict:
    """Title-card stats. /api/events/summary is the source per the brief; the
    event list is the fallback when the endpoint errors."""
    try:
        rows = client.event_summary()
        rows = filter_summary_rows(rows, cfg.window, cfg.camera, cfg.labels)
        events, cameras = count_summary(rows)
        return {"events": events, "cameras": cameras, "source": "summary"}
    except FrigateError:
        cameras = len({event.camera for event in included})
        return {"events": len(included), "cameras": cameras, "source": "events"}


def plan_recap(cfg: RecapConfig, client: FrigateClient) -> dict:
    """Dry run: fetches data, prints the exact render plan, writes nothing."""
    window_start, window_end = cfg.window
    raw_events, paging_note = client.events(
        window_start, window_end, cfg.camera, cfg.labels, cfg.zone, cfg.min_score
    )
    included, skipped = select_events(raw_events, cfg.window, cfg.camera, cfg.labels, cfg.zone, cfg.min_score)
    stats = fetch_stats(client, cfg, included)
    segments, plan_skipped = plan_segments(
        included, cfg.max_clip_seconds, cfg.title_seconds, cfg.end_seconds, cfg.tz
    )
    skipped = skipped + plan_skipped

    durations = [segment.duration for segment in segments]
    video_graph = audio_graph = None
    if len(segments) >= 2:
        video_graph, audio_graph = xfade_graphs(durations, cfg.xfade_seconds)

    font = cards.resolve_font(cfg.font_path)
    commands = _plan_commands(cfg, segments, video_graph, audio_graph, client)

    return {
        "tool_version": __version__,
        "day": cfg.day.isoformat(),
        "base_url": cfg.base_url,
        "filters": {
            "camera": cfg.camera,
            "labels": list(cfg.labels),
            "zone": cfg.zone,
            "min_score": cfg.min_score,
            "max_clip_seconds": cfg.max_clip_seconds,
        },
        "stats": stats,
        "font": font,
        "segments": [segment.to_dict() for segment in segments],
        "xfade_seconds": cfg.xfade_seconds,
        "joins": max(0, len(segments) - 1),
        "total_duration": round(total_duration(durations, cfg.xfade_seconds), 3),
        "skipped": [
            {"id": skip.event.id, "camera": skip.event.camera, "label": skip.event.label, "reason": skip.reason}
            for skip in skipped
        ],
        "paging_note": paging_note,
        "video_graph": video_graph,
        "audio_graph": audio_graph,
        "commands": commands,
        "duration_basis": "event end-start from the API; rendered clips may run longer when pre/post capture applies",
        "note": "plan only: nothing was written and nothing was rendered",
    }


def final_command(ffmpeg: str, segment_paths, graph: str, out_path: str) -> list[str]:
    """Concat all segments with the xfade graph into the finished recap."""
    inputs: list[str] = []
    for path in segment_paths:
        inputs += ["-i", str(path)]
    return [
        ffmpeg, "-y", "-nostdin", "-v", "error", *inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(OUT_SAMPLE_RATE), "-ac", "2",
        "-movflags", "+faststart",
        # the staging name ends in .part, which ffmpeg cannot infer a muxer from
        "-f", "mp4",
        str(out_path),
    ]


def _plan_commands(cfg: RecapConfig, segments: list[Segment], video_graph: str | None, audio_graph: str | None, client: FrigateClient) -> list[str]:
    commands: list[str] = []
    work = "<workdir>"
    for index, segment in enumerate(segments):
        if segment.kind == "clip":
            src = client.clip_url(segment.event.id)  # type: ignore[union-attr]
            dst = f"{work}/seg_{index:04d}.mp4"
            font = cards.resolve_font(cfg.font_path) or "<font>"
            textfile = f"{work}/lower_{index:04d}.txt"
            extra = drawtext_filter(font, textfile)
            commands.append(_display(normalize_command(cfg.ffmpeg, src, dst, segment.duration, True, extra)))
        elif segment.kind in ("title", "end", "quiet"):
            png = f"{work}/{segment.kind}.png"
            dst = f"{work}/seg_{index:04d}.mp4" if segment.kind != "quiet" else f"{work}/{cfg.video_name}"
            commands.append(_display(card_command(cfg.ffmpeg, png, dst, segment.duration)))
    if video_graph:
        planned_paths = [f"{work}/seg_{index:04d}.mp4" for index in range(len(segments))]
        commands.append(
            _display(final_command(cfg.ffmpeg, planned_paths, f"{video_graph};{audio_graph}", f"{work}/{cfg.video_name}"))
        )
    return commands


def card_command(ffmpeg: str, png: str, dst: str, seconds: float) -> list[str]:
    return [
        ffmpeg, "-y", "-nostdin", "-v", "error",
        "-loop", "1", "-framerate", "30", "-i", str(png),
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={OUT_SAMPLE_RATE}",
        "-t", f"{seconds:.3f}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(OUT_SAMPLE_RATE), "-ac", "2",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(dst),
    ]


def render_recap(
    cfg: RecapConfig,
    client: FrigateClient,
    log=print,
    keep_work: bool = False,
) -> dict:
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / cfg.video_name
    # ffmpeg writes the output progressively, so encode to a staging name and
    # move into place only once the file probes clean: a crashed run can never
    # leave something at the final name that only looks like a recap.
    staging_path = out_dir / f"{cfg.video_name}.part"
    manifest_path = out_dir / cfg.manifest_name

    window_start, window_end = cfg.window
    log(f"frigate-recap {__version__}  day {cfg.day.isoformat()}  server {cfg.base_url}")
    raw_events, paging_note = client.events(
        window_start, window_end, cfg.camera, cfg.labels, cfg.zone, cfg.min_score
    )
    included, skipped = select_events(raw_events, cfg.window, cfg.camera, cfg.labels, cfg.zone, cfg.min_score)
    log(f"events: {len(raw_events)} fetched, {len(included)} usable, {len(skipped)} filtered out")

    stats = fetch_stats(client, cfg, included)
    note = paging_note

    font = cards.resolve_font(cfg.font_path)

    workdir = Path(tempfile.mkdtemp(prefix=f"frigate-recap-{cfg.day.isoformat()}-"))
    kept: list[RecapEvent] = []
    clip_seconds: dict[str, float] = {}
    try:
        title_png = workdir / "title.png"
        end_png = workdir / "end.png"

        if not included:
            log(f"no usable events; writing a {QUIET_SECONDS:.0f}s quiet-day card")
            cards.draw_quiet_card(str(title_png), cfg.day, font)
            _run(card_command(cfg.ffmpeg, str(title_png), str(staging_path), QUIET_SECONDS), "quiet-day card")
            final_duration = probe_clip(cfg.ffprobe, str(staging_path)).duration
            expected_duration = QUIET_SECONDS
            _promote(staging_path, out_path)
        else:
            cards.draw_title_card(str(title_png), cfg.day, stats["events"], stats["cameras"], font)

            segment_paths: list[Path] = []

            downloads = _download_all(client, included, workdir, log)
            for index, event in enumerate(included):
                downloaded = downloads.get(event.id)
                if downloaded is None:
                    skipped.append(SkippedEvent(event, SKIP_DOWNLOAD_FAILED))
                    log(f"clip {event.id} ({event.camera}/{event.label}): download failed after retries; skipped")
                    continue
                try:
                    info = probe_clip(cfg.ffprobe, str(downloaded))
                except ProbeError as exc:
                    skipped.append(SkippedEvent(event, SKIP_PROBE_FAILED))
                    log(f"clip {event.id}: {exc}; skipped")
                    continue
                duration = min(info.duration, cfg.max_clip_seconds)
                if duration < MIN_CLIP_SECONDS:
                    skipped.append(SkippedEvent(event, SKIP_TOO_SHORT))
                    log(f"clip {event.id}: {duration:.2f}s is shorter than the crossfade window; skipped")
                    continue

                text = lower_third(event, cfg.tz)
                if "%{" in text:
                    log(f"warning: lower third for {event.id} contains %{{; drawtext may treat it as text expansion")
                textfile = workdir / f"lower_{index:04d}.txt"
                textfile.write_text(text, encoding="utf-8", newline="\n")
                extra = drawtext_filter(font, str(textfile))

                seg = workdir / f"seg_{index:04d}.mp4"
                _run(
                    normalize_command(cfg.ffmpeg, str(downloaded), str(seg), duration, info.has_audio, extra),
                    f"normalize {event.camera}/{event.label}",
                )
                segment_paths.append(seg)
                kept.append(event)
                clip_seconds[event.id] = duration
                log(f"clip {len(kept)}/{len(included)}: {text} ({duration:.1f}s)")

            if not segment_paths:
                log(f"every clip failed; writing a {QUIET_SECONDS:.0f}s quiet-day card instead")
                note = (note + "; " if note else "") + "all clips failed to render; quiet-day card written"
                cards.draw_quiet_card(str(title_png), cfg.day, font)
                _run(card_command(cfg.ffmpeg, str(title_png), str(staging_path), QUIET_SECONDS), "quiet-day card")
                final_duration = probe_clip(cfg.ffprobe, str(staging_path)).duration
                expected_duration = QUIET_SECONDS
                _promote(staging_path, out_path)
            else:
                # The end card counts what is actually in the video, so a clip
                # lost to a download failure moves to the skipped line.
                per_camera: dict[str, int] = {}
                per_label: dict[str, int] = {}
                for event in kept:
                    per_camera[event.camera] = per_camera.get(event.camera, 0) + 1
                    per_label[event.label] = per_label.get(event.label, 0) + 1
                cards.draw_end_card(
                    str(end_png), cfg.day, len(kept), len(skipped),
                    per_camera, per_label, font,
                )
                segment_paths.insert(0, workdir / "seg_title.mp4")
                segment_paths.append(workdir / "seg_end.mp4")

                _run(card_command(cfg.ffmpeg, str(title_png), str(segment_paths[0]), cfg.title_seconds), "title card")
                _run(card_command(cfg.ffmpeg, str(end_png), str(segment_paths[-1]), cfg.end_seconds), "end card")

                # Use the probed segment durations for the graph so the offsets
                # match what is actually on disk, not what we asked for.
                durations = [probe_clip(cfg.ffprobe, str(path)).duration for path in segment_paths]
                video_graph, audio_graph = xfade_graphs(durations, cfg.xfade_seconds)
                graph = f"{video_graph};{audio_graph}"
                # The *_script options were removed from newer ffmpeg; a plain
                # argv argument works on every version we support. Windows
                # command lines cap around 32k chars, so guard rather than fail.
                if len(graph) > 30000:
                    raise RenderError(
                        f"filter graph is {len(graph)} chars, too long for one command; "
                        "narrow the day with --camera, --label or --max-clip-seconds"
                    )

                _run(final_command(cfg.ffmpeg, segment_paths, graph, str(staging_path)), "final concat")
                final_duration = probe_clip(cfg.ffprobe, str(staging_path)).duration
                expected_duration = total_duration(durations, cfg.xfade_seconds)
                _promote(staging_path, out_path)

        log(f"wrote {out_path} ({final_duration:.1f}s, stats source: {stats.get('source')})")
        manifest = manifest_mod.build_manifest(
            cfg,
            kept,
            skipped,
            stats,
            str(out_path),
            final_duration,
            note=note,
            versions={"frigate_recap": __version__, "ffmpeg": ffmpeg_version(cfg.ffmpeg)},
            clip_seconds=clip_seconds,
            expected_duration=expected_duration,
        )
        manifest_mod.write(str(manifest_path), manifest)
        log(f"wrote {manifest_path}")
        return manifest
    finally:
        if keep_work:
            log(f"workdir kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
