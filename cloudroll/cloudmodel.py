"""Recalculated cloud cover: the sensor's algorithm replayed from stored frames.

The SkyConditions sensor interpolates each pixel's Δ = ambient − pixel between
an Overcast Δ (100 %) and a Clear Δ (0 %).  Fixed thresholds can't serve a
−3 °C night and a 21 °C afternoon (CALIBRATION.md), so this model shifts both by
amb_slope × (ambient − amb_ref) – the same correction proposed for the firmware
in SkyConditions_90640#9.  With amb_slope 0 it reproduces the sensor exactly.

Config ("cloud_recalc"):
  enabled, label, clear_delta, overcast_delta, amb_slope, amb_ref,
  method ("pixel" | "mean"), region ("full" | "center"), edge_exclude
"""

import numpy as np

ROWS, COLS = 24, 32
CENTER = (slice(6, 18), slice(8, 24))   # the sensor's centre 50 % FOV


class CloudModel:
    def __init__(self, cfg):
        c = cfg.get("cloud_recalc") or {}
        self.enabled = bool(c.get("enabled"))
        self.label = c.get("label") or "Recalculated"
        self.clear = float(c.get("clear_delta", 26.0))
        self.overcast = float(c.get("overcast_delta", 16.0))
        self.slope = float(c.get("amb_slope", 0.0))
        self.ref = float(c.get("amb_ref", 0.0))
        self.method = c.get("method", "pixel")
        self.region = c.get("region", "full")
        self.edge = int(c.get("edge_exclude", 3))

    def params(self):
        return {"label": self.label, "clear_delta": self.clear, "overcast_delta": self.overcast,
                "amb_slope": self.slope, "amb_ref": self.ref, "method": self.method,
                "region": self.region, "edge_exclude": self.edge}

    def thresholds(self, ambient):
        """(clear, overcast) Δ in effect at this ambient temperature."""
        off = self.slope * (ambient - self.ref)
        return self.clear + off, self.overcast + off

    def cover(self, pixels, ambient):
        """Cloud cover % for one stored frame, or None without a frame."""
        if pixels is None or ambient is None:
            return None
        a = np.asarray(pixels, dtype=float)
        if a.shape != (ROWS, COLS):
            return None
        clear, over = self.thresholds(ambient)
        span = clear - over
        if span <= 0.01:
            return 50.0
        if self.method == "mean":
            d = ambient - a[CENTER].mean()
        elif self.region == "center":
            d = ambient - a[CENTER]
        else:
            e = self.edge if 2 * self.edge < min(ROWS, COLS) else 0
            d = ambient - a[e:ROWS - e, e:COLS - e]
        return round(float(np.clip(1 - (d - over) / span, 0, 1).mean() * 100), 1)
