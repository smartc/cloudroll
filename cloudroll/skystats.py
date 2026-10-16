"""Allsky camera statistics recorded with each capture (config "allsky_stats").

indi-allsky publishes per-image statistics to Home Assistant: the number of
stars it detected, its own SQM estimate, exposure, gain, moon mode, dew heater
level.  The star count is the useful one: on a moonless night it is ~1000+
under a clear sky and near 0 when overcast, which makes it a ground truth for
the IR sensor's cloud cover (see CALIBRATION.md).

Config:
  "allsky_stats": {
    "entities": {"stars": "sensor.allsky_stars", "sqm": "sensor.allsky_sqm", …},
    "max_age_sec": 300
  }
Entity names on the left are free-form; "stars" is the one CloudRoll charts.
An optional "image_time" entity (indi-allsky's exposure date, local time) dates
the latest allsky image.  Use it if you can: HA only re-stamps an entity when
its value changes, so a star count that sits at 0 under thick cloud would
otherwise look stale.
"""

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class AllskyStats:
    def __init__(self, cfg, ha):
        sc = cfg.get("allsky_stats") or {}
        self.entities = dict(sc.get("entities") or {})
        self.max_age = sc.get("max_age_sec", 300)
        self.ha = ha
        self.tz = ZoneInfo(cfg["location"].get("timezone") or "UTC")
        self.last_error = None

    @property
    def enabled(self):
        return bool(self.entities) and self.ha.enabled

    def read(self):
        """{name: value, …, "age_sec": {name: s}} for the configured entities.
        Never raises: an unreachable entity is simply missing."""
        out, ages = {}, {}
        now = datetime.now(timezone.utc)
        t0 = time.monotonic()
        errors = []
        for name, entity in self.entities.items():
            try:
                value, updated = self.ha.state(entity)
            except Exception as e:  # noqa: BLE001 – one bad entity must not lose the rest
                errors.append(f"{name}: {e}")
                continue
            out[name] = value
            if updated is not None:
                ages[name] = round((now - updated).total_seconds())
        out["age_sec"] = ages
        img = self._image_time(out.get("image_time"))
        if img is not None:
            out["image_age_sec"] = round((now - img).total_seconds())
        self.last_error = "; ".join(errors) or None
        if errors:
            log.info("Allsky stats: %s", self.last_error)
        log.debug("Allsky stats read in %.2f s", time.monotonic() - t0)
        return out

    def _image_time(self, value):
        """indi-allsky's "YYYY-MM-DD HH:MM:SS" (local, naive) or ISO -> aware datetime."""
        if not isinstance(value, str):
            return None
        try:
            t = datetime.fromisoformat(value)
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=self.tz)

    def fresh(self, stats, name="stars"):
        """The value of `name` if the allsky image it came from is within
        max_age_sec (or, without an image_time entity, if the value changed that
        recently), else None."""
        return fresh_value(stats, name, self.max_age)


def fresh_value(stats, name, max_age):
    if not stats or stats.get(name) is None:
        return None
    age = stats.get("image_age_sec")
    if age is None:
        age = (stats.get("age_sec") or {}).get(name)
    return stats[name] if age is None or age <= max_age else None
