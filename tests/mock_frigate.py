"""A small FastAPI app that speaks enough of the Frigate API for tests.

Mirrors the server semantics that matter to the client: strict
`start_time > after` / `< before`, comma-split label, default sort
start_time DESC, limit/offset paging, and the 401 shape.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response


def create_mock(
    events: list[dict],
    summary: list[dict],
    clips: dict[str, Path] | None = None,
    require_api_key: str | None = None,
    fail_first: dict[str, int] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.clip_requests: dict[str, int] = {}
    app.state.event_params: list[dict] = []
    clips = clips or {}
    fail_first = fail_first or {}

    def authorized(request: Request) -> bool:
        if require_api_key is None:
            return True
        return request.headers.get("x-api-key") == require_api_key

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/events")
    def list_events(
        request: Request,
        after: float | None = None,
        before: float | None = None,
        camera: str = "all",
        label: str = "all",
        zone: str = "all",
        min_score: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        if not authorized(request):
            return JSONResponse({"message": "Unauthorized"}, status_code=401)
        app.state.event_params.append(request.url.query)

        selected = list(events)
        if after is not None:
            selected = [e for e in selected if e["start_time"] > after]
        if before is not None:
            selected = [e for e in selected if e["start_time"] < before]
        if camera != "all":
            selected = [e for e in selected if e["camera"] == camera]
        if label != "all":
            wanted = [part for part in label.split(",") if part]
            selected = [e for e in selected if e["label"] in wanted]
        if zone != "all":
            selected = [e for e in selected if zone in (e.get("zones") or [])]
        if min_score is not None:
            selected = [e for e in selected if (e.get("data", {}).get("score") or 0) >= min_score]
        selected.sort(key=lambda e: e["start_time"], reverse=True)
        return JSONResponse(selected[offset:offset + limit])

    @app.get("/api/events/summary")
    def events_summary(request: Request, timezone: str = "utc"):
        if not authorized(request):
            return JSONResponse({"message": "Unauthorized"}, status_code=401)
        return JSONResponse(summary)

    @app.get("/api/events/{event_id}/clip.mp4")
    def event_clip(event_id: str, request: Request):
        if not authorized(request):
            return JSONResponse({"message": "Unauthorized"}, status_code=401)
        count = app.state.clip_requests.get(event_id, 0) + 1
        app.state.clip_requests[event_id] = count
        if count <= fail_first.get(event_id, 0):
            return Response(content=b"boom", status_code=500)
        path = clips.get(event_id)
        if path is None or not path.is_file():
            return JSONResponse({"message": "Not found"}, status_code=404)
        return FileResponse(path, media_type="video/mp4", filename=f"{event_id}.mp4")

    return app
