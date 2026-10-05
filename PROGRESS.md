# PROGRESS: frigate-recap

## Phase 0 verification (2026-10-05, all re-fetched live this session)

| Resource | Claim | Status |
|---|---|---|
| docs.frigate.video/integrations/api/events-summary-events-summary-get | `GET /events/summary`, "Access: Any authenticated user", per-day/camera/label counts | VERIFIED (page live; source confirms row shape `{camera,label,sub_label,data,day,zones,count}`) |
| docs.frigate.video/configuration/record | "defaults to time-lapse output settings (25x speed, 30 FPS) with audio removed (-an)" | VERIFIED verbatim on page |
| github.com/blakeblackshear/frigate/issues/54 | maintainer-opened pinned request, "Something like this would be incredible." | VERIFIED (open, labels: enhancement + planned) |
| github.com/blakeblackshear/frigate/issues/23886 | PaulEins 2026-08-02 daily-recap request, closed dup of #54 | VERIFIED (exact quote present) |
| github.com/blakeblackshear/frigate/discussions/7519 | NickM-27: separate repo using the API is the shape; zmMagik license-incompatible | VERIFIED (collaborator reply, both facts) |
| github.com/blakeblackshear/frigate | repo live; API shapes | VERIFIED (36.4k stars; live demo instance used as a real server) |

Extra verification beyond the brief:
- Event clip endpoint read from source at two versions: v0.14.0 `@MediaBp.route("/events/<id>/clip.mp4")` and master `@router.get("/events/{event_id}/clip.mp4")` -> stable `GET /api/events/{id}/clip.mp4` across 0.14-0.18.
- `GET /api/events` params confirmed at v0.14.0 (Flask: after/before/camera/label/zone/min_score/has_clip, limit) and master (FastAPI, same names). Server uses strict `start_time > after` / `< before`, default sort start_time DESC -> client sorts + window-filters itself. `offset` paging deduped by id defensively.
- LIVE demo server (demo.frigate.video) used as a real Frigate throughout: real event JSON recorded for fixtures, `min_score` and `has_clip` server semantics probed (0.18 types `has_clip` as int; our client never sends it), and a real 8-clip recap rendered from the live `beach` camera (exit 0, 23.4s).
- Auth: Frigate's own API auth is JWT bearer via /api/login (port 8971). `x-api-key` is the reverse-proxy pattern -> tool sends both if configured (`FRIGATE_API_KEY` as x-api-key per brief, `FRIGATE_TOKEN` as bearer).
- Cost: zero paid inputs (self-hosted Frigate, httpx/Pillow/tzdata permissive licenses, system ffmpeg, Docker already installed, GitHub Actions/ghcr free). No cost barrier.

## State: v1.1 review pass COMPLETE (2026-10-05, same day)

Full review-fix-enhancement loop shipped as **0.1.1**:

Fixes found by re-review + real runs:
- Atomic output: final encode goes to `recap-<day>.mp4.part` and is `os.replace`d into place only after probing clean (the README's "never leaves a half-written recap" claim is now true; it was aspirational before).
- `-f mp4` on staging outputs (ffmpeg cannot infer a muxer from `.part` — caught by the suite, not by review).
- Clip downloads send `Accept-Encoding: identity` so a compressing proxy cannot false-positive the Content-Length short-read check into skipping every clip.
- ffprobe/ffmpeg subprocess output decoded with `errors=replace`; stdout/stderr reconfigured to UTF-8 (a non-cp1252 camera name would have raised inside print after a successful render, exit 1 on Windows).
- Card rows shrink to fit (long camera names ran off the card edge).
- `--min-score` range-checked, `--max-clip-seconds` must be positive (clean exit 1).

Enhancements:
- Manifest `clip_seconds` per included event: the duration actually in the video after clamping.
- Plan JSON carries `duration_basis` documenting that plan durations come from event metadata and real clips may run longer with pre/post capture.
- tests/test_cards.py (fit-to-width, sizes); version-subprocess test derives the version instead of pinning it.

Verification after the loop: 57 passed; examples regenerated on 0.1.1; docker acceptance re-run on the rebuilt 0.1.1 image (exit 0, 23.957s); live demo re-run (8 real clips, exit 0, no `.part` left behind).

## State: v1 COMPLETE

Note on the brief's worked example: it says "about 27s (1.5s title + 23.9s clips + 1.5s end card)", whose own pieces sum to 26.9. The real crossfade arithmetic is 1.5 + 22.5 + 1.5 - 4 joins x 0.4 = 23.9s total, which the build produces and tests assert within +-0.5s. The brief's "23.9s" figure matches the produced total exactly.

All 8 bar items met:
1. FEATURE-COMPLETE: day recap MP4 (title card, lower thirds, 0.4s xfade, stats end card, quiet-day card), all brief filters, --plan, manifest, exit codes 0/1/2, auth headers, retries, Dockerfile, CI, release workflow.
2. NO MOCKS in the product: the mock Frigate exists only in tests/; examples/ carries real rendered pixels from the real pipeline.
3. REAL END-TO-END, by the tool itself: (a) worked example against the mock -> exit 0, 23.900s, ffprobe-verified h264 1920x1080 30fps aac 48k stereo, drawtext lines exactly as briefed; (b) live demo.frigate.video -> 8 real events, exit 0, 23.4s; (c) docker run against the mock -> exit 0, 23.957s (within +-0.5s of local).
4. HANDLES REALITY: 401 -> exit 2 one-liner; unreachable -> exit 2 one-liner; 500 retries x3; short reads detected against Content-Length; all-clips-failed -> quiet card + report; bad --day / missing FRIGATE_URL -> clean exit 1; missing ffmpeg -> clean exit 1 (proven live during the demo run before .tools was on PATH).
5. TESTS: 51 passed (0 skipped with ffmpeg present) via `.venv\Scripts\python.exe -m pytest -q` and `uv run pytest -q` (the CI command).
6. PACKAGING: pyproject (hatchling, console script `frigate-recap`, MIT, keywords, repo URLs), uv.lock committed, Dockerfile (ffmpeg + fonts + tzdata, non-root), CI (ubuntu+windows, uv --frozen) + release workflow (ghcr + GitHub Release on tags), .gitignore/.gitattributes (LF enforcement per LESSONS 120).
7. README: real GIF + card frames, exact commands, real output, config, exit codes, limitations, cron line, origin story.
8. VERSION 0.1.0 (brief prefers 0.1.0).

## Verified working (this box)
- `py -3.12` + `.venv` (uv 0.12.23 inside), ffmpeg N-127197 in `.tools/` (dev only, gitignored), Docker 29.8.0 (engine started during the build).

## Not done / owner-side
- Nothing published: no git remote push, no ghcr push, no GitHub release (rules forbid publishing; the owner ships from the phone). `git init` + commits done locally.
- Acceptance (3) on the owner's real Frigate needs FRIGATE_URL for their NVR; the live demo run is the closest proof available here.
- GitHub repo + topics (frigate/nvr/cctv/home-assistant/ffmpeg) + **v0.1.1 tag**: owner-operated push, then release.yml does the rest.

## Next steps (best-in-class candidates, v2 per brief non-goals)
- Pre-capture aware overlap trim (drop duplicated footage between adjacent events).
- Optional music bed with auto ducking; night-mode speed ramp (the Synology "Smart Time-Lapse" shape from issue #23886).
- --since/--until half-day windows and per-camera split outputs.
- HA add-on packaging + MQTT notification with the manifest summary.
- VLM captions (v2 non-goal here).
- Snapshot-based fallback when has_clip=false but has_snapshot=true (Kenburns stills segment).
- gh-pages rendered index of manifests (a watchable archive over time).
