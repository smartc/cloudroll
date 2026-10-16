# Cloud-cover calibration

The SkyConditions sensor turns its 32×24 IR frame into a cloud-cover percentage using two
thresholds on Δ = ambient − sky temperature: **Clear Δ** (at or above this, 0 %) and
**Overcast Δ** (at or below this, 100 %). The right values depend on the site and the
season, so CloudRoll keeps a long-running dataset to check them against a ground truth:
the allsky camera's **star count**.

## Why star counts

indi-allsky counts the stars it detects in each image. On a moonless night a clear sky
shows ~1000+ stars here and an overcast sky almost none, so the count is a good
independent measure of how clear the sky really was. It is not perfect: moonlight,
twilight, dew or frost on the dome, smoke and aurora all lower it. The dataset records
what is needed to filter those out (moon altitude and phase, sun altitude, exposure,
gain, moon mode, dew heater level, smoke rating).

The first calibration (October 2026, one week) found clear skies at Δ 25–32 °C. The
sensor was set to Clear Δ 30, which read clear skies as ~21 % cloud on median and ≥ 20 %
for more than half of all clear minutes. It was changed to **Clear Δ 26 / Overcast Δ 16**.
Clear-sky Δ also moved with ambient temperature (and humidity), so fixed thresholds will
need re-checking as winter brings colder nights. Firmware ideas are filed as
[SkyConditions_90640#9](https://github.com/smartc/SkyConditions_90640/issues/9).

## What CloudRoll records

**With every capture** (in `data/readings/<date>.jsonl`):

| Field | Contents |
|-------|----------|
| `allsky` | the configured `allsky_stats` entities from Home Assistant, plus `age_sec` (time since each value last *changed*) and `image_age_sec` (age of the allsky image, from the `image_time` entity) |
| `astro` | `sun_alt`, `moon_alt` (degrees) and `moon_illum` (%), computed locally |
| `sensor_cloud` | the sensor's cloud settings: method, region, edge exclude, Clear Δ, Overcast Δ (read from its `/setup` page along with the colour scale, at most every 10 min) |

A star count is only trusted while its allsky image is less than `allsky_stats.max_age_sec`
old. HA re-stamps an entity only when its value changes, so a count stuck at 0 under thick
cloud would look stale. That is why the image time is the freshness signal, not the
entity's own timestamp.

**Each night, after the window closes** (the same housekeeping pass that builds the
timelapse), CloudRoll harvests the night into `data/calibration/`:

```
data/calibration/
  nights/<night>.jsonl.gz       standalone night file (see below)
  nights/<night>.summary.json   compact per-minute columns for the web page (rebuilt if deleted)
  images/<night>/<stamp>.jpg    ~12 sampled allsky images, 1024 px wide
  labels.json                   your labels for those images
  ha-history-backfill-*.json.gz raw HA history saved before it expired (one-off)
```

Unharvested nights from the last 9 days are retried on every pass, so a Home Assistant
outage just delays the harvest. Nights older than HA's history can still be harvested
from a saved export: `python -m cloudroll calibrate --history FILE`.

### Night file format (`format: 1`)

One JSON object per line, gzipped. Line 1 is the **header**:

| Key | Contents |
|-----|----------|
| `night`, `window` | the window date and its start/end |
| `night_events` | sunset, astronomical dusk and dawn, sunrise |
| `location` | name, latitude, longitude, time zone |
| `sun_below_deg` | which minutes count as dark (default −18°) |
| `allsky_entities`, `extra_entities` | name → HA entity map used |
| `allsky_max_age_sec`, `clear_stars` | the freshness limit, and the star count treated as a fully clear sky |
| `history_source` | where allsky values came from when not recorded live: `homeassistant`, `file`, or `none` |
| `minutes`, `with_stars`, `with_pixels` | counts |
| `images` | sampled images: stamp, file, reason, time, stars, reported cloud, moon altitude |
| `notes` | anything that went wrong or is missing |
| `created`, `cloudroll_version`, `format` | provenance |

Every following line is one **dark minute**:

| Key | Contents |
|-----|----------|
| `time`, `frame` | local ISO time; frame stamp (matches the allsky image name) |
| `ambient`, `sky`, `pixels` | the sensor's ambient (°C), centre sky mean, and the raw 32×24 frame (°C, rows top to bottom) |
| `readings` | the sensor's reported values, including `cloud_cover` and `humidity` |
| `color_scale`, `sensor_cloud` | the sensor's settings in effect (`sensor_cloud` from Oct 16 2026 on) |
| `astro` | sun and moon position |
| `allsky`, `allsky_from` | allsky statistics, `live` (recorded at capture) or `history` (rebuilt from HA history: as-of values at that minute) |
| `allsky_image` | the allsky source's fetch status for that capture |
| `extra` | values of `calibration.history_entities`, if configured |

To replay the sensor's per-pixel cloud cover from a minute:
`mean(clip(1 − ((ambient − pixel) − overcast) / (clear − overcast), 0, 1)) × 100` over rows
`edge … 23−edge`, columns `edge … 31−edge` (full-frame region) or rows 6–17, columns 8–23
(centre region).

## Image sample

Each night keeps `calibration.images_per_night` (12) allsky images, downscaled. A purely
random sample would be mostly plain clear or plain overcast sky, which teaches little, so
it is split three ways:

* **Disagreement** – the minutes where the sensor's cloud % and the star count's estimate
  (1 − stars / `clear_stars`) differ most: at least 25 points apart, at least 20 min apart
  from each other, moon down.
* **Band** – one from each star-count band (< 50, 50–300, 300–800, ≥ 800) in turn, so
  partly cloudy skies are represented.
* **Random** – the rest, which also fills in when the other two run short.

The choice is seeded by the date, so re-harvesting picks the same images, and any image
you have labelled is always kept.

## The Calibration page

`/calibration` (nav: **Calibration**) works over the last 7/30/90 or all nights:

* **Tiles** – dark, clear and overcast minutes, the sensor's current thresholds, and how
  often clear minutes read as cloudy.
* **What if…** – replays the per-pixel cloud cover for any Clear/Overcast pair from per-minute
  Δ quantiles (within ~0.4 % of an exact replay), by star-count band, with false-cloud
  and missed-cloud rates. **Best fit** grid-searches the pair that best matches the star
  estimate. Treat it as a starting point, not an answer.
* **Scatter** – Δ against ambient (or dew point), coloured by star count, with the
  sensor's and the what-if thresholds and a straight-line fit through the clear minutes.
  If the clear dots drift away from the Clear Δ line, it is time to re-tune.
* **By month** – clear-sky Δ and the false-cloud rate per month; ⚠ marks a month whose
  clear nights were colder than any before it.
* **Image review** – the sampled allsky images beside the IR image of the same minute.
  Label each one (clear, thin, partly, overcast, unusable) and add a note. Labels save at
  once to `labels.json` and are summarised against the sensor's cloud %.

## Recalculated cloud cover

Fixed thresholds can't serve both a cold night and a warm afternoon. On 2026-10-16 (21 °C,
RH 19 %) an overcast sky had Δ 27.5, which reads as clear with Clear Δ 26, while clear
afternoons at 20 °C have Δ 37–40. A fit over clear nights and clear afternoons (ambient
−3 … 22 °C) gives clear-sky Δ ≈ 26.8 + 0.58 × ambient.

With `cloud_recalc.enabled`, the Data page's Cloud Cover chart and record panel also show a
**Recalculated** cloud cover, and the CSV gets a `cloud_cover_recalc` column. It is the
sensor's per-pixel algorithm, replayed from each stored frame with both thresholds shifted by
`amb_slope × (ambient − amb_ref)`: the same correction proposed for the firmware in
[SkyConditions_90640#9](https://github.com/smartc/SkyConditions_90640/issues/9). With
`amb_slope` 0 it reproduces the sensor's own reading (to within ~0.1 %). Stored readings are
never changed; the line is computed when the page loads, so it covers every day with
frames and follows any change to the settings.

The Calibration page's what-if takes the same **ambient slope** and reference. The
**Recalculated model** button loads the `cloud_recalc` settings, so tunings can be compared
before they go into the firmware.

```json
"cloud_recalc": {"enabled": true, "label": "Recalculated", "clear_delta": 26, "overcast_delta": 16,
                 "amb_slope": 0.58, "amb_ref": 0, "method": "pixel", "region": "full", "edge_exclude": 3}
```

Limits: thin high cloud is partly transparent in the IR and still reads low, and the BME280
reads several °C high in morning sun (vs the weather station), which makes daytime skies
look clearer than they are.

## Revisiting the calibration

1. Once a month or so, or when the By month table shows ⚠, open **Calibration** with the
   last 30 nights and *Moon down only*.
2. Label the unlabelled images (a few minutes).
3. Compare the clear and overcast rows for the current settings and a few what-if pairs.
   Check the labelled images agree.
4. If a change is worth it, set it on the sensor's own `/setup` page. **Save the whole
   form:** its save handler clears every checkbox that is missing from the request
   (MQTT, rain sensor, safety broadcast).
5. The night files record which thresholds were in effect, so before/after comparisons
   stay possible.

## Config

```json
"allsky_stats": {
  "entities": {
    "stars": "sensor.allsky_stars",
    "sqm": "sensor.allsky_sqm",
    "exposure": "sensor.allsky_exposure",
    "gain": "sensor.allsky_camera_gain",
    "moon_mode": "binary_sensor.allsky_moon_mode",
    "night": "binary_sensor.allsky_night",
    "dew_heater": "sensor.allsky_dew_heater_level",
    "smoke": "sensor.allsky_smoke_rating",
    "image_time": "sensor.allsky_exposure_date"
  },
  "max_age_sec": 300
},
"calibration": {
  "enabled": true,
  "sun_below_deg": -18,
  "images_per_night": 12,
  "image_width": 1024,
  "clear_stars": 1000,
  "history_entities": {}
}
```

`allsky_stats` names on the left are free-form; `stars` drives the charts and calibration,
and `image_time` (indi-allsky's exposure date) dates the allsky image. `calibration.allsky_source`
picks which image source to sample (default: `observatory.allsky_source`).
`history_entities` adds other HA entities to each night file (e.g. a weather station's
temperature).

Storage: about 0.8 MB per night for the night file, 0.15 MB for its summary and 0.5–1 MB of
images. That's roughly 0.5–0.7 GB a year.
