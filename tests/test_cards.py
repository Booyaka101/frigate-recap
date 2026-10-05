"""Card rendering tests. Skipped when the machine has no TrueType font."""
from __future__ import annotations

from datetime import date

import pytest
from PIL import Image

from frigate_recap.cards import draw_end_card, draw_quiet_card, draw_title_card, resolve_font
from tests.fixtures import DAY

FONT = resolve_font()

pytestmark = pytest.mark.skipif(FONT is None, reason="no TrueType font on this machine")


def test_title_card_size(tmp_path):
    out = tmp_path / "title.png"
    draw_title_card(str(out), DAY, 3, 3, FONT)
    assert Image.open(out).size == (1920, 1080)


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
