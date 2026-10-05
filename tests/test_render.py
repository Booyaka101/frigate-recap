"""Render-plan tests against the mock Frigate. No media, no ffmpeg required."""
from __future__ import annotations

import pytest

from frigate_recap.config import RecapConfig
from frigate_recap.frigate import FrigateClient
from frigate_recap.render import plan_recap
from tests.conftest import serve
from tests.mock_frigate import create_mock
from tests.fixtures import (
    DAY,
    duplicate_events,
    in_progress_event,
    midnight_spanning_event,
    no_clip_event,
    short_event,
    summary_extra_rows,
    summary_rows,
    worked_example_events,
)

EXPECTED_THIRDS = [
    "08:12 front_door - person",
    "09:40 driveway - car",
    "23:14 backyard - cat",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("FRIGATE_URL", "FRIGATE_API_KEY", "FRIGATE_TOKEN", "FRIGATE_RECAP_FONT"):
        monkeypatch.delenv(var, raising=False)


def make_cfg(base_url: str, tmp_path, **over) -> RecapConfig:
    labels = over.pop("labels", None)
    if isinstance(labels, str):
        labels = tuple(part.strip() for part in labels.split(",") if part.strip())
    tz = over.pop("tz", None)
    defaults = dict(base_url=base_url, day=DAY, out_dir=str(tmp_path),
                    labels=labels or (), tz_name=tz)
    defaults.update(over)
    return RecapConfig(**defaults)


def plan_client(base_url: str) -> FrigateClient:
    return FrigateClient(base_url, trust_env=False, sleep=lambda s: None)


def workdir_tree(root) -> set[tuple[str, bytes]]:
    snapshot = set()
    for path in root.rglob("*"):
        if path.is_file():
            snapshot.add((str(path.relative_to(root)), path.read_bytes()))
    return snapshot


def test_worked_example_plan(tmp_path):
    with serve(create_mock(worked_example_events(), summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))

    kinds = [segment["kind"] for segment in plan["segments"]]
    assert kinds == ["title", "clip", "clip", "clip", "end"]
    durations = [segment["duration"] for segment in plan["segments"]]
    assert durations == [1.5, 7.2, 9.8, 5.5, 1.5]
    thirds = [segment["lower_third"] for segment in plan["segments"] if segment["kind"] == "clip"]
    assert thirds == EXPECTED_THIRDS
    # 1.5 + 22.5 + 1.5 - 4 joins * 0.4 = 23.9s
    assert plan["total_duration"] == pytest.approx(23.9, abs=0.01)
    assert plan["joins"] == 4
    assert plan["xfade_seconds"] == 0.4
    assert plan["skipped"] == []
    assert plan["stats"] == {"events": 3, "cameras": 3, "source": "summary"}
    assert plan["video_graph"].startswith("[0:v][1:v]xfade=transition=fade:duration=0.400:offset=1.100")
    assert "acrossfade=d=0.400" in plan["audio_graph"]
    assert "drawtext" in " ".join(plan["commands"])
    assert plan["note"] == "plan only: nothing was written and nothing was rendered"
    assert "pre/post capture" in plan["duration_basis"]


def test_plan_writes_nothing(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    before = workdir_tree(tmp_path)
    with serve(create_mock(worked_example_events(), summary_rows())) as base_url:
        plan_recap(make_cfg(base_url, out_dir), plan_client(base_url))
    assert workdir_tree(tmp_path) == before


def test_has_clip_false_skipped_and_counted(tmp_path):
    events = worked_example_events() + [no_clip_event()]
    with serve(create_mock(events, summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))
    assert [s["kind"] for s in plan["segments"]].count("clip") == 3
    assert [s["reason"] for s in plan["skipped"]] == ["has_clip false"]


def test_in_progress_event_skipped(tmp_path):
    events = worked_example_events() + [in_progress_event()]
    with serve(create_mock(events, summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))
    assert any(s["reason"].startswith("event still in progress") for s in plan["skipped"])


def test_midnight_spanning_event_included(tmp_path):
    events = worked_example_events() + [midnight_spanning_event()]
    with serve(create_mock(events, summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))
    thirds = [s["lower_third"] for s in plan["segments"] if s["kind"] == "clip"]
    assert "23:59 driveway - car" in thirds


def test_duplicate_timestamps_ordered_by_id(tmp_path):
    events = duplicate_events()
    with serve(create_mock(events, [])) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))
    clip_ids = [s["event_id"] for s in plan["segments"] if s["kind"] == "clip"]
    assert clip_ids == sorted(clip_ids)


def test_filters_shrink_the_plan(tmp_path):
    with serve(create_mock(worked_example_events(), summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path, camera="driveway"), plan_client(base_url))
        thirds_camera = [s["lower_third"] for s in plan["segments"] if s["kind"] == "clip"]

        plan = plan_recap(make_cfg(base_url, tmp_path, labels="cat,person"), plan_client(base_url))
        thirds_labels = [s["lower_third"] for s in plan["segments"] if s["kind"] == "clip"]

        plan = plan_recap(make_cfg(base_url, tmp_path, zone="backyard"), plan_client(base_url))
        thirds_zone = [s["lower_third"] for s in plan["segments"] if s["kind"] == "clip"]

        plan = plan_recap(make_cfg(base_url, tmp_path, min_score=0.88), plan_client(base_url))
        thirds_score = [s["lower_third"] for s in plan["segments"] if s["kind"] == "clip"]

    assert thirds_camera == ["09:40 driveway - car"]
    assert thirds_labels == ["08:12 front_door - person", "23:14 backyard - cat"]
    assert thirds_zone == ["23:14 backyard - cat"]
    assert thirds_score == ["08:12 front_door - person"]


def test_max_clip_seconds_clamps(tmp_path):
    with serve(create_mock(worked_example_events(), summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path, max_clip_seconds=4.0), plan_client(base_url))
    durations = [s["duration"] for s in plan["segments"] if s["kind"] == "clip"]
    assert durations == [4.0, 4.0, 4.0]
    # 1.5 + 12.0 + 1.5 - 4 joins * 0.4
    assert plan["total_duration"] == pytest.approx(13.4, abs=0.01)


def test_too_short_clip_planned_as_skipped(tmp_path):
    events = worked_example_events() + [short_event()]
    with serve(create_mock(events, summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))
    assert any(s["reason"] == "clip shorter than the crossfade window" for s in plan["skipped"])
    assert all(s["event_id"] != short_event()["id"] for s in plan["segments"] if s["kind"] == "clip")


def test_quiet_day_plan(tmp_path):
    with serve(create_mock([], [])) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path), plan_client(base_url))
    assert [s["kind"] for s in plan["segments"]] == ["quiet"]
    assert plan["total_duration"] == 5.0
    assert plan["joins"] == 0
    assert plan["video_graph"] is None


def test_summary_filters_follow_the_run(tmp_path):
    rows = summary_rows() + summary_extra_rows()
    with serve(create_mock(worked_example_events(), rows)) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path, labels="person"), plan_client(base_url))
    # front_door person 1 + patio person 3, all on the recap day
    assert plan["stats"] == {"events": 4, "cameras": 2, "source": "summary"}


def test_explicit_timezone_shifts_lower_thirds(tmp_path):
    from datetime import datetime, timezone
    from tests.fixtures import epoch

    with serve(create_mock(worked_example_events(), summary_rows())) as base_url:
        plan = plan_recap(make_cfg(base_url, tmp_path, tz="UTC"), plan_client(base_url))
    thirds = [s["lower_third"] for s in plan["segments"] if s["kind"] == "clip"]
    stamp = datetime.fromtimestamp(epoch(8, 12, 33), tz=timezone.utc).strftime("%H:%M")
    assert thirds[0] == f"{stamp} front_door - person"


def test_unknown_timezone_is_a_clean_error(tmp_path):
    with serve(create_mock([], [])) as base_url:
        with pytest.raises(ValueError, match="unknown timezone"):
            plan_recap(make_cfg(base_url, tmp_path, tz="Mars/Olympus"), plan_client(base_url))


def test_min_score_out_of_range_is_rejected():
    code, out, err = _run_cli(["--day", DAY.isoformat(), "--base-url", "http://127.0.0.1:1",
                               "--min-score", "1.5"])
    assert code == 1
    assert "--min-score must be between 0.0 and 1.0" in err


def test_max_clip_seconds_must_be_positive():
    code, out, err = _run_cli(["--day", DAY.isoformat(), "--base-url", "http://127.0.0.1:1",
                               "--max-clip-seconds", "0"])
    assert code == 1
    assert "--max-clip-seconds must be positive" in err


def _run_cli(argv):
    import contextlib
    import io

    from frigate_recap import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()
