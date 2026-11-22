# Printvault

[![Tests](https://github.com/emphyri0/printvault/actions/workflows/tests.yml/badge.svg)](https://github.com/emphyri0/printvault/actions?workflow=tests.yml)
<!-- GitHub badge: replace OWNER once the repository is on GitHub, then remove these comment markers.
[![Tests](https://github.com/OWNER/printvault/actions/workflows/tests.yml/badge.svg)](https://github.com/OWNER/printvault/actions/workflows/tests.yml)
-->

A small web app to keep track of your 3D prints: photos, model files (3mf, stl, …), filament, print time, settings and notes.

## Features

- Prints with photos, model files (3mf, stl, step, obj, gcode), filament, print time, settings and notes
- Printers with a photo, and per print which printer made it
- Import from MakerWorld: paste a model link and the title, designer, cover, filament, weight and print time are filled in (you add the 3mf file yourself)
- Live connection with Bambu Lab printers via MQTT (read-only, cloud mode can stay on): prints are created and closed automatically, with AMS slots, and the home page shows the running print large at the top
- Error codes (print_error and HMS) as readable text, with a suggested fail reason for failed prints
- AMS humidity and temperature per unit, with the last 24 hours in the live block
- Maintenance tasks per printer with an interval in print hours and/or days
- Notifications via ntfy: print finished, print failed, maintenance due (each can be switched on/off)
- Live status like the printer screen: stage, progress, finish time, temperatures, speed, fans and active AMS slot
- Overview with search and filter chips (status, fail reason, material, nozzle, printer), dark theme, works on a phone
- Temperature and stage history per print as a chart on the detail page
- Live camera image of the printer at the top of the home page, and an automatic camera photo on the print when it finishes or fails

## Values and labels

Internal values are lowercase snake_case; the UI shows them through a label mapping (`LABELS` in `app.py`):

| Kind | Internal values |
|---|---|
| Status | `planned`, `printing`, `succeeded`, `failed`, `unknown`, `reprint` |
| Fail reason | `adhesion`, `warping`, `spaghetti`, `clog`, `power_loss`, `other` |
| Speed profile | `silent`, `standard`, `sport`, `ludicrous` |

Dates are shown as `22 Nov 2026` and times as `22 Nov 2026, 16:42` (24-hour clock).

## Live connection (MQTT)

Under **Printers → Edit**, fill in the IP address, serial number and access code and tick *Live connection on*. The `printvault-mqtt` service re-reads the settings every minute; adding printers needs no restart.

- Connection: `mqtts://<ip>:8883`, user `bblp`, password = access code, TLS without certificate verification. Subscribe only on `device/<serial>/report`; nothing is sent to the printer.
- `PREPARE`/`RUNNING` → print with status *Printing*; `FINISH` → *Succeeded* with end time and duration; `FAILED` → *Failed* with the printer's error code.
- If the listener restarts during a print, the existing *Printing* record is resumed (recognised by `task_id`). If the listener missed the end of a print and a new one starts, the old one gets status *Unknown* with a note (*Reprint* is only for prints you deliberately want to make again).
- The live status (percentage, time left, layer) lives only in the listener's memory and is available through `/api/live`.
- Status *Unknown*: the listener did not see the print end (e.g. because it was not connected). Check yourself how the print went.
- AMS: at most one reading per unit per 10 minutes in `ams_readings` (humidity in % when the firmware sends `humidity_raw`, otherwise Bambu's level 1–5, plus temperature).
- Log: `docker compose logs -f printvault-mqtt`

## Camera

Printers with an IP address and access code (X1, P2S, H2D: RTSPS on port 322) get a camera image. *LAN live view* must be enabled on the printer.

- At the top of the home page: the **Live video** button on the camera image (and a button to open it in a new tab); under **Printers** the **Camera** link.
- `/printers/<id>/camera.mjpg`: live image as MJPEG (± 5 fps, max. 1280 wide; adjustable with `CAMERA_FPS` and `CAMERA_WIDTH`). ffmpeg in the container converts the stream. There is one connection per printer, shared by all viewers. It stops 15 seconds after the last viewer leaves; a hidden tab also stops the stream.
- `/printers/<id>/snapshot.jpg`: a single image in full resolution. The image is kept ~20 s per printer (a failed attempt too), so several tabs do not put extra load on the printer; when the live stream is running, the image comes from there.
- On the home page, the card of a print with status *Printing* without an own cover (a thumbnail does not count; it is the fallback when there is no camera image) shows the camera image (label *live*), refreshed every 30 s. When the listener reports that the print is no longer running, one last image follows (label *end*); after a reload the card shows the end photo the listener attached to the print.
- When a print goes live to *Succeeded* or *Failed*, the listener attaches a camera photo (`camera-succeeded-…jpg` / `camera-failed-…jpg`). A cover you chose or an own photo added earlier stays the cover; a slicer/MakerWorld thumbnail is replaced by the camera photo (the thumbnail stays among the photos). If the image fails, that is logged and everything else carries on. An end the listener only notices after a restart gets no photo.
- The access code never ends up in logs or error messages.

## Live status and time series

- The live block at the top of the home page shows what the printer screen shows: the stage as readable text (`stg_cur`), plate x of y, %, layer, time left and finish time, nozzle/bed/chamber temperature (current/target; the P2S has no chamber target), speed profile, fans (part, aux, chamber, heatbreak) and the active AMS slot with its colour. The large image is the camera image (when the print has no own cover), otherwise the cover or the printer photo; the last two finished prints are next to it. A printer that is not printing gets a compact bar with its temperatures and AMS.
- Fields that are missing (other model, older firmware) simply drop out. The translation of the raw fields is in `bambu_status.py`.
- At the start of a print the listener fills in the print record: nozzle diameter, nozzle type (e.g. *Hardened steel (HS01)*), plate (`device.plate.cur_id`, e.g. `P0101`; the source has no readable name for it), plate number and number of plates, speed profile, and whether spaghetti detection and first layer inspection were on. Only fields that are still empty; what you filled in yourself (or what came from a 3mf) stays. If the full report only arrives later, the empty fields are filled in then.
- Time series: per print at most one data point per minute (temperatures, stage, %, layer) in `print_samples`, plus one at every stage change and at the end. Shown on the detail page as a chart with a stage band, crosshair and table.
- Filter on nozzle diameter in the overview.
- Full MQTT report: `data/mqtt-report-p2s.json` (access code filtered out; not in git).

## Error codes

The texts (English, P2S variants where they exist), wiki links and stage texts (`CURRENT_STAGE_IDS` from `pybambu/const.py` with its translations, read with `ast.literal_eval`, never executed) are in `bambu_errors_data.json`. Own stage texts in `bambu_status.STAGE_TEXT` take precedence. Source: the error code lists of [ha-bambulab](https://github.com/greghesp/ha-bambulab) (`scripts/hms_error_text/`, MIT), which come from Bambu Lab itself (`e.bambulab.com`, like Bambu Studio) and from the [HMS wiki](https://wiki.bambulab.com/en/hms/home). The commit used is recorded in `_source` in the file.

```bash
python3 scripts/update_bambu_errors.py             # regenerate the table (latest main)
python3 scripts/update_bambu_errors.py --ref <sha> # or a fixed version
```

Then rebuild (`docker compose up -d --build`). Own texts and the suggested fail reason per code are in `bambu_errors.py` (`PRINT_ERRORS_EXTRA`, `HMS_CODES_EXTRA`, `FAIL_REASONS_BY_CODE`) and are not overwritten by the script. Only HMS codes have their own wiki page; for print_error codes and HMS codes without a known page, Printvault links to the HMS home page.

## Maintenance and notifications

- **Printers → Maintenance**: tasks with an interval in print hours and/or days; a task becomes due at whichever is reached first. Print hours = the print time of the prints that finished after *last done* (planned and running prints do not count). On the first start with this version, existing printers get a set of default tasks for the P2S.
- **Settings**: ntfy server, topic, optional access token, and on/off per kind of notification. *Send test notification* checks the settings.
- The listener (`printvault-mqtt`) sends the notifications. Maintenance is checked every minute; a task that becomes due sends one notification, and again only after it has been done and becomes due again.
- No notification for an end the listener only notices after a restart.

## Running

```bash
cp .env.example .env              # fill in SECRET_KEY
mkdir -p data
docker compose up -d --build
```

The app then runs at http://<vm-ip>:8100. It is also reachable via Tailscale.

## Design

Plain HTML, CSS and JavaScript: no framework, no build step, no requests to other sites.

- `static/style.css`: the whole look (dark theme, colours as variables at the top). `static/app.js`: shared helpers and the printer status in the sidebar. `static/live.js`: live status on the home and detail page. `static/chart.js`: the temperature chart.
- Icons: own inline SVG in `icons.py`, included once per page as a sprite (`{{ icon("clock") }}` in templates, `PV.icon("clock")` in JavaScript).
- Fonts: Inter and JetBrains Mono (numbers), self-hosted in `static/fonts/` (SIL OFL 1.1, licences next to them).
- Accessibility: a status is always an icon plus a word, never colour alone; visible focus; text contrast at least 4.5:1 and form field borders at least 3:1 on their background; the chart colours are checked for colour blindness and come with direct labels and a table.
- On a phone (≤ 760 px) the sidebar becomes a bar at the bottom and the overview shows two columns.

## Photos and thumbnails

Every photo gets two smaller WebP versions in `data/uploads/thumbs/`: 600 px wide for the cards and 1600 px for the detail page. They are made with ffmpeg in the background when a photo comes in (upload, 3MF/G-code thumbnail, MakerWorld cover, printer photo, camera photo of the listener) and once at start-up for photos that do not have them yet.

- Only scaled down, never up. The EXIF orientation of phone photos is applied.
- No metadata in the thumbnails (no EXIF, GPS or XMP). The originals are stored unchanged and stay downloadable.
- Of an animated GIF the thumbnail is the first frame; the animation is only shown on the detail page.
- `/thumb/<600|1600>/<file>` serves the original as long as the thumbnail does not exist (yet), so a failed conversion (logged) never breaks a page.
- Deleting a photo or a print also deletes its thumbnails. The folder can be deleted at any time; it is rebuilt at the next start.

## Continuous integration

Every push and pull request runs the tests: `.forgejo/workflows/tests.yml` (Forgejo Actions, runner label `docker`) and `.github/workflows/tests.yml` (GitHub Actions) with the same steps. Both run in a `python:3.12-slim` container (the Python of the Dockerfile), install ffmpeg and the requirements, and run `python -m unittest discover -s tests` with `REQUIRE_FFMPEG=1`.

## Data

Everything is in `./data`: the SQLite database `printvault.db` and the uploads in `uploads/`. Back up that folder and you have everything.

## Local development

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python app.py                     # http://127.0.0.1:8100
python mqtt_listener.py           # second terminal; live status at http://127.0.0.1:8101/live
python -m unittest discover -s tests
```

The thumbnail tests need ffmpeg and are skipped without it (with `REQUIRE_FFMPEG=1` they fail instead, as in CI). To run everything in the image (with ffmpeg, no network):

```bash
docker run --rm --network none -u "$(id -u):$(id -g)" -e PYTHONDONTWRITEBYTECODE=1 -e DATA_DIR=/tmp/pvtest \
  -v "$PWD":/src:ro -w /src --entrypoint python printvault:local -m unittest discover -s tests
```
