# Changelog

## 0.1.1 - 2026-10-05

Review-pass fixes and small hardening. No output changes for a clean run except
two extra manifest fields.

- The recap is now written to `recap-YYYY-MM-DD.mp4.part` and moved into place
  only after it probes clean, so a crashed run can never leave a half-written
  file at the final name.
- Clip downloads request `Accept-Encoding: identity`, because the short-read
  check compares raw bytes and a compressing proxy would false-positive every
  clip into a skip.
- The manifest records `clip_seconds` per included event: the duration that is
  actually in the video after `--max-clip-seconds` clamping.
- Card rows shrink to fit, so long camera names stay on the card.
- `--min-score` is validated against 0.0-1.0 and `--max-clip-seconds` must be
  positive, both as clean one-line errors.
- Stdout and stderr are reconfigured to UTF-8 with replacement, so a camera or
  label name outside the console code page can no longer raise after a
  successful render on Windows.
- ffprobe/ffmpeg output is decoded with errors=replace for the same reason.

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
