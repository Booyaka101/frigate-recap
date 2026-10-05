"""Card rendering tests. Skipped when the machine has no TrueType font."""
from __future__ import annotations

from datetime import date

import pytest
from PIL import Image

from frigate_recap.cards import (
    draw_end_card,
    draw_quiet_card,
    draw_title_card,
    end_card_rows,
    resolve_font,
)
from tests.fixtures import DAY

FONT = resolve_font()

pytestmark = pytest.mark.skipif(FONT is None, reason="no TrueType font on this machine")


def test_title_card_size(tmp_path):
    out = tmp_path / "title.png"
    draw_title_card(str(out), DAY, 3, 3, FONT)
    assert Image.open(out).size == (1920, 1080)


def test_title_card_with_filter_note_renders(tmp_path):
    out = tmp_path / "title.png"
    draw_title_card(str(out), DAY, 2, 1, FONT, filter_note="filtered: camera=front_door, min_score=0.7")
    assert Image.open(out).size == (1920, 1080)


def test_end_card_rows_caps_many_cameras():
    cameras = {f"camera_{i:02d}": i + 1 for i in range(20)}
    rows = end_card_rows(cameras, {"person": 5}, 2)
    assert len(rows) == 7
    assert rows[2] == "+18 more cameras (see manifest)"
    assert rows[4] == "person 5"
    assert rows[6] == "2 events skipped (see manifest)"


def test_end_card_rows_shows_small_systems_in_full():
    rows = end_card_rows({"front_door": 2, "driveway": 1}, {"car": 1}, 0)
    assert len(rows) == 4
    assert rows[0].startswith("front_door.") and rows[0].endswith(" 2")
    assert rows[1].startswith("driveway.") and rows[1].endswith(" 1")
    assert rows[3] == "car 1"


def test_long_camera_names_fit_on_the_end_card(tmp_path):
    out = tmp_path / "end.png"
    cameras = {"front_door_garden_gate_by_the_old_fence": 1,
               "driveway": 1}
    draw_end_card(str(out), DAY, 2, 0, cameras, {"person": 2}, FONT)
    img = Image.open(out)
    assert img.size == (1920, 1080)


def test_quiet_card(tmp_path):
    out = tmp_path / "quiet.png"
    draw_quiet_card(str(out), date(2026, 10, 4), FONT)
    assert Image.open(out).size == (1920, 1080)
