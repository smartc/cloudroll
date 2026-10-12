"""Runtime settings changed from the web UI, kept in data/settings.json.

  dashboard_default   layout the live page shows until a browser picks its own
  timelapse_layouts   layouts encoded when a day's timelapse is built
  scope_marker        telescope marker style on the allsky image: ring | fov | both | off

The layout settings only matter when image sources are configured; values are
checked against the configured layouts so a removed source can't leave a
dangling choice.
"""

import json
import logging
import threading

log = logging.getLogger(__name__)

MARKER_STYLES = ("ring", "fov", "both", "off")


class Settings:
    def __init__(self, cfg):
        self.path = cfg["data_dir"] / "settings.json"
        self.layouts = list(cfg["layouts"])
        self._lock = threading.Lock()
        self._data = self._clean(self._read())

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError as e:
            log.warning("Ignoring unreadable %s (%s)", self.path, e)
            return {}

    def _clean(self, raw):
        first = self.layouts[0] if self.layouts else None
        dash = raw.get("dashboard_default")
        tl = [n for n in raw.get("timelapse_layouts") or [] if n in self.layouts]
        marker = raw.get("scope_marker")
        return {
            "dashboard_default": dash if dash in self.layouts else first,
            "timelapse_layouts": tl or ([first] if first else []),
            "scope_marker": marker if marker in MARKER_STYLES else "ring",
        }

    @property
    def dashboard_default(self):
        return self._data["dashboard_default"]

    @property
    def timelapse_layouts(self):
        return list(self._data["timelapse_layouts"])

    @property
    def scope_marker(self):
        return self._data["scope_marker"]

    def as_dict(self):
        return {**self._data, "layouts": self.layouts}

    def update(self, changes):
        """Merge and save; raises ValueError for values outside the configured
        layouts or marker styles.  Settings not in `changes` keep their value."""
        dash = changes.get("dashboard_default", self.dashboard_default)
        tl = changes.get("timelapse_layouts", self.timelapse_layouts)
        marker = changes.get("scope_marker", self.scope_marker)
        if self.layouts:
            if dash not in self.layouts:
                raise ValueError(f"unknown layout {dash!r}")
            if not isinstance(tl, list) or not tl or any(n not in self.layouts for n in tl):
                raise ValueError("timelapse_layouts must list one or more of " +
                                 ", ".join(self.layouts))
        if marker not in MARKER_STYLES:
            raise ValueError("scope_marker must be one of " + ", ".join(MARKER_STYLES))
        with self._lock:
            self._data = {"dashboard_default": dash,
                          "timelapse_layouts": [n for n in self.layouts if n in tl],
                          "scope_marker": marker}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)
        return self.as_dict()
