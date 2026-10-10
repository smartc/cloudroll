"""Configuration loading and location resolution.

config.json lives in the project root.  The location may be given either as a
place name (geocoded once via OpenStreetMap Nominatim and cached) or as an
explicit latitude/longitude pair, which takes precedence when both are set.
"""

import copy
import json
import logging
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
    "timelapse": {"fps": 24, "crf": 23, "offset_after_sunrise_min": 60},
    "retention": {"frames_days": 7, "readings_days": 0, "timelapses_days": 0},
    "web": {"host": "0.0.0.0", "port": 8090, "title": "CloudRoll"},
    "data_dir": "data",
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
    return cfg


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
