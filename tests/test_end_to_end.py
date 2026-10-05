"""End-to-end renders against the mock Frigate. Requires ffmpeg/ffprobe."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from tests.conftest import ffmpeg_available, run_cli, serve
from tests.fixtures import (
    DAY,
    CAR_ID,
    PERSON_ID,
    generate_clips,
    summary_rows,
    worked_example_events,
)
from tests.mock_frigate import create_mock

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")

EXPECTED_THIRDS = [
    "08:12 front_door - person",
    "09:40 driveway - car",
    "23:14 backyard - cat",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("FRIGATE_URL", "FRIGATE_API_KEY", "FRIGATE_TOKEN", "FRIGATE_RECAP_FONT",
                "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")


@pytest.fixture(scope="module")
def clips_dir(tmp_path_factory):
    return generate_clips(tmp_path_factory.mktemp("clips"))


def probe(path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def video_stream(data: dict) -> dict:
    return next(s for s in data["streams"] if s["codec_type"] == "video")


def audio_stream(data: dict) -> dict:
    return next(s for s in data["streams"] if s["codec_type"] == "audio")


def mean_volume_db(path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )
    for line in proc.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    raise AssertionError("volumedetect produced no mean_volume")


def test_worked_example(tmp_path, clips_dir):
    app = create_mock(worked_example_events(), summary_rows(), clips=clips_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # a crashed earlier run leaves a staging file; the new run must replace it
    (out_dir / "recap-2026-10-04.mp4.part").write_bytes(b"stale")
    with serve(app) as base_url:
        code, stdout, stderr = run_cli([
            "--day", DAY.isoformat(), "--base-url", base_url,
            "--out", str(out_dir), "--keep-work",
        ])

    assert code == 0, stdout + stderr
    workdir = next(line.split("workdir kept: ", 1)[1].strip() for line in stdout.splitlines()
                   if line.startswith("workdir kept:"))

    video = out_dir / "recap-2026-10-04.mp4"
    assert video.is_file()
    assert list(out_dir.glob("*.part")) == []
    manifest = json.loads((out_dir / "recap-2026-10-04.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in manifest["included"]] == [
        "1791382353.012345-abc123", "1791386810.654321-def456", "1791434042.111222-ghi789"
    ]
    # clip_seconds is what is actually in the video: full length here because
    # every fixture clip is under --max-clip-seconds
    assert {e["id"]: e["clip_seconds"] for e in manifest["included"]} == {
        PERSON_ID: 7.2, CAR_ID: 9.8, "1791434042.111222-ghi789": 5.5
    }
    assert manifest["skipped"] == []
    assert manifest["per_camera"] == {"backyard": {"included": 1, "skipped": 0},
                                      "driveway": {"included": 1, "skipped": 0},
                                      "front_door": {"included": 1, "skipped": 0}}
    assert manifest["stats"] == {"events": 3, "cameras": 3, "source": "summary"}

    data = probe(video)
    duration = float(data["format"]["duration"])
    # 1.5 title + 7.2 + 9.8 + 5.5 clips + 1.5 end - 4 joins * 0.4
    assert duration == pytest.approx(23.9, abs=0.5)
    assert manifest["output"]["duration_seconds"] == pytest.approx(duration, abs=0.5)
    assert manifest["output"]["expected_duration_seconds"] == pytest.approx(23.9, abs=0.1)
    vs = video_stream(data)
    assert (vs["width"], vs["height"]) == (1920, 1080)
    assert vs["codec_name"] == "h264"
    assert vs["r_frame_rate"] == "30/1"
    as_ = audio_stream(data)
    assert as_["codec_name"] == "aac"
    assert as_["sample_rate"] == "48000"
    assert as_["channels"] == 2

    segments_dir = pathlib.Path(workdir)
    person = probe(segments_dir / "seg_0000.mp4")
    assert float(person["format"]["duration"]) == pytest.approx(7.2, abs=0.3)
    assert (video_stream(person)["width"], video_stream(person)["height"]) == (1920, 1080)
    assert audio_stream(person)["channels"] == 2
    car = probe(segments_dir / "seg_0001.mp4")
    # the car fixture clip is 640x480 (4:3): padded, never cropped
    assert (video_stream(car)["width"], video_stream(car)["height"]) == (1920, 1080)
    cat = probe(segments_dir / "seg_0002.mp4")
    assert float(cat["format"]["duration"]) == pytest.approx(5.5, abs=0.3)
    # the cat clip has no source audio: a silent track must have been generated
    assert mean_volume_db(segments_dir / "seg_0002.mp4") < -70.0

    frame = out_dir / "frame.png"
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-ss", "3", "-i", str(segments_dir / "seg_0000.mp4"),
         "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    assert frame.stat().st_size > 2000

    for third in EXPECTED_THIRDS:
        assert third in stdout


def test_download_fails_once_then_succeeds(tmp_path, clips_dir):
    app = create_mock(worked_example_events(), summary_rows(), clips=clips_dir,
                      fail_first={CAR_ID: 1})
    with serve(app) as base_url:
        code, stdout, stderr = run_cli(["--day", DAY.isoformat(), "--base-url", base_url, "--out", str(tmp_path)])
    assert code == 0
    assert app.state.clip_requests[CAR_ID] == 2
    manifest = json.loads((tmp_path / "recap-2026-10-04.json").read_text(encoding="utf-8"))
    assert len(manifest["included"]) == 3
    assert manifest["skipped"] == []


def test_download_fails_three_times_is_reported(tmp_path, clips_dir):
    app = create_mock(worked_example_events(), summary_rows(), clips=clips_dir,
                      fail_first={CAR_ID: 3})
    with serve(app) as base_url:
        code, stdout, stderr = run_cli(["--day", DAY.isoformat(), "--base-url", base_url, "--out", str(tmp_path)])
    assert code == 0
    assert app.state.clip_requests[CAR_ID] == 3
    manifest = json.loads((tmp_path / "recap-2026-10-04.json").read_text(encoding="utf-8"))
    assert len(manifest["included"]) == 2
    assert manifest["skipped"] == [
        {"id": CAR_ID, "camera": "driveway", "label": "car",
         "reason": "clip download failed after retries"}
    ]
    data = probe(tmp_path / "recap-2026-10-04.mp4")
    # 1.5 + 7.2 + 5.5 + 1.5 - 3 joins * 0.4
    assert float(data["format"]["duration"]) == pytest.approx(14.5, abs=0.5)


def test_empty_day_writes_quiet_card(tmp_path):
    app = create_mock([], [], clips={})
    with serve(app) as base_url:
        code, stdout, stderr = run_cli(["--day", DAY.isoformat(), "--base-url", base_url, "--out", str(tmp_path)])
    assert code == 0
    manifest = json.loads((tmp_path / "recap-2026-10-04.json").read_text(encoding="utf-8"))
    assert manifest["included"] == []
    data = probe(tmp_path / "recap-2026-10-04.mp4")
    assert float(data["format"]["duration"]) == pytest.approx(5.0, abs=0.3)
    vs = video_stream(data)
    assert (vs["width"], vs["height"]) == (1920, 1080)


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX can replace a file that is open")
def test_locked_output_fails_cleanly(tmp_path):
    # the previous recap open in a player blocks os.replace on Windows; that
    # must be a one-line error, not a traceback
    app = create_mock([], [], clips={})
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "recap-2026-10-04.mp4"
    lock = open(target, "wb")
    try:
        with serve(app) as base_url:
            code, stdout, stderr = run_cli(
                ["--day", DAY.isoformat(), "--base-url", base_url, "--out", str(out_dir)]
            )
    finally:
        lock.close()
    assert code == 1
    assert "cannot finalize" in stderr
    assert "Traceback" not in stderr


def test_wrong_api_key_exits_2(monkeypatch, tmp_path):
    app = create_mock(worked_example_events(), summary_rows(), require_api_key="right")
    monkeypatch.setenv("FRIGATE_API_KEY", "wrong")
    with serve(app) as base_url:
        code, stdout, stderr = run_cli(["--day", DAY.isoformat(), "--base-url", base_url, "--out", str(tmp_path)])
    assert code == 2
    assert "auth failed: HTTP 401" in stderr


def test_unreachable_exits_2(tmp_path):
    dead = f"http://127.0.0.1:{_closed_port()}"
    code, stdout, stderr = run_cli(["--day", DAY.isoformat(), "--base-url", dead, "--out", str(tmp_path)])
    assert code == 2
    assert stderr.strip().startswith("unreachable API")
    assert len(stderr.strip().splitlines()) == 1


def test_bad_day_exits_1(tmp_path):
    code, stdout, stderr = run_cli(["--day", "not-a-date", "--base-url", "http://127.0.0.1:1"])
    assert code == 1
    assert "invalid --day" in stderr


def test_missing_url_exits_1():
    code, stdout, stderr = run_cli(["--day", DAY.isoformat()])
    assert code == 1
    assert "FRIGATE_URL" in stderr


def test_console_script_version_subprocess():
    from frigate_recap import __version__

    proc = subprocess.run([sys.executable, "-m", "frigate_recap.cli", "--version"],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"frigate-recap {__version__}"


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
