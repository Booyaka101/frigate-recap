"""Title, stats and quiet-day cards drawn with Pillow."""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import OUT_HEIGHT, OUT_WIDTH

BG = (13, 17, 23)
FG = (230, 237, 243)
MUTED = (139, 148, 158)
ACCENT = (88, 166, 255)
RULE = (48, 54, 61)

FONT_CANDIDATES = {
    "Windows": ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"],
    "Linux": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "Darwin": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
}


class CardsError(Exception):
    """A one-line card failure."""


@dataclass(frozen=True)
class CardText:
    kicker: str
    headline: str
    subline: str
    rows: tuple[str, ...]
    footnote: str


def resolve_font(explicit: str | None = None) -> str | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_font = os.environ.get("FRIGATE_RECAP_FONT", "").strip()
    if env_font:
        candidates.append(env_font)
    candidates.extend(FONT_CANDIDATES.get(platform.system(), []))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def draw_title_card(
    path: str,
    day: date,
    event_count: int,
    camera_count: int,
    font_path: str | None,
    filter_note: str | None = None,
) -> None:
    weekday = day.strftime("%A, %d %B %Y").lstrip("0")
    if event_count == 1:
        body = "1 event"
    else:
        body = f"{event_count} events"
    body += f" from {camera_count} camera{'s' if camera_count != 1 else ''}"
    rows = (body, filter_note) if filter_note else (body,)
    _write_card(
        path,
        CardText(
            kicker="FRIGATE RECAP",
            headline=day.isoformat(),
            subline=weekday,
            rows=rows,
            footnote="frigate NVR",
        ),
        font_path,
    )


END_CARD_MAX_ROWS = 7


def end_card_rows(
    per_camera: dict[str, int],
    per_label: dict[str, int],
    skipped: int,
    max_rows: int = END_CARD_MAX_ROWS,
) -> tuple[str, ...]:
    """Rows that fit on the card: cameras first, then labels and the skipped
    line. Busy systems get a "+N more cameras" line instead of an overflow."""
    camera_rows = [f"{camera:.<24} {count}" for camera, count in per_camera.items()]
    tail: list[str] = []
    if per_label:
        tail += ["", "  ".join(f"{label} {count}" for label, count in per_label.items())]
    if skipped:
        tail += ["", f"{skipped} event{'s' if skipped != 1 else ''} skipped (see manifest)"]

    budget = max_rows - len(tail)
    if len(camera_rows) > budget:
        shown = max(budget - 1, 1)
        hidden = len(camera_rows) - shown
        camera_rows = camera_rows[:shown] + [f"+{hidden} more cameras (see manifest)"]
    return tuple(camera_rows + tail)


def draw_end_card(
    path: str,
    day: date,
    included: int,
    skipped: int,
    per_camera: dict[str, int],
    per_label: dict[str, int],
    font_path: str | None,
) -> None:
    _write_card(
        path,
        CardText(
            kicker=day.isoformat(),
            headline="DAY IN NUMBERS",
            subline=f"{included} clip{'s' if included != 1 else ''} in this recap",
            rows=end_card_rows(per_camera, per_label, skipped),
            footnote="made with frigate-recap",
        ),
        font_path,
    )


def draw_quiet_card(path: str, day: date, font_path: str | None) -> None:
    _write_card(
        path,
        CardText(
            kicker="FRIGATE RECAP",
            headline="Quiet day",
            subline=f"No events recorded on {day.isoformat()}",
            rows=(),
            footnote="made with frigate-recap",
        ),
        font_path,
    )


def _write_card(path: str, text: CardText, font_path: str | None) -> None:
    if not font_path:
        raise CardsError(
            "no TrueType font found; install fonts-dejavu-core (Debian/Ubuntu) "
            "or pass --font / set FRIGATE_RECAP_FONT"
        )
    try:
        sizes = {"kicker": 44, "headline": 148, "subline": 52, "row": 40, "footnote": 30}
        fonts = {name: ImageFont.truetype(font_path, size) for name, size in sizes.items()}
    except OSError as exc:
        raise CardsError(f"cannot load font {font_path}: {exc}") from exc

    img = Image.new("RGB", (OUT_WIDTH, OUT_HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (24, OUT_HEIGHT)], fill=ACCENT)

    def row_font(value: str):
        # camera names come from user config and can be long; shrink rather
        # than run off the right edge
        font = fonts["row"]
        while draw.textlength(value, font=font) > OUT_WIDTH - 200 and font.size > 18:
            font = ImageFont.truetype(font_path, font.size - 2)
        return font

    y = 190
    x = 140
    draw.text((x, y), text.kicker, font=fonts["kicker"], fill=ACCENT)
    y += 78
    draw.text((x, y), text.headline, font=fonts["headline"], fill=FG)
    y += 196
    draw.text((x, y), text.subline, font=fonts["subline"], fill=MUTED)
    y += 108

    for row in text.rows:
        draw.text((x, y), row, font=row_font(row), fill=FG if row else RULE)
        y += 58

    draw.text((x, OUT_HEIGHT - 90), text.footnote, font=fonts["footnote"], fill=MUTED)
    img.save(path, format="PNG")
