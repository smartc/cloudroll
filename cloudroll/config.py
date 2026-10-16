"""Configuration loading and location resolution.

config.json lives in the project root.  The location may be given either as a
place name (geocoded once via OpenStreetMap Nominatim and cached) or as an
explicit latitude/longitude pair, which takes precedence when both are set.
"""

import copy
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULTS = {
    "sensor": {
        "host": "skyconditions.local",
        "http_port": 80,
        "alpaca_port": 11111,
        "websocket_port": 81,
        "timeout_sec": 8,
    },
    "location": {
        "name": "",
        "latitude": None,
        "longitude": None,
        "timezone": "UTC",
    },
    "capture": {"interval_sec": 60, "jpeg_quality": 88},
    "color_scale": {
        "mode": "sensor",          # "sensor" | "fixed" | "floating"
        "fixed_min": -45.0,
        "fixed_max": 60.0,
        "clear_delta": 20.0,
        "overcast_delta": 5.0,
        "hot_marker": True,
    },
    # run: also cut the night's imaging run (roof open → closed) out of the day video.
    "timelapse": {"fps": 24, "crf": 23, "offset_after_sunrise_min": 60,
                  "run": {"enabled": True, "pad_min": 5}},
    "retention": {"frames_days": 7, "readings_days": 0, "timelapses_days": 0},
    # cors_origins: other sites (e.g. a Home Assistant dashboard card) allowed to
    # read the /api/ JSON from a browser.
    "web": {"host": "0.0.0.0", "port": 8090, "title": "CloudRoll", "cors_origins": []},
    "data_dir": "data",
    # Optional extra images (allsky, observatory cams, …) composited into each frame.
    # With none configured, frames are the classic thermal + conditions layout.
    "homeassistant": {"url": "", "token": "", "token_file": "", "verify_ssl": True},
    "sources": [],
    # Source names that get a layout with themselves as the large image; every
    # capture renders all of them.  Defaults to every source, in config order.
    "layouts": None,
    "source_timeout_sec": 15,
    # Optional allsky statistics (star count, SQM, …) read from Home Assistant
    # entities and stored with each capture.  See cloudroll/skystats.py.
    "allsky_stats": {"entities": {}, "max_age_sec": 300},
    # A second, recalculated cloud cover from the stored frames (Data page,
    # CSV): the sensor's per-pixel algorithm with ambient-compensated thresholds.
    # See cloudroll/cloudmodel.py.
    "cloud_recalc": {"enabled": False, "label": "Recalculated", "clear_delta": 26.0,
                     "overcast_delta": 16.0, "amb_slope": 0.58, "amb_ref": 0.0,
                     "method": "pixel", "region": "full", "edge_exclude": 3},
    # Nightly calibration harvest: a standalone dataset of dark minutes (frames,
    # sensor settings, star counts, sun/moon) plus a few sampled allsky images.
    # See CALIBRATION.md.
    "calibration": {
        "enabled": True,
        "sun_below_deg": -18,       # a minute is "dark" when the sun is below this
        "images_per_night": 12,     # sampled allsky images kept for review
        "image_width": 1024,        # sampled images are downscaled to this width
        "clear_stars": 1000,        # star count treated as a fully clear sky
        "history_entities": {},     # extra HA entities to harvest each night
    },
    # Optional roof / park sensor status and telescope markers on the allsky image.
    # Read-only: CloudRoll only ever GETs these devices.  See README "Observatory".
    "observatory": {
        "roof": None,               # {"url": ".../api/v1/dome/0"} (ASCOM Alpaca Dome)
        "rigs": [],                 # [{name, label, nina, park_sensor, focal_length_mm, sensor, …}]
        "allsky_source": "allsky",  # source the markers are drawn on
        "lens": None,               # fisheye model of that source (see README)
        "timeout_sec": 3,
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Location:
    name: str
    latitude: float
    longitude: float
    tz: ZoneInfo


def load_config(path=CONFIG_PATH):
    if not Path(path).exists():
        raise SystemExit(f"{path} not found – copy config.example.json to config.json "
                         "and set your sensor host and location")
    with open(path) as f:
        raw = json.load(f)
    cfg = _merge(DEFAULTS, raw)
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    cfg["data_dir"] = data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    _check_sources(cfg)
    _check_observatory(cfg)
    return cfg


def _check_sources(cfg):
    names = [s.get("name") for s in cfg["sources"]]
    for s in cfg["sources"]:
        if not s.get("name") or not re.fullmatch(r"[a-z0-9_-]+", s["name"]):
            raise SystemExit(f"config.json: source name {s.get('name')!r} must be "
                             "lowercase letters, digits, '-' or '_'")
        if s.get("type") not in ("homeassistant", "url", "rtsp"):
            raise SystemExit(f"config.json: source {s['name']!r} has unknown type "
                             f"{s.get('type')!r} (homeassistant, url or rtsp)")
    if len(set(names)) != len(names):
        raise SystemExit("config.json: source names must be unique")
    if cfg["layouts"] is None:
        cfg["layouts"] = list(names)
    unknown = [n for n in cfg["layouts"] if n not in names]
    if unknown:
        raise SystemExit(f"config.json: layouts {unknown} don't match any source name")


def _check_observatory(cfg):
    oc = cfg["observatory"]
    names = [r.get("name") for r in oc["rigs"]]
    for r in oc["rigs"]:
        if not r.get("name") or not re.fullmatch(r"[a-z0-9_-]+", r["name"]):
            raise SystemExit(f"config.json: observatory rig name {r.get('name')!r} must be "
                             "lowercase letters, digits, '-' or '_'")
    if len(set(names)) != len(names):
        raise SystemExit("config.json: observatory rig names must be unique")
    lens = oc.get("lens")
    if lens:
        missing = [k for k in ("width", "height", "cx", "cy", "rot_deg", "k1", "k2", "k3")
                   if k not in lens]
        if missing:
            raise SystemExit(f"config.json: observatory.lens is missing {missing}")


def resolve_location(cfg):
    loc = cfg["location"]
    tz = ZoneInfo(loc.get("timezone") or "UTC")
    name = loc.get("name") or ""
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is not None and lon is not None:
        return Location(name or f"{lat:.4f}, {lon:.4f}", float(lat), float(lon), tz)
    if not name:
        raise ValueError("config.json: location needs either a name or latitude/longitude")

    cache_path = cfg["data_dir"] / "location_cache.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except ValueError:
            cache = {}
    if name in cache:
        c = cache[name]
        return Location(name, c["latitude"], c["longitude"], tz)

    from geopy.geocoders import Nominatim
    geo = Nominatim(user_agent="cloudroll-sky-timelapse").geocode(name, timeout=15)
    if geo is None:
        raise ValueError(f"config.json: could not geocode location {name!r}; "
                         "set latitude/longitude instead")
    cache[name] = {"latitude": geo.latitude, "longitude": geo.longitude,
                   "resolved": geo.address}
    cache_path.write_text(json.dumps(cache, indent=2))
    log.info("Geocoded %r -> %.4f, %.4f (%s)", name, geo.latitude, geo.longitude, geo.address)
    return Location(name, geo.latitude, geo.longitude, tz)
