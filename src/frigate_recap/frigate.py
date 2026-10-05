"""Frigate REST API client with retries, plus the event model and day filtering."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

PAGE_SIZE = 500
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
CLIP_TEMPLATE = "/api/events/{event_id}/clip.mp4"
OPENAPI_CANDIDATES = ("/api/openapi.json", "/openapi.json")


class FrigateError(Exception):
    """A one-line message suitable for a cron log."""


class FrigateAuthError(FrigateError):
    pass


class FrigateUnreachableError(FrigateError):
    pass


class FrigateApiError(FrigateError):
    pass


SKIP_NO_CLIP = "has_clip false"
SKIP_IN_PROGRESS = "event still in progress (no end_time)"
SKIP_OUT_OF_WINDOW = "starts outside the requested day"
SKIP_CAMERA_FILTER = "outside --camera filter"
SKIP_LABEL_FILTER = "outside --label filter"
SKIP_ZONE_FILTER = "outside --zone filter"
SKIP_LOW_SCORE = "score below --min-score"
SKIP_DOWNLOAD_FAILED = "clip download failed after retries"
SKIP_PROBE_FAILED = "clip could not be probed"
SKIP_TOO_SHORT = "clip shorter than the crossfade window"


@dataclass(frozen=True)
class RecapEvent:
    id: str
    camera: str
    label: str
    zones: tuple[str, ...]
    start_time: float
    end_time: float | None
    has_clip: bool
    has_snapshot: bool
    score: float | None

    @property
    def duration(self) -> float | None:
        if self.end_time is None:
            return None
        return max(0.0, self.end_time - self.start_time)

    @classmethod
    def from_api(cls, raw: dict) -> "RecapEvent":
        data = raw.get("data") or {}
        end_time = raw.get("end_time")
        score = data.get("score")
        return cls(
            id=str(raw.get("id", "")),
            camera=str(raw.get("camera", "")),
            label=str(raw.get("label", "")),
            zones=tuple(raw.get("zones") or ()),
            start_time=float(raw.get("start_time") or 0.0),
            end_time=float(end_time) if end_time is not None else None,
            has_clip=bool(raw.get("has_clip")),
            has_snapshot=bool(raw.get("has_snapshot")),
            score=float(score) if score is not None else None,
        )


@dataclass(frozen=True)
class SkippedEvent:
    event: RecapEvent
    reason: str


def select_events(
    raw_events: list[dict],
    window: tuple[float, float],
    camera: str | None = None,
    labels: tuple[str, ...] = (),
    zone: str | None = None,
    min_score: float | None = None,
) -> tuple[list[RecapEvent], list[SkippedEvent]]:
    """Apply every filter client-side; the API query is only a pre-filter.

    Ordering is (start_time, id) because the API returns start_time DESC and
    gives no order guarantee between events that share a start_time.
    """
    window_start, window_end = window
    included: list[RecapEvent] = []
    skipped: list[SkippedEvent] = []
    seen: set[str] = set()

    for raw in raw_events:
        event = RecapEvent.from_api(raw)
        if event.id in seen:
            continue
        seen.add(event.id)

        if not (window_start <= event.start_time < window_end):
            skipped.append(SkippedEvent(event, SKIP_OUT_OF_WINDOW))
        elif camera and event.camera != camera:
            skipped.append(SkippedEvent(event, SKIP_CAMERA_FILTER))
        elif labels and event.label not in labels:
            skipped.append(SkippedEvent(event, SKIP_LABEL_FILTER))
        elif zone and zone not in event.zones:
            skipped.append(SkippedEvent(event, SKIP_ZONE_FILTER))
        elif not event.has_clip:
            skipped.append(SkippedEvent(event, SKIP_NO_CLIP))
        elif event.end_time is None:
            skipped.append(SkippedEvent(event, SKIP_IN_PROGRESS))
        elif min_score is not None and (event.score is None or event.score < min_score):
            skipped.append(SkippedEvent(event, SKIP_LOW_SCORE))
        else:
            included.append(event)

    included.sort(key=lambda e: (e.start_time, e.id))
    return included, skipped


class FrigateClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        bearer_token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        attempts: int = RETRY_ATTEMPTS,
        trust_env: bool = True,
        sleep=time.sleep,
    ):
        headers = {}
        if api_key:
            headers["x-api-key"] = api_key
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=trust_env,
            follow_redirects=True,
        )
        self._attempts = max(1, attempts)
        self._sleep = sleep
        self._clip_template: str | None = None

    def close(self) -> None:
        self._client.close()

    def clip_url(self, event_id: str) -> str:
        """Clip URL for an event, from the server's own OpenAPI when reachable.

        0.14 through 0.18 all expose the same route, so any probe failure
        quietly falls back to the known template; this only catches drift.
        """
        if self._clip_template is None:
            self._clip_template = self._resolve_clip_template()
        return self._clip_template.format(event_id=quote(event_id, safe=""))

    def _resolve_clip_template(self) -> str:
        for candidate in OPENAPI_CANDIDATES:
            try:
                resp = self._client.get(candidate)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                continue
            try:
                paths = (resp.json() or {}).get("paths") or {}
            except ValueError:
                continue
            for path in paths:
                if path.endswith("/clip.mp4") and "{event_id}" in path:
                    return path if path.startswith("/api/") else f"/api{path}"
        return CLIP_TEMPLATE

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(RETRY_BACKOFF_SECONDS * attempt)
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if resp.status_code in (401, 403):
                # Auth failures are deterministic; retrying would only delay the answer.
                raise FrigateAuthError(f"auth failed: HTTP {resp.status_code}")
            if 200 <= resp.status_code < 300:
                return resp
            if resp.status_code >= 500:
                last_error = FrigateApiError(f"HTTP {resp.status_code} from {path}")
                continue
            raise FrigateApiError(f"HTTP {resp.status_code} from {path}")
        if isinstance(last_error, FrigateApiError):
            raise last_error
        raise FrigateUnreachableError(f"unreachable API: {last_error}")

    def _get_json(self, path: str, params: dict | None = None):
        resp = self._get(path, params)
        try:
            return resp.json()
        except ValueError as exc:
            raise FrigateApiError(
                f"unexpected response from {path}: not JSON ({resp.headers.get('content-type', 'no content-type')})"
            ) from exc

    def events(
        self,
        after: float,
        before: float,
        camera: str | None = None,
        labels: tuple[str, ...] = (),
        zone: str | None = None,
        min_score: float | None = None,
    ) -> tuple[list[dict], str | None]:
        """Fetch events with start_time in (after, before). Returns (raw events, paging note).

        The singular `label`/`zone` params are the old names every 0.14-0.18
        release accepts; the server splits them on commas itself.
        """
        events: list[dict] = []
        seen: set[str] = set()
        offset = 0
        note: str | None = None

        while True:
            params: dict = {
                "after": f"{after:.3f}",
                "before": f"{before:.3f}",
                "limit": PAGE_SIZE,
                "offset": offset,
            }
            if camera:
                params["camera"] = camera
            if labels:
                params["label"] = ",".join(labels)
            if zone:
                params["zone"] = zone
            if min_score is not None:
                params["min_score"] = min_score

            page = self._get_json("/api/events", params)
            if not isinstance(page, list):
                raise FrigateApiError("unexpected /api/events response: expected a JSON array")

            fresh = [raw for raw in page if isinstance(raw, dict) and raw.get("id") not in seen]
            for raw in fresh:
                seen.add(str(raw["id"]))
            events.extend(fresh)

            if len(page) < PAGE_SIZE or not fresh:
                if len(page) >= PAGE_SIZE and not fresh:
                    note = f"server ignored offset paging; results capped at {PAGE_SIZE} events"
                break
            offset += len(page)

        return events, note

    def event_summary(self, timezone: str | None = None) -> list[dict]:
        params = {"timezone": timezone} if timezone else {}
        rows = self._get_json("/api/events/summary", params)
        if not isinstance(rows, list):
            raise FrigateApiError("unexpected /api/events/summary response: expected a JSON array")
        return rows

    def download_clip(self, event_id: str, dest_path: str) -> bool:
        """Download an event clip, retrying transient failures. False after 3 attempts."""
        url = self.clip_url(event_id)
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(RETRY_BACKOFF_SECONDS * attempt)
            try:
                with self._client.stream("GET", url) as resp:
                    if resp.status_code in (401, 403):
                        raise FrigateAuthError(f"auth failed: HTTP {resp.status_code}")
                    if resp.status_code == 404:
                        last_error = FrigateApiError("clip not found (HTTP 404)")
                        break
                    if resp.status_code >= 400:
                        last_error = FrigateApiError(f"HTTP {resp.status_code}")
                        if resp.status_code < 500:
                            break
                        continue
                    written = 0
                    with open(dest_path, "wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                            written += len(chunk)
                    declared = resp.headers.get("content-length")
                    # A closed connection can pass Content-Length and deliver less;
                    # reads return short without raising, so compare instead.
                    if declared is not None and written != int(declared):
                        last_error = FrigateApiError(
                            f"short read: got {written} of {declared} bytes"
                        )
                        continue
                    return True
            except httpx.HTTPError as exc:
                last_error = exc
                continue
        if isinstance(last_error, FrigateAuthError):
            raise last_error
        return False
