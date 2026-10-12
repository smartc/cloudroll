"""Observatory status and telescope markers on the allsky image.

Each capture reads, in parallel:
  roof          ASCOM Alpaca Dome  .../dome/N/shutterstatus
  park sensors  ASCOM Alpaca Switch .../switch/N/getswitch (one per rig)
  mounts        NINA Advanced API   /equipment/mount/info, /equipment/rotator/info

and works out each rig's status.  The park sensor and roof decide whether a
session is under way; NINA only supplies the pointing.  A rig gets a marker
when its park sensor says unparked, the roof is open and NINA reports a
position.

READ-ONLY: everything here is an HTTP GET to an allow-listed endpoint.  This
code must never send anything that could move a telescope, rotator or the
roof (no PUT, no slew/park/unpark, not even Alpaca "connected").
"""

import logging
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timezone

import requests
from PIL import ImageDraw

log = logging.getLogger(__name__)

# The only requests this module may make, all GET.  Anything else is refused.
ALLOWED = {
    "nina": ("equipment/mount/info", "equipment/rotator/info", "version"),
    "switch": ("getswitch",),
    "dome": ("shutterstatus",),
}

SHUTTER = {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "error"}
ACTIVE = ("tracking", "slewing", "idle")      # NINA states that get a marker
DEFAULT_SENSOR = {"width_px": 6248, "height_px": 4176, "pixel_um": 3.76}   # ASI2600
DEFAULT_FOCAL_MM = 1000.0
COLOURS = ["#ffd200", "#00dcff", "#ff5ac8", "#78ff78"]
RING_DEG = 2.0


@dataclass
class RigState:
    name: str
    label: str
    status: str = "offline"     # offline | parked | unparked | tracking | slewing | idle
    detail: str = ""            # e.g. "roof closed", "no pointing", "NINA says parked"
    ra: float | None = None     # hours, JNOW
    dec: float | None = None    # degrees, JNOW
    pa: float | None = None     # rotator sky angle, degrees
    alt: float | None = None
    az: float | None = None
    marker: bool = False

    def as_dict(self):
        d = {"status": self.status}
        for k in ("detail", "ra", "dec", "pa", "alt", "az"):
            v = getattr(self, k)
            if v not in (None, ""):
                d[k] = round(v, 5) if isinstance(v, float) else v
        return d


@dataclass
class ObservatoryState:
    roof: str = "unknown"       # open | closed | opening | closing | error | unknown
    rigs: list = field(default_factory=list)

    def as_dict(self):
        return {"roof": self.roof, "rigs": {r.name: r.as_dict() for r in self.rigs}}


class ReadOnlyClient:
    """HTTP GET to allow-listed endpoints only."""

    def __init__(self, timeout):
        self.session = requests.Session()
        self.timeout = (min(2.0, timeout), timeout)    # (connect, read)

    def get(self, kind, base, path, params=None):
        if path not in ALLOWED[kind]:
            raise PermissionError(f"refusing non-allow-listed {kind} endpoint {path!r}")
        r = self.session.get(f"{base.rstrip('/')}/{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def alpaca(self, kind, base, path, params=None):
        j = self.get(kind, base, path, params)
        if j.get("ErrorNumber"):
            raise RuntimeError(j.get("ErrorMessage") or f"Alpaca error {j['ErrorNumber']}")
        return j["Value"]

    def nina(self, base, path):
        j = self.get("nina", base, path)
        if not j.get("Success"):
            raise RuntimeError(j.get("Error") or "NINA request failed")
        return j["Response"]


class Observatory:
    def __init__(self, cfg, loc):
        oc = cfg["observatory"]
        self.cfg = oc
        self.rigs = oc["rigs"]
        self.lens = oc.get("lens")
        self.allsky = oc["allsky_source"]
        self.lat = (self.lens or {}).get("lat", loc.latitude)
        self.lon = (self.lens or {}).get("lon", loc.longitude)
        self.client = ReadOnlyClient(oc["timeout_sec"])
        self._pool = ThreadPoolExecutor(max_workers=len(self.rigs) + 1,
                                        thread_name_prefix="observatory")
        self._last = {}             # name -> (status, detail), to log changes only
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.rigs or self.cfg.get("roof"))

    def label(self, rig):
        return rig.get("label") or rig["name"].title()

    def colour(self, i):
        return self.rigs[i].get("color") or COLOURS[i % len(COLOURS)]

    # -- reading ------------------------------------------------------------
    def read(self, when):
        """One status snapshot.  Never raises."""
        roof_f = self._pool.submit(self._roof)
        rig_fs = [self._pool.submit(self._park, r) for r in self.rigs]
        state = ObservatoryState(roof=roof_f.result())
        for rig, f in zip(self.rigs, rig_fs):
            st = RigState(rig["name"], self.label(rig))
            parked = f.result()
            if parked is None:
                st.status = "offline"
            elif parked:
                st.status = "parked"
            elif state.roof != "open":
                st.status, st.detail = "unparked", f"roof {state.roof}"
            else:
                self._pointing(rig, st, when)
            state.rigs.append(st)
        self._log_changes(state)
        return state

    def _roof(self):
        roof = self.cfg.get("roof")
        if not roof:
            return "unknown"
        try:
            v = self.client.alpaca("dome", roof["url"], "shutterstatus")
            return SHUTTER.get(int(v), "error")
        except Exception as e:  # noqa: BLE001 – unreachable roof controller = unknown
            log.debug("Roof status failed: %s", e)
            return "unknown"

    def _park(self, rig):
        """True parked, False unparked, None sensor unreachable (powered down)."""
        ps = rig.get("park_sensor")
        if not ps:
            return False        # no sensor: let NINA decide
        try:
            v = self.client.alpaca("switch", ps["url"], "getswitch", {"Id": ps.get("id", 0)})
            return bool(v) == bool(ps.get("parked_value", True))
        except Exception as e:  # noqa: BLE001
            log.debug("Park sensor %s failed: %s", rig["name"], e)
            return None

    def _pointing(self, rig, st, when):
        st.status = "unparked"
        if not rig.get("nina"):
            st.detail = "no pointing"
            return
        try:
            m = self.client.nina(rig["nina"], "equipment/mount/info")
        except Exception as e:  # noqa: BLE001
            st.detail = "no pointing"
            log.debug("NINA %s failed: %s", rig["name"], e)
            return
        if not m.get("Connected"):
            st.detail = "no pointing"
            return
        if m.get("AtPark"):
            st.detail = "NINA says parked"
            return
        st.status = ("slewing" if m.get("Slewing")
                     else "tracking" if m.get("TrackingEnabled") else "idle")
        st.ra, st.dec = float(m["RightAscension"]), float(m["Declination"])
        st.alt, st.az = radec_to_altaz(st.ra, st.dec, when, self.lat, self.lon)
        st.marker = st.alt > 0
        try:
            r = self.client.nina(rig["nina"], "equipment/rotator/info")
            if r.get("Connected") and r.get("Position") is not None:
                st.pa = float(r["Position"])
        except Exception as e:  # noqa: BLE001 – no rotator: FOV drawn unrotated
            log.debug("NINA rotator %s failed: %s", rig["name"], e)

    def _log_changes(self, state):
        with self._lock:
            for key, val in [("roof", (state.roof, ""))] + \
                    [(r.name, (r.status, r.detail)) for r in state.rigs]:
                if self._last.get(key) != val:
                    if key in self._last:
                        log.info("Observatory: %s %s%s", key, val[0],
                                 f" ({val[1]})" if val[1] else "")
                    self._last[key] = val

    # -- drawing ------------------------------------------------------------
    def badges(self, state):
        """Caption text segments [(text, rgb)] for the allsky card."""
        out = [(f"ROOF {state.roof.upper()}", _roof_rgb(state.roof))]
        for i, r in enumerate(state.rigs):
            rgb = _hex(self.colour(i)) if r.status in ACTIVE else (178, 190, 195)
            out.append((f"{r.label.upper()} {r.status.upper()}", rgb))
        return out

    def draw(self, image, state, when, style, crop=None):
        """Copy of the decoded allsky panel image with a marker per active rig."""
        if style == "off" or not self.lens or not any(r.marker for r in state.rigs):
            return image
        im = image.copy()
        d = ImageDraw.Draw(im)
        w = max(2, round(im.width / 600))
        font = _marker_font(max(12, round(im.width / 70)))
        to_px = self._pixel_mapper(im, crop)
        for i, (rig, st) in enumerate(zip(self.rigs, state.rigs)):
            if not st.marker:
                continue
            col = _hex(self.colour(i))
            ra0, dec0 = math.radians(st.ra * 15), math.radians(st.dec)

            def pts(offsets):
                out = []
                for xi, eta in offsets:
                    ra, dec = tangent_to_radec(ra0, dec0, xi, eta)
                    alt, az = radec_to_altaz(math.degrees(ra) / 15, math.degrees(dec),
                                             when, self.lat, self.lon)
                    out.append(to_px(alt, az))
                return out

            cx, cy = to_px(st.alt, st.az)
            ring = math.radians(RING_DEG)
            ring_px = math.dist((cx, cy), pts([(0, ring)])[0])
            if style in ("ring", "both"):
                circle = [(ring * math.sin(t), ring * math.cos(t))
                          for t in (k * math.pi / 24 for k in range(49))]
                d.line(pts(circle), fill=col, width=w, joint="curve")
            if style in ("fov", "both"):
                d.line(pts(fov_outline(rig, st.pa)), fill=col, width=w, joint="curve")
            # Ticks outside the ring, so even a tiny FOV is easy to find.
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                d.line([cx + dx * ring_px * 1.3, cy + dy * ring_px * 1.3,
                        cx + dx * ring_px * 2.2, cy + dy * ring_px * 2.2], fill=col, width=w)
            d.text((cx + ring_px * 1.6, cy - ring_px * 1.6), st.label, fill=col, font=font,
                   anchor="ld", stroke_width=max(2, w), stroke_fill=(0, 0, 0))
        return im

    def _pixel_mapper(self, im, crop):
        """alt/az -> pixel in the (decoded, possibly cropped) panel image."""
        lens = self.lens
        l, t, r, b = crop or (0, 0, 1, 1)

        def to_px(alt, az):
            x, y = project(lens, alt, az)
            fx, fy = x / lens["width"], y / lens["height"]
            return ((fx - l) / (r - l) * im.width, (fy - t) / (b - t) * im.height)
        return to_px

    def stop(self):
        self._pool.shutdown(wait=False)


# -- astronomy -----------------------------------------------------------------
def julian_date(when):
    return when.astimezone(timezone.utc).timestamp() / 86400.0 + 2440587.5


def radec_to_altaz(ra_h, dec_deg, when, lat, lon):
    """Apparent (of-date) RA/Dec -> (alt, az) in degrees, az from north through east.
    GMST from the IAU 1982 expression; refraction ignored."""
    jd = julian_date(when)
    t = (jd - 2451545.0) / 36525.0
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
            + 0.000387933 * t * t - t ** 3 / 38710000.0)
    ha = math.radians((gmst + lon - ra_h * 15.0) % 360.0)
    dec, la = math.radians(dec_deg), math.radians(lat)
    alt = math.asin(math.sin(dec) * math.sin(la) + math.cos(dec) * math.cos(la) * math.cos(ha))
    az = math.atan2(-math.sin(ha) * math.cos(dec),
                    math.sin(dec) * math.cos(la) - math.cos(dec) * math.sin(la) * math.cos(ha))
    return math.degrees(alt), math.degrees(az) % 360.0


def tangent_to_radec(ra0, dec0, xi, eta):
    """Inverse gnomonic projection: offsets (radians; xi east, eta north) about
    (ra0, dec0) -> (ra, dec) in radians."""
    dd = math.cos(dec0) - eta * math.sin(dec0)
    ra = ra0 + math.atan2(xi, dd)
    dec = math.atan2(math.sin(dec0) + eta * math.cos(dec0), math.hypot(xi, dd))
    return ra, dec


def project(lens, alt, az):
    """alt/az (degrees) -> full-resolution pixel (x, y) through the fisheye model."""
    z = math.radians(90.0 - alt)
    a = math.radians(az + lens["rot_deg"])
    r = lens["k1"] * z + lens["k2"] * z ** 3 + lens["k3"] * z ** 5
    return lens["cx"] - r * math.sin(a), lens["cy"] - r * math.cos(a)


def fov_size_deg(rig):
    s = {**DEFAULT_SENSOR, **(rig.get("sensor") or {})}
    f = float(rig.get("focal_length_mm") or DEFAULT_FOCAL_MM)
    side = lambda px: math.degrees(2 * math.atan(px * s["pixel_um"] / 1000.0 / 2 / f))  # noqa: E731
    return side(s["width_px"]), side(s["height_px"])


def fov_outline(rig, pa):
    """Closed outline of the camera's field as tangent-plane offsets (radians).

    The sensor's "up" (height) axis points at position angle pa (north through
    east), plus rotation_offset_deg; the width axis is 90° clockwise from it on
    the sky (towards west at pa=0), or the other way when flip is set."""
    w, h = (math.radians(v) / 2 for v in fov_size_deg(rig))
    p = math.radians((pa or 0.0) + float(rig.get("rotation_offset_deg") or 0.0))
    up = (math.sin(p), math.cos(p))
    right = (-math.cos(p), math.sin(p))
    if rig.get("flip"):
        right = (-right[0], -right[1])
    corners = [(-1, 1), (1, 1), (1, -1), (-1, -1), (-1, 1)]
    out = []
    for (a1, b1), (a2, b2) in zip(corners, corners[1:]):
        for k in range(8):
            f = k / 8
            a, b = a1 + (a2 - a1) * f, b1 + (b2 - b1) * f
            out.append((a * w * right[0] + b * h * up[0], a * w * right[1] + b * h * up[1]))
    out.append(out[0])
    return out


# -- helpers -------------------------------------------------------------------
def _hex(c):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", c or "")
    if not m:
        return (255, 210, 0)
    v = int(m.group(1), 16)
    return (v >> 16, (v >> 8) & 255, v & 255)


def _roof_rgb(roof):
    return {"open": (85, 239, 196), "closed": (178, 190, 195),
            "opening": (253, 203, 110), "closing": (253, 203, 110)}.get(roof, (225, 112, 85))


def _marker_font(size):
    from .render import _font
    return _font(size, True)
