# CloudRoll

Captures the IR sky image and conditions from the
[SkyConditions_90640](https://github.com/smartc/SkyConditions_90640) sensor, renders a
timelapse frame (thermal image + colour scale + conditions panel), builds one MP4 per
day, and serves a gallery web page.

A day's window runs from **sunrise + 1 h** to **sunrise + 1 h on the following day**,
labelled by the date it starts on.

Once a window closes (checked every 10 min), the day is finalised: its frames are encoded
into one MP4 (~16 s of CPU on this board), its readings file is gzipped, and its frame
JPEGs are deleted. Only the video and the numeric data are kept.

## Data layout

```
data/latest.jpg, latest.json          most recent capture
data/frames/<date>/*.jpg              current day's frames (removed once its timelapse is built)
data/readings/<date>.jsonl[.gz]       one record per capture: readings, colour scale, raw 32×24 °C matrix
data/timelapses/<date>.mp4/.jpg/.json video, poster, metadata
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

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # then set sensor.host and location
.venv/bin/python -m cloudroll info      # check location / windows / sensor
.venv/bin/python -m cloudroll run       # capture + web UI on port 8090
```

Other commands: `capture` (one frame), `build [YYYY-MM-DD]` (rebuild a day's timelapse).

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
| `retention.frames_days` | Safety net: delete frame folders older than N days even if no timelapse was built |
| `retention.readings_days` | Delete readings files older than N days (0 = keep forever) |
| `retention.timelapses_days` | Delete timelapses older than N days (0 = keep forever) |
| `web.host` / `web.port` | Listen address – `127.0.0.1:8090`, reached via the Apache proxy (`0.0.0.0` exposes it directly to the LAN) |

## Data sources

| Value | Source |
|-------|--------|
| Thermal image | `GET /thermalmatrix`, coloured with the sensor's palette (`heatmapColor()`) |
| Cloud cover, ambient, sky temp, brightness, SQM, humidity | Alpaca `observingconditions/0/*` |
| Rain | Alpaca `safetymonitor/0/issafe` (shows `--` if the rain sensor is disabled) |
| Pressure | Sensor WebSocket state message (not exposed via Alpaca) |

`SkyConditions_90640/` is a read-only reference clone of the sensor repo and is not part of this project.

## License

MIT – see [LICENSE](LICENSE).
