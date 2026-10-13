"""Observatory status and telescope markers on the allsky image.

Each capture reads, in parallel:
  roof          ASCOM Alpaca Dome  .../dome/N/shutterstatus
  park sensors  ASCOM Alpaca Switch .../switch/N/getswitch (one per rig)
  rigs          NINA Advanced API   /equipment/{mount,rotator,camera,guider,focuser}/info,
                                    /sequence/json

A pier's status comes from its park sensor (offline / parked / unparked), a
rig's from NINA (offline / slewing / focusing / calibrating / capturing /
tracking / waiting / parked / idle / disconnected).  A rig gets a marker when its pier is unparked, the roof is open
and NINA reports a position.

READ-ONLY: everything here is an HTTP GET to an allow-listed endpoint.  This
code must never send anything that could move a telescope, rotator or the
roof (no PUT, no slew/park/unpark, not even Alpaca "connected").  Note that
NINA's Advanced API does actions with GET too (sequence/start, mount/slew…),
so the allow-list names exact read-only paths; check the API source before
adding one.
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
    "nina": ("equipment/mount/info", "equipment/rotator/info", "equipment/camera/info",
             "equipment/guider/info", "equipment/focuser/info", "sequence/json", "version"),
    "switch": ("getswitch",),
    "dome": ("shutterstatus",),
}

SHUTTER = {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "error"}
# Rig states drawn in the rig's colour (the rest are muted).
ACTIVE = ("slewing", "focusing", "calibrating", "capturing", "tracking", "waiting")
DEFAULT_SENSOR = {"width_px": 6248, "height_px": 4176, "pixel_um": 3.76}   # ASI2600
DEFAULT_FOCAL_MM = 1000.0
COLOURS = ["#ffd200", "#00dcff", "#ff5ac8", "#78ff78"]
RING_DEG = 2.0
MUTED_RGB = (178, 190, 195)


@dataclass
class RigState:
    name: str
    label: str
    status: str = "offline"     # NINA, see _nina()
    detail: str = ""            # running sequence item and guider state, e.g. "Take Exposure · guiding"
    pier_label: str = ""        # e.g. "Pier 1"; empty without a park sensor
    pier: str | None = None     # park sensor: offline | parked | unparked
    ra: float | None = None     # hours, JNOW
    dec: float | None = None    # degrees, JNOW
    pa: float | None = None     # rotator sky angle, degrees
    alt: float | None = None
    az: float | None = None
    marker: bool = False

    def as_dict(self):
        d = {"status": self.status}
        if self.pier:
            d["pier"] = {"label": self.pier_label, "status": self.pier}
        for k in ("detail", "ra", "dec", "pa", "alt", "az"):
            v = getattr(self, k)
            if v not in (None, ""):
                d[k] = round(v, 5) if isinstance(v, float) else v
        d["marker"] = self.marker
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
        rig_fs = [self._pool.submit(self._rig, r, when) for r in self.rigs]
        state = ObservatoryState(roof=roof_f.result())
        for f in rig_fs:
            st = f.result()
            # A marker needs the pier unparked (or no sensor), an open roof and a position.
            st.marker = (st.pier in (None, "unparked") and state.roof == "open"
                         and st.alt is not None and st.alt > 0)
            state.rigs.append(st)
        self._log_changes(state)
        return state

    def _rig(self, rig, when):
        st = RigState(rig["name"], self.label(rig))
        if rig.get("park_sensor"):
            ps = rig["park_sensor"]
            st.pier_label = ps.get("label") or "Pier"
            parked = self._park(rig)
            st.pier = "offline" if parked is None else "parked" if parked else "unparked"
        if rig.get("nina"):
            self._nina(rig, st, when)
        return st

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
        ps = rig["park_sensor"]
        try:
            v = self.client.alpaca("switch", ps["url"], "getswitch", {"Id": ps.get("id", 0)})
            return bool(v) == bool(ps.get("parked_value", True))
        except Exception as e:  # noqa: BLE001
            log.debug("Park sensor %s failed: %s", rig["name"], e)
            return None

    def _nina(self, rig, st, when):
        try:
            m = self.client.nina(rig["nina"], "equipment/mount/info")
        except Exception as e:  # noqa: BLE001 – NINA not running / API off
            log.debug("NINA %s failed: %s", rig["name"], e)
            st.status = "offline"
            return
        running = self._running_item(rig)
        cam = self._info(rig, "camera")
        guider = self._info(rig, "guider")
        focuser = self._info(rig, "focuser")
        gstate = str(guider.get("State") or "") if guider.get("Connected") else ""
        tracking = m.get("TrackingEnabled") and not m.get("AtPark")
        # Highest priority first.
        if not m.get("Connected"):
            st.status = "waiting" if running else "disconnected"
        elif m.get("Slewing"):
            st.status = "slewing"
        elif "autofocus" in (running or "").lower().replace(" ", "") or focuser.get("IsMoving"):
            st.status = "focusing"
        elif gstate.lower() == "calibrating":
            st.status = "calibrating"
        elif tracking:
            st.status = "capturing" if cam.get("IsExposing") else "tracking"
        elif running:
            st.status = "waiting"
        else:
            st.status = "parked" if m.get("AtPark") else "idle"
        g = "" if gstate.lower() in ("", "stopped") else re.sub(r"(?<!^)(?=[A-Z])", " ", gstate).lower()
        st.detail = " · ".join(x for x in (running, g) if x)
        if not m.get("Connected"):
            return
        if m.get("AtPark"):
            return              # no pointing worth drawing
        st.ra, st.dec = float(m["RightAscension"]), float(m["Declination"])
        st.alt, st.az = radec_to_altaz(st.ra, st.dec, when, self.lat, self.lon)
        try:
            r = self.client.nina(rig["nina"], "equipment/rotator/info")
            if r.get("Connected") and r.get("Position") is not None:
                st.pa = float(r["Position"])
        except Exception as e:  # noqa: BLE001 – no rotator: FOV drawn unrotated
            log.debug("NINA rotator %s failed: %s", rig["name"], e)

    def _info(self, rig, device):
        """NINA's info for camera / guider / focuser, or {} if unavailable."""
        try:
            return self.client.nina(rig["nina"], f"equipment/{device}/info") or {}
        except Exception as e:  # noqa: BLE001
            log.debug("NINA %s %s: %s", rig["name"], device, e)
            return {}

    def _running_item(self, rig):
        """Name of the innermost RUNNING sequence item, or None if no sequence runs."""
        try:
            tree = self.client.nina(rig["nina"], "sequence/json")
        except Exception as e:  # noqa: BLE001 – sequencer not initialised (409) etc.
            log.debug("NINA sequence %s: %s", rig["name"], e)
            return None

        def deepest(items):
            for it in items or []:
                if isinstance(it, dict) and it.get("Status") == "RUNNING":
                    inner = deepest(it.get("Items"))
                    name = str(it.get("Name", "")).removesuffix("_Container")
                    return inner or name or "running"
            return None
        return deepest(tree if isinstance(tree, list) else [])

    def _log_changes(self, state):
        with self._lock:
            for key, val in [("roof", (state.roof, ""))] + \
                    [(r.name, (r.status, r.detail, r.pier)) for r in state.rigs]:
                if self._last.get(key) != val:
                    if key in self._last:
                        log.info("Observatory: %s %s%s%s", key, val[0],
                                 f" ({val[1]})" if val[1] else "",
                                 f", pier {val[2]}" if len(val) > 2 and val[2] else "")
                    self._last[key] = val

    # -- drawing ------------------------------------------------------------
    def state_from_record(self, obs):
        """ObservatoryState rebuilt from a record's "observatory" field, for
        drawing markers on archived images."""
        state = ObservatoryState(roof=obs.get("roof") or "unknown")
        rigs = obs.get("rigs") or {}
        for rig in self.rigs:
            e = rigs.get(rig["name"]) or {}
            pier = e.get("pier") or {}
            state.rigs.append(RigState(
                rig["name"], self.label(rig), status=e.get("status") or "offline",
                detail=e.get("detail") or "", pier_label=pier.get("label") or "",
                pier=pier.get("status"), ra=e.get("ra"), dec=e.get("dec"), pa=e.get("pa"),
                alt=e.get("alt"), az=e.get("az"),
                marker=bool(e.get("marker")) and e.get("ra") is not None))
        return state

    def badges(self, state):
        """Caption text segments [(text, rgb[, short text])] for the allsky card."""
        out = [(f"ROOF {state.roof.upper()}", _roof_rgb(state.roof))]
        for i, r in enumerate(state.rigs):
            if r.pier:
                short = re.sub(r"^PIER\s*", "P", r.pier_label.upper())
                out.append((f"{r.pier_label.upper()} {r.pier.upper()}",
                            (253, 203, 110) if r.pier == "unparked" else MUTED_RGB,
                            f"{short} {r.pier.upper()}"))
            rgb = _hex(self.colour(i)) if r.status in ACTIVE else MUTED_RGB
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
        ring = math.radians(RING_DEG)
        # Centres of all markers first, so overlapping ones can be spread out.
        centres = [to_px(st.alt, st.az) if st.marker else None for st in state.rigs]
        placed = []
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
            ring_px = math.dist((cx, cy), pts([(0, ring)])[0])
            # k-th marker overlapping earlier ones (e.g. both rigs on Polaris for
            # focus): a slightly larger ring, ticks turned 45° and the label in
            # the next quadrant, so every rig stays visible.
            near = lambda c: c is not None and math.dist((cx, cy), c) < 4 * ring_px  # noqa: E731
            k = sum(near(c) for c in placed) % 4
            n = sum(near(c) for c in centres)       # markers in this cluster, incl. this one
            placed.append((cx, cy))
            grow = 1 + 0.35 * k
            # Alone: label just off the ring.  In a cluster: past every marker's ticks.
            label_off = ring_px * (1.6 if n < 2 else 2.3 * (1 + 0.35 * (min(n, 4) - 1)) * 0.75)
            if style in ("ring", "both"):
                r = ring * grow
                circle = [(r * math.sin(t), r * math.cos(t))
                          for t in (j * math.pi / 24 for j in range(49))]
                d.line(pts(circle), fill=col, width=w, joint="curve")
            if style in ("fov", "both"):
                d.line(pts(fov_outline(rig, st.pa)), fill=col, width=w, joint="curve")
            # Ticks outside the ring, so even a tiny FOV is easy to find.
            rp = ring_px * grow
            for j in range(4):
                t = math.pi / 2 * j + (math.pi / 4 if k % 2 else 0)
                dx, dy = math.cos(t), math.sin(t)
                d.line([cx + dx * rp * 1.3, cy + dy * rp * 1.3,
                        cx + dx * rp * 2.2, cy + dy * rp * 2.2], fill=col, width=w)
            # Label quadrant: upper right, upper left, lower right, lower left.
            sx, sy, anchor = [(1, -1, "ld"), (-1, -1, "rd"), (1, 1, "la"), (-1, 1, "ra")][k]
            d.text((cx + sx * label_off, cy + sy * label_off), st.label, fill=col, font=font,
                   anchor=anchor, stroke_width=max(2, w), stroke_fill=(0, 0, 0))
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
