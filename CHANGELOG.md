# Changelog

## 0.1.0 - 2026-10-05

First release.

- `frigate-recap --day YYYY-MM-DD` renders one day of Frigate events into a single
  1080p30 MP4: 1.5s title card, every event clip normalized (padded, never cropped)
  with a burned-in `HH:MM camera - label` lower third, 0.4s crossfades, 1.5s stats
  end card, and a JSON manifest of what was included and skipped.
- Filters: `--camera`, `--label a,b`, `--zone`, `--min-score`, `--max-clip-seconds`.
- `--tz` sets the IANA timezone for the day window and the lower-third clock
  (default: the machine's own zone; containers usually want this).
- `--plan` prints the whole render plan as JSON without touching disk or ffmpeg.
- Quiet days produce a 5s quiet-day card instead of an empty file.
- Auth: `FRIGATE_API_KEY` sent as `x-api-key`, `FRIGATE_TOKEN` sent as bearer.
- Exit codes: 0 success, 1 error, 2 auth failure or unreachable API.
- Docker image with ffmpeg, DejaVu fonts and tzdata baked in.
