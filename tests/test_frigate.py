"""Client and filter tests. Fast: httpx.MockTransport, no server, no media."""
from __future__ import annotations

import httpx
import pytest

from frigate_recap import frigate as frigate_mod
from frigate_recap.config import day_window, parse_day
from frigate_recap.frigate import (
    FrigateApiError,
    FrigateAuthError,
    FrigateClient,
    FrigateUnreachableError,
    select_events,
    SKIP_CAMERA_FILTER,
    SKIP_IN_PROGRESS,
    SKIP_LABEL_FILTER,
    SKIP_LOW_SCORE,
    SKIP_NO_CLIP,
    SKIP_OUT_OF_WINDOW,
    SKIP_ZONE_FILTER,
)
from tests.fixtures import (
    DAY,
    CAR_ID,
    CAT_ID,
    PERSON_ID,
    duplicate_events,
    in_progress_event,
    low_score_event,
    midnight_spanning_event,
    no_clip_event,
    short_event,
    summary_extra_rows,
    summary_rows,
    worked_example_events,
)

WINDOW = day_window(DAY)


def make_client(handler, **over) -> FrigateClient:
    defaults = dict(
        transport=httpx.MockTransport(handler),
        attempts=3,
        sleep=lambda seconds: None,
        trust_env=False,
    )
    defaults.update(over)
    return FrigateClient("http://mock.test", **defaults)


def json_response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://mock.test"))


class TestSelectEvents:
    def test_orders_by_start_time_and_reports_skips(self):
        raw = [
            no_clip_event(),
            worked_example_events()[2],
            worked_example_events()[0],
            in_progress_event(),
            worked_example_events()[1],
        ]
        included, skipped = select_events(raw, WINDOW)
        assert [e.id for e in included] == [PERSON_ID, CAR_ID, CAT_ID]
        assert [s.reason for s in skipped] == [SKIP_NO_CLIP, SKIP_IN_PROGRESS]

    def test_duplicate_timestamps_order_by_id(self):
        included, _ = select_events(duplicate_events(), WINDOW)
        assert [e.id for e in included] == sorted(e.id for e in included)
        assert [e.id for e in included] == [d["id"] for d in sorted(duplicate_events(), key=lambda d: d["id"])]

    def test_midnight_spanning_event_belongs_to_start_day(self):
        included, skipped = select_events([midnight_spanning_event()], WINDOW)
        assert len(included) == 1
        assert not skipped

    def test_event_on_next_day_excluded(self):
        raw = [midnight_spanning_event()]
        next_window = day_window(parse_day("2026-10-05"))
        included, skipped = select_events(raw, next_window)
        assert included == []
        assert skipped[0].reason == SKIP_OUT_OF_WINDOW

    def test_filters(self):
        raw = worked_example_events()
        camera_only, _ = select_events(raw, WINDOW, camera="front_door")
        assert [e.camera for e in camera_only] == ["front_door"]

        label_only, _ = select_events(raw, WINDOW, labels=("cat", "car"))
        assert [e.label for e in label_only] == ["car", "cat"]

        zone_only, _ = select_events(raw, WINDOW, zone="backyard")
        assert [e.id for e in zone_only] == [CAT_ID]

        scored, _ = select_events(raw, WINDOW, min_score=0.88)
        assert [e.id for e in scored] == [PERSON_ID]

    def test_filter_reasons_recorded(self):
        raw = worked_example_events() + [low_score_event()]
        included, _ = select_events(raw, WINDOW, min_score=0.85)
        assert [e.id for e in included] == [PERSON_ID, CAR_ID]
        _, skipped = select_events(raw, WINDOW, min_score=0.88)
        reasons = {s.event.id: s.reason for s in skipped}
        assert reasons[CAR_ID] == SKIP_LOW_SCORE
        assert reasons[low_score_event()["id"]] == SKIP_LOW_SCORE

    def test_camera_and_label_skip_reasons(self):
        raw = worked_example_events()
        _, skipped = select_events(raw, WINDOW, camera="driveway", labels=("car",))
        assert all(s.reason in (SKIP_CAMERA_FILTER, SKIP_LABEL_FILTER) for s in skipped)
        _, skipped = select_events(raw, WINDOW, zone="nowhere")
        assert all(s.reason == SKIP_ZONE_FILTER for s in skipped)


class TestClient:
    def test_query_params(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return json_response([])

        client = make_client(handler)
        after, before = WINDOW
        client.events(after, before, camera="front_door", labels=("person", "car"),
                      zone="steps", min_score=0.7)
        client.close()
        assert seen["camera"] == "front_door"
        assert seen["label"] == "person,car"
        assert seen["zone"] == "steps"
        assert float(seen["min_score"]) == 0.7
        assert float(seen["after"]) == pytest.approx(after, abs=0.5)
        assert float(seen["before"]) == pytest.approx(before, abs=0.5)

    def test_pagination_and_server_offset_ignore(self, monkeypatch):
        monkeypatch.setattr(frigate_mod, "PAGE_SIZE", 2)
        events = worked_example_events()

        def handler(request):
            offset = int(request.url.params.get("offset", 0))
            # an old server that ignores offset returns the same full page
            return json_response(events if offset == 0 else events)

        client = make_client(handler)
        got, note = client.events(*WINDOW)
        client.close()
        assert len(got) == 3
        assert note and "capped" in note

    def test_pagination_multiple_pages(self, monkeypatch):
        monkeypatch.setattr(frigate_mod, "PAGE_SIZE", 2)
        events = worked_example_events()

        def handler(request):
            offset = int(request.url.params.get("offset", 0))
            return json_response(events[offset:offset + 2])

        client = make_client(handler)
        got, note = client.events(*WINDOW)
        client.close()
        assert len(got) == 3
        assert note is None

    def test_retries_500_once_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return json_response({"detail": "boom"}, status=500)
            return json_response([])

        client = make_client(handler)
        got, _ = client.events(*WINDOW)
        client.close()
        assert got == []
        assert calls["n"] == 2

    def test_500_exhausted_raises_api_error(self):
        def handler(request):
            return json_response({"detail": "boom"}, status=500)

        client = make_client(handler)
        with pytest.raises(FrigateApiError, match="HTTP 500"):
            client.events(*WINDOW)
        client.close()

    def test_401_raises_auth_error_with_briefed_message(self):
        client = make_client(lambda request: json_response({"message": "Unauthorized"}, status=401))
        with pytest.raises(FrigateAuthError) as excinfo:
            client.events(*WINDOW)
        client.close()
        assert str(excinfo.value) == "auth failed: HTTP 401"

    def test_403_also_auth(self):
        client = make_client(lambda request: json_response({}, status=403))
        with pytest.raises(FrigateAuthError):
            client.event_summary()
        client.close()

    def test_connection_error_is_unreachable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        client = make_client(handler)
        with pytest.raises(FrigateUnreachableError) as excinfo:
            client.events(*WINDOW)
        client.close()
        assert str(excinfo.value).startswith("unreachable API")

    def test_non_json_body(self):
        def handler(request):
            return httpx.Response(200, text="<html>proxy error</html>",
                                  request=httpx.Request("GET", "http://mock.test"))

        client = make_client(handler)
        with pytest.raises(FrigateApiError, match="not JSON"):
            client.events(*WINDOW)
        client.close()

    def test_summary_parses_rows(self):
        client = make_client(lambda request: json_response(summary_rows()))
        rows = client.event_summary()
        client.close()
        assert len(rows) == len(summary_rows())


class TestClipDownload:
    def _clip_handler(self, state, payload=b"mp4-bytes"):
        def handler(request):
            if request.url.path.endswith("openapi.json"):
                return httpx.Response(404, request=request)
            if not request.url.path.endswith("/clip.mp4"):
                return httpx.Response(404, request=request)
            state["calls"] = state.get("calls", 0) + 1
            if state["calls"] == 1:
                return json_response({}, status=500)
            return httpx.Response(200, content=payload,
                                  headers={"content-length": str(len(payload))},
                                  request=request)
        return handler

    def test_download_retry_once(self, tmp_path):
        state: dict = {}
        client = make_client(self._clip_handler(state))
        dest = tmp_path / "clip.mp4"
        assert client.download_clip(CAR_ID, str(dest)) is True
        client.close()
        assert dest.read_bytes() == b"mp4-bytes"
        assert state["calls"] == 2

    def test_download_fails_after_three_attempts(self, tmp_path):
        def handler(request):
            return json_response({}, status=500)

        client = make_client(handler)
        dest = tmp_path / "clip.mp4"
        assert client.download_clip(CAR_ID, str(dest)) is False
        client.close()
        assert not dest.exists()

    def test_download_detects_short_read(self, tmp_path):
        # http.client and httpx alike return short reads without raising, so the
        # byte count must be compared against Content-Length (LESSONS 2026-09-21).
        def handler(request):
            return httpx.Response(200, content=b"half",
                                  headers={"content-length": "10"},
                                  request=request)

        client = make_client(handler)
        dest = tmp_path / "clip.mp4"
        assert client.download_clip(CAT_ID, str(dest)) is False
        client.close()

    def test_download_404_is_not_transient(self, tmp_path):
        calls = {"n": 0}

        def handler(request):
            if request.url.path.endswith("openapi.json"):
                return httpx.Response(404, request=request)
            calls["n"] += 1
            return json_response({"message": "Not found"}, status=404)

        client = make_client(handler)
        dest = tmp_path / "clip.mp4"
        assert client.download_clip("missing-id", str(dest)) is False
        client.close()
        assert calls["n"] == 1

    def test_download_auth_failure_propagates(self, tmp_path):
        client = make_client(lambda request: json_response({}, status=401))
        with pytest.raises(FrigateAuthError):
            client.download_clip(CAR_ID, str(tmp_path / "clip.mp4"))
        client.close()


class TestClipPathProbe:
    def _handler(self, state, clip_template, openapi_status=200):
        def handler(request):
            state.setdefault("paths", []).append(request.url.path)
            if request.url.path in ("/api/openapi.json", "/openapi.json"):
                spec = {"paths": {
                    "/events": {},
                    "/review/{review_id}/clip.mp4": {},
                    clip_template: {"get": {}},
                }}
                return httpx.Response(openapi_status, json=spec,
                                      headers={"content-type": "application/json"},
                                      request=request)
            return httpx.Response(200, content=b"mp4-bytes",
                                  headers={"content-length": "9"},
                                  request=request)
        return handler

    def test_probe_prefixes_mount_relative_spec_path(self, tmp_path):
        # real Frigate serves its spec at /api/openapi.json with paths that
        # lack the /api mount prefix (measured on demo.frigate.video)
        state: dict = {}
        client = make_client(self._handler(state, "/events/{event_id}/clip.mp4"))
        assert client.download_clip("abc", str(tmp_path / "c.mp4")) is True
        client.close()
        assert "/api/events/abc/clip.mp4" in state["paths"]

    def test_probe_keeps_already_prefixed_spec_path(self, tmp_path):
        state: dict = {}
        client = make_client(self._handler(state, "/api/events/{event_id}/clip.mp4"))
        assert client.download_clip("abc", str(tmp_path / "c.mp4")) is True
        client.close()
        assert "/api/events/abc/clip.mp4" in state["paths"]

    def test_probe_ignores_review_route(self, tmp_path):
        state: dict = {}
        client = make_client(self._handler(state, "/events/{event_id}/clip.mp4"))
        url = client.clip_url("abc")
        client.close()
        assert url == "/api/events/abc/clip.mp4"

    def test_fallback_when_openapi_unreachable(self, tmp_path):
        state: dict = {}

        def handler(request):
            state.setdefault("paths", []).append(request.url.path)
            if request.url.path.endswith("openapi.json"):
                return httpx.Response(404, request=request)
            return httpx.Response(200, content=b"mp4-bytes",
                                  headers={"content-length": "9"},
                                  request=request)

        client = make_client(handler)
        assert client.download_clip("abc", str(tmp_path / "c.mp4")) is True
        client.close()
        assert "/api/events/abc/clip.mp4" in state["paths"]

    def test_fallback_when_spec_is_html(self, tmp_path):
        # a live instance's /openapi.json serves the SPA HTML (measured)
        state: dict = {}

        def handler(request):
            state.setdefault("paths", []).append(request.url.path)
            if request.url.path == "/openapi.json":
                return httpx.Response(200, text="<!doctype html>", request=request)
            return httpx.Response(200, content=b"mp4-bytes",
                                  headers={"content-length": "9"},
                                  request=request)

        client = make_client(handler)
        assert client.clip_url("abc") == "/api/events/abc/clip.mp4"
        client.close()


class TestSummaryDayKeys:
    def test_window_maps_to_at_most_two_utc_days(self):
        from frigate_recap.manifest import day_keys_for_window

        keys = day_keys_for_window(WINDOW)
        assert DAY.isoformat() in keys
        assert len(keys) <= 2
        assert keys <= {"2026-10-03", "2026-10-04", "2026-10-05"}

    def test_summary_rows_filtered_by_window_and_filters(self):
        from frigate_recap.manifest import count_summary, filter_summary_rows

        rows = summary_rows() + summary_extra_rows()
        kept = filter_summary_rows(rows, WINDOW)
        # only the far-off day is dropped; patio is same-day so it stays
        assert all(r["day"] == DAY.isoformat() for r in kept)
        assert {r["camera"] for r in kept} == {"front_door", "driveway", "backyard", "patio"}

        kept = filter_summary_rows(rows, WINDOW, camera="driveway")
        assert [r["label"] for r in kept] == ["car"]

        kept = filter_summary_rows(rows, WINDOW, labels=("cat",))
        assert [r["camera"] for r in kept] == ["backyard"]

        events, cameras = count_summary(kept)
        assert (events, cameras) == (1, 1)
