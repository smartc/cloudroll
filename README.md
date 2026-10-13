# CloudRoll

Captures the IR sky image and conditions from the
[SkyConditions_90640](https://github.com/smartc/SkyConditions_90640) sensor, renders a
timelapse frame (thermal image + colour scale + conditions panel), builds one MP4 per
day, and serves a gallery web page.

Optionally, other cameras – an allsky camera, an observatory webcam, anything reachable
through Home Assistant, a URL or RTSP – can be added to every frame. See
[Image sources](#image-sources).

A day's window runs from **sunrise + 1 h** to **sunrise + 1 h on the following day**,
labelled by the date it starts on.

Once a window closes (checked every 10 min), the day is finalised: its frames are encoded
into one MP4 (~16 s of CPU on this board for classic frames, about a minute per layout
with image sources) and its readings file is gzipped. Frame JPEGs, and the original source
images, are kept for `retention.frames_days` (7 by default) so recent days can be browsed
frame by frame or re-encoded in another layout; the video and numeric data are kept
long-term.

## Data layout

```
data/latest.jpg, latest.json          most recent capture
data/latest-<layout>.jpg              most recent capture in each layout (image sources only)
data/frames/<date>/*.jpg              frames, classic layout (kept retention.frames_days)
data/frames/<date>/<layout>/*.jpg     frames, one folder per layout (image sources only)
data/sources/<date>/<source>/*.jpg    each source's original image as fetched
data/readings/<date>.jsonl[.gz]       one record per capture: readings, colour scale, raw 32×24 °C matrix
data/timelapses/<date>.mp4/.jpg/.json main video, poster, metadata
data/timelapses/<date>.<layout>.mp4   further layouts of the same day, with their posters
data/settings.json                    runtime settings changed from the web UI
```

Each readings record looks like
`{"time", "frame", "window", "readings": {...}, "color_scale": {...}, "ambient", "sky", "pixels": [[...32]×24]}`
(about 5 KB raw, ~1.7 KB gzipped, so roughly 2.5 MB/day once compressed).

The **Data** page lists stored days by month with summary stats and CSV / raw downloads.
Each day's view charts every reading over the window (hover for values, click to select)
and shows the selected record's values plus its temperature grid, re-rendered with the
colour scale that was in effect at capture.

| Endpoint | Returns |
|----------|---------|
| `/api/readings/<date>.csv` | scalar readings, one row per capture |
| `/api/readings/<date>.csv?grid=1` | same plus `frame_ambient`, `frame_sky` and the grid as columns `r00c00`…`r23c31` |
| `/api/readings/<date>.json` | column-oriented scalar series (for charts) |
| `/api/readings/<date>/<n>.json` | full record *n* (0-based), including `pixels` |
| `/api/readings/<date>/<n>.csv` | record *n*'s grid as a 24 × 32 CSV (°C) |
| `/api/readings/<date>/<n>.png` | record *n*'s grid rendered with its colour bar |
| `/media/readings/<date>.jsonl[.gz]` | the raw day file |
| `/media/sources/<date>/<source>/<stamp>.jpg` | a source's original image (while frames are kept) |
| `/api/settings` | GET / POST the runtime settings (see below) |

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # then set sensor.host and location
.venv/bin/python -m cloudroll info      # check location / windows / sensor
.venv/bin/python -m cloudroll run       # capture + web UI on port 8090
```

Other commands: `capture` (one frame), `build [YYYY-MM-DD] [--layout NAME|both]` (rebuild a
day's timelapse; while its frames are kept, in any layout). `info` also test-fetches each
image source.

To run at boot behind an Apache HTTPS proxy:

```sh
sudo sh deploy/install.sh cloudroll.example.com /etc/letsencrypt/live/example.com
```

This installs a systemd service (running as you, from this checkout) and an Apache vhost
for the given hostname and certificate directory; it needs `mod_ssl`, `mod_proxy_http`
and `mod_headers`. Logs: `journalctl -u cloudroll -f`.

## config.json

Copied from `config.example.json`; it holds your site's details and is not tracked by git.

| Key | Meaning |
|-----|---------|
| `sensor.host` | Sensor hostname or IP; ports for web (80), Alpaca (11111), WebSocket (81) |
| `location.name` | Place name, geocoded once via OpenStreetMap and cached in `data/location_cache.json` |
| `location.latitude` / `longitude` | Explicit coordinates – take precedence over `name` when both set |
| `location.timezone` | IANA zone for display and day boundaries |
| `capture.interval_sec` | Seconds between frames (aligned to the clock) |
| `color_scale.mode` | `sensor` = read the sensor's own heatmap settings (matches its live view); `fixed` = `fixed_min`..`fixed_max` °C; `floating` = ambient − `clear_delta` .. ambient − `overcast_delta` |
| `color_scale.hot_marker` | Show magenta for pixels above the scale (fixed/floating modes) |
| `timelapse.fps` / `crf` | Playback rate and x264 quality |
| `timelapse.offset_after_sunrise_min` | Window start offset after sunrise (default 60) |
| `retention.frames_days` | Keep frames and source images for N days, whether or not the timelapse was built |
| `retention.readings_days` | Delete readings files older than N days (0 = keep forever) |
| `retention.timelapses_days` | Delete timelapses older than N days (0 = keep forever) |
| `web.cors_origins` | Other sites allowed to read `/api/` from a browser, e.g. `["https://home-assist.example"]` for the Home Assistant card |
| `web.host` / `web.port` | Listen address – `127.0.0.1:8090`, reached via the Apache proxy (`0.0.0.0` exposes it directly to the LAN) |
| `homeassistant.url` / `token_file` / `token` | Home Assistant base URL and a long-lived access token (file preferred, e.g. `~/.config/cloudroll/ha_token`, mode 600) |
| `sources` | Extra images for each frame – see below (empty = classic frames) |
| `layouts` | Source names that each get a layout with themselves as the large image (default: all sources, in order) |
| `source_timeout_sec` | Per-fetch timeout for image sources |
| `observatory` | Optional roof and telescope status, and pointing markers on the allsky image – see below |

## Image sources

Each entry in `sources` adds one image to every frame:

| Key | Meaning |
|-----|---------|
| `name` | Short id (`a-z0-9_-`), used in folder names and URLs |
| `label` | Caption shown in the frame and the web UI |
| `type` | `homeassistant` (`entity`: `image.*` via `/api/image_proxy`, `camera.*` via `/api/camera_proxy`), `url` (`url`: any JPEG/PNG), or `rtsp` (`url`: one frame grabbed with ffmpeg) |
| `max_age_sec` | Optional: tag the image STALE when it is older than this (HA `image.*` entity time or the URL's `Last-Modified`) |
| `crop` | Optional `[left, top, right, bottom]` as fractions, e.g. to trim an allsky's corners |
| `aspect` | Optional width/height used for the placeholder until the first image arrives |
| `link` | Optional URL for a "Label ↗" button in the page header, e.g. the camera's own web UI or its Home Assistant view |

Sources are fetched in parallel with the sensor reads. A failed fetch draws a
"no image" placeholder; the capture still happens and the record's `sources` field
says what went wrong. Thermal sensor failures still skip the capture, as before.

### Layouts

With sources configured, frames are 1920×1080 and every capture is rendered once per
layout – one per source named in `layouts`, with that source large:

* a squarish source (an allsky) fills the left side, with the others, the IR sky image
  and the conditions to its right;
* a wide source (a 16:9 camera) sits top-left above the conditions grid, with the IR
  sky image and the others in a column on the right.

### Runtime settings

The Live page has a layout switch (remembered per browser) and a **Settings**
card, saved to `data/settings.json`:

| Setting | Meaning |
|---------|---------|
| Dashboard default | Large image for browsers that haven't picked one |
| Timelapse videos | Layouts encoded when a day is built; ticking several builds one MP4 each, switchable on the timelapse page |
| Telescope markers | `ring` (2° circle), `fov` (the camera's field of view), `both` or `off`; applies from the next capture (only shown with `observatory.rigs`) |

Like **Build now**, these controls have no login; if the site is reachable from outside
your network, restrict `/api/` in the Apache vhost.

## Observatory status and telescope markers

With an `observatory` block, every capture also reads the roll-off roof, each
telescope's park sensor and its mount position, and:

* adds a status line to the allsky panel's caption (`ROOF OPEN · PIER 1 UNPARKED · RIG 1 CAPTURING ·
  PIER 2 PARKED · RIG 2 PARKED`) and to the Live page;
* draws a marker on the allsky image where each active telescope points (a 2° ring,
  the camera's field of view, or both – see Runtime settings);
* stores the roof state and each rig's status, RA/Dec, alt/az and rotator angle in the
  record's `observatory` field.

The archived allsky originals stay unmarked; only the composite frames carry markers.

**Read-only.** CloudRoll only sends HTTP `GET`s to an allow-list of status
endpoints (below). It never sends anything that could move a mount, rotator or roof
– not even an Alpaca `PUT connected`.

| Device | Endpoint read |
|--------|---------------|
| Roof | ASCOM Alpaca Dome `…/dome/N/shutterstatus` (0 open, 1 closed, 2 opening, 3 closing, 4 error) |
| Park sensor | ASCOM Alpaca Switch `…/switch/N/getswitch?Id=n` (e.g. a Dark Dragon Mount Park Sensor) |
| Mount | [NINA Advanced API](https://github.com/christian-photo/ninaAPI) `GET /equipment/mount/info` (RA/Dec of date) |
| Rotator | NINA Advanced API `GET /equipment/rotator/info` (`Position`, the sky angle) |
| Camera, guider, focuser | NINA Advanced API `GET /equipment/{camera,guider,focuser}/info` (`IsExposing`, `State`, `IsMoving`) |
| Sequence | NINA Advanced API `GET /sequence/json` (which item is running) |

NINA's API also performs actions with `GET` (`sequence/start`, `mount/slew`, …), so
the allow-list names exact read-only paths.

Each pier (park sensor) and rig (NINA) has its own status:

| Pier | Meaning |
|------|---------|
| `offline` | park sensor unreachable – usually powered down |
| `parked` / `unparked` | as the sensor reports |

| Rig (first match) | Meaning |
|-----|---------|
| `offline` | NINA's API unreachable |
| `slewing` | mount slewing (goto, centering, meridian flip) |
| `focusing` | an autofocus sequence item is running, or the focuser is moving |
| `calibrating` | guider calibrating |
| `capturing` | mount tracking and the camera exposing (plate-solve frames included) |
| `tracking` | mount tracking between exposures (download, dither, settle) |
| `waiting` | a sequence is running but the mount isn't tracking (e.g. "Wait for Time") |
| `parked` / `idle` / `disconnected` | no sequence running; mount parked, unparked but not tracking, or not connected |

The Live page adds the running sequence item and the guider state, e.g.
`Rig 1: capturing (Take Exposure · guiding)`. Status is sampled once per capture.

The Data page shows an **Observatory** strip (one lane each for the roof, every pier
and every rig, coloured by state) and the same states in the record inspector and the
table view. The day's **Download CSV** adds `roof` and, per rig, `<rig>_pier`,
`<rig>_status`, `<rig>_detail`, `<rig>_ra_h`, `<rig>_dec_deg` (of date), `<rig>_alt_deg`,
`<rig>_az_deg`, `<rig>_pa_deg` and `<rig>_marker` (1 if drawn); the raw and record JSON
downloads carry the full `observatory` field.

A rig gets a marker when its pier is `unparked` (or it has no park sensor), the roof is
`open` and NINA reports the mount's position above the horizon. Without a `roof`, the
roof is `unknown` and no markers are drawn.

```json
"observatory": {
  "roof": {"url": "http://roof.example.com:11111/api/v1/dome/0", "link": "http://roof.example.com/"},
  "allsky_source": "allsky",
  "rigs": [
    {
      "name": "rig1",
      "label": "Rig 1",
      "nina": "http://rig1.example.com:1888/v2/api",
      "park_sensor": {"url": "http://mps1.example.com/api/v1/switch/0", "id": 0, "parked_value": true,
                      "label": "Pier 1"},
      "focal_length_mm": 1000,
      "sensor": {"width_px": 6248, "height_px": 4176, "pixel_um": 3.76},
      "color": "#ffd200",
      "rotation_offset_deg": 0,
      "flip": false
    }
  ],
  "lens": {"width": 3552, "height": 3552, "cx": 1776, "cy": 1776, "rot_deg": 0,
           "k1": 1268.5, "k2": -67.5, "k3": 0.44},
  "timeout_sec": 3
}
```

| Key | Meaning |
|-----|---------|
| `roof.url` | Alpaca Dome device base URL |
| `roof.link`, `roof.label` | Optional: a "Label ↗" button in the page header for the roof controller's own web page (label defaults to "Roof") |
| `allsky_source` | Name of the source the markers are drawn on |
| `rigs[].nina` | NINA Advanced API base (`http://host:1888/v2/api`); the plugin must be enabled and port 1888 reachable |
| `rigs[].park_sensor` | Alpaca Switch base URL, switch `id`, the `getswitch` value that means parked, and the pier's `label` |
| `rigs[].focal_length_mm`, `sensor` | Field of view (defaults: 1000 mm, ASI2600 6248×4176 at 3.76 µm) |
| `rigs[].color` | Marker colour |
| `rigs[].rotation_offset_deg`, `flip` | Match the rotator angle to the sky: the sensor's long side is drawn perpendicular to `Position + offset` (east of north), mirrored with `flip` |
| `lens` | Fisheye model of the allsky camera: centre `cx`/`cy`, north rotation `rot_deg`, radius r = k1·z + k2·z³ + k3·z⁵ (pixels, zenith angle z in radians) at the original image's `width`×`height`; `lat`/`lon` override the location for the RA/Dec → alt/az conversion. Without a lens, only the status is shown |
| `timeout_sec` | Per-request timeout; an unreachable device only costs this once per capture |

`python -m cloudroll info` prints the current roof and rig status – a quick check
that every device answers.

## Home Assistant card

`cloudroll/static/ha/cloudroll-card.js` puts the Data page (day charts, record
inspector with the matching camera and IR images, table view) into a Lovelace
dashboard, with a "Now" row of live readings from Home Assistant entities.

1. Add the Home Assistant origin to `web.cors_origins` in config.json and restart.
2. Settings → Dashboards → ⋮ → Resources: add
   `https://<cloudroll host>/static/ha/cloudroll-card.js?v=<n>` as a JavaScript module
   (bump `v` after updating the card).
3. Add the card, ideally in a panel view:

```yaml
type: custom:cloudroll-card
url: https://<cloudroll host>
entities:                 # optional; readings without one use CloudRoll's latest record
  cloud_cover: sensor.observatory_sky_conditions_cloud_cover
  ambient_temp: sensor.observatory_sky_conditions_ambient_temperature
  sky_temp: sensor.observatory_sky_conditions_sky_temperature
  brightness: sensor.observatory_sky_conditions_sky_brightness
  sqm: sensor.observatory_sky_conditions_sky_quality_sqm
  raining: binary_sensor.observatory_sky_conditions_rain_snow
```

The card reads `/api/info` (days, cameras, time zone) and the same
`/api/readings/...` endpoints as the Data page.

## Data sources

| Value | Source |
|-------|--------|
| Thermal image | `GET /thermalmatrix`, coloured with the sensor's palette (`heatmapColor()`) |
| Cloud cover, ambient, sky temp, brightness, SQM, humidity | Alpaca `observingconditions/0/*` |
| Rain | Alpaca `safetymonitor/0/issafe` (shows `--` if the rain sensor is disabled) |
| Pressure | Sensor WebSocket state message (not exposed via Alpaca) |

`SkyConditions_90640/` is a read-only reference clone of the sensor repo and is not part of this project.

## Roadmap

* **Lens calibration tool** – fit the allsky `lens` model from a clear-night frame by
  matching stars to a catalogue (currently a separate script).
* **Telescope markers on the Home Assistant card** – draw them from the records'
  `observatory` field over the original allsky images.
* **Web setup page** – configure the observatory from the browser instead of
  config.json: NINA instances, piers / park sensors and the roof, with a read-only
  "test connection" for each.
* **Any number of rigs in the setup** – add and remove rigs freely (the config already
  takes a list; markers cycle 4 default colours and spread up to 4 overlapping labels).
* **Interlock warning** – highlight the roof moving while any pier reads unparked,
  which the roof interlock should never allow.
* **Park sensor dropouts** – hold a pier's last state for a capture or two before
  showing it offline, so a sensor reboot doesn't hide a marker.

## License

MIT – see [LICENSE](LICENSE).
