"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

from . import __version__
from .cards import CardsError
from .config import DEFAULT_MAX_CLIP_SECONDS, RecapConfig, base_url_from_env, api_key_from_env, bearer_from_env, parse_day, resolve_tz
from .frigate import (
    FrigateAuthError,
    FrigateClient,
    FrigateError,
    FrigateUnreachableError,
)
from .normalize import ProbeError
from .render import RenderError, plan_recap, render_recap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frigate-recap",
        description="Turn one day of Frigate NVR events into a single digest MP4.",
    )
    parser.add_argument(
        "--day",
        metavar="YYYY-MM-DD",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="day to recap in local time (default: yesterday)",
    )
    parser.add_argument("--camera", metavar="NAME", help="only this camera")
    parser.add_argument("--label", metavar="A,B", help="only these labels, comma separated")
    parser.add_argument("--zone", metavar="NAME", help="only events that entered this zone")
    parser.add_argument("--min-score", type=float, metavar="0.0-1.0", help="minimum detection score")
    parser.add_argument(
        "--max-clip-seconds", type=float, default=DEFAULT_MAX_CLIP_SECONDS, metavar="S",
        help=f"cap each clip at this many seconds (default: {DEFAULT_MAX_CLIP_SECONDS:g})",
    )
    parser.add_argument("--out", default=".", metavar="DIR", help="output directory (default: current directory)")
    parser.add_argument("--base-url", metavar="URL", help="Frigate base URL (default: $FRIGATE_URL)")
    parser.add_argument(
        "--tz", metavar="ZONE", dest="tz",
        help="IANA timezone for the day window and lower-third clock "
             "(default: this machine's zone; e.g. Europe/London, America/New_York)",
    )
    parser.add_argument("--font", metavar="PATH", help="TrueType font for cards (default: auto-detect)")
    parser.add_argument("--ffmpeg", default=os.environ.get("FRIGATE_RECAP_FFMPEG", "ffmpeg"), metavar="PATH")
    parser.add_argument("--ffprobe", default=os.environ.get("FRIGATE_RECAP_FFPROBE", "ffprobe"), metavar="PATH")
    parser.add_argument(
        "--plan", action="store_true",
        help="print the render plan as JSON and exit; writes nothing, renders nothing",
    )
    parser.add_argument(
        "--keep-work", action="store_true",
        help="keep the temporary render working directory (downloaded clips, cards, segments)",
    )
    parser.add_argument("--version", action="version", version=f"frigate-recap {__version__}")
    return parser


def _labels(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(label.strip() for label in value.split(",") if label.strip())


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; camera and label names can
    # be any unicode and a print raising after a successful render would turn a
    # good run into exit 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.min_score is not None and not 0.0 <= args.min_score <= 1.0:
        print("error: --min-score must be between 0.0 and 1.0", file=sys.stderr)
        return 1
    if args.max_clip_seconds <= 0:
        print("error: --max-clip-seconds must be positive", file=sys.stderr)
        return 1

    base_url = args.base_url or base_url_from_env()
    if not base_url:
        print("error: FRIGATE_URL is not set and --base-url was not given", file=sys.stderr)
        return 1

    try:
        day = parse_day(args.day)
        tz = resolve_tz(args.tz)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    cfg = RecapConfig(
        base_url=base_url,
        day=day,
        api_key=api_key_from_env(),
        bearer_token=bearer_from_env(),
        camera=args.camera,
        labels=_labels(args.label),
        zone=args.zone,
        min_score=args.min_score,
        max_clip_seconds=args.max_clip_seconds,
        out_dir=args.out,
        font_path=args.font,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        tz_name=args.tz,
    )

    client = FrigateClient(cfg.base_url, cfg.api_key, cfg.bearer_token)
    try:
        if args.plan:
            plan = plan_recap(cfg, client)
            json.dump(plan, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
            return 0
        manifest = render_recap(cfg, client, keep_work=args.keep_work)
        skipped = len(manifest["skipped"])
        print(f"done: {len(manifest['included'])} clips, {skipped} skipped, exit 0")
        return 0
    except (FrigateAuthError, FrigateUnreachableError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (FrigateError, RenderError, CardsError, ProbeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # a cron job deserves a line, not a traceback
        if os.environ.get("FRIGATE_RECAP_DEBUG"):
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
