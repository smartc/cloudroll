"""Fit the allsky camera's fisheye `lens` model by matching stars to Hipparcos.

Model (the same one cloudroll/observatory.py projects through):
    z = zenith angle (rad), a = azimuth (rad, N=0, E=90) + rot_deg
    r = k1·z + k2·z³ + k3·z⁵                      (pixels)
    x = cx − r·sin(a)     (east is left: the sky seen from below)
    y = cy − r·cos(a)     (north is up)

Steps: detect stars in a clear, moonless frame; compute the alt/az of every
Hipparcos star brighter than `maglim` at the frame's mid-exposure time; seed the
model (from a few hand-identified stars, or a coarse search with the bright ones);
then grow a least-squares fit outward from the zenith.  See LENS.md.

Needs scipy and skyfield (requirements-lensfit.txt), which the service doesn't.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

STAMP = re.compile(r"(\d{8}T\d{6}Z)")
HIP_FILE, EPHEMERIS = "hip_main.dat", "de421.bsp"


def detect_stars(path, max_r=None):
    """Star centroids (brightest first), their fluxes, and the image size."""
    from scipy import ndimage
    im = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    h, w = im.shape
    max_r = max_r or 0.48 * min(w, h)
    # Background: median on a 4x-reduced image, upsampled.
    small = im[::4, ::4]
    bg = ndimage.median_filter(small, size=15)
    bg = np.kron(bg, np.ones((4, 4), np.float32))[:h, :w]
    d = ndimage.gaussian_filter(im - bg, 1.0)
    noise = 1.4826 * np.median(np.abs(d - np.median(d)))
    lab, n = ndimage.label(d > 6 * noise)
    idx = np.arange(1, n + 1)
    cy, cx = np.array(ndimage.center_of_mass(d, lab, idx)).T
    flux = ndimage.sum(d, lab, idx)
    size = ndimage.sum(np.ones_like(d), lab, idx)
    keep = (size >= 3) & (size < 400) & (np.hypot(cx - w / 2, cy - h / 2) < max_r)
    order = np.argsort(-flux[keep])
    return np.column_stack([cx[keep], cy[keep]])[order], flux[keep][order], (w, h)


def catalogue_altaz(when, lat, lon, skydata, maglim=5.5, pressure_mbar=900):
    """Alt/az (degrees), magnitude and HIP number of the catalogue stars above 3°.
    skyfield downloads the catalogue and ephemeris into `skydata` if missing."""
    from skyfield.api import Loader, Star, wgs84
    from skyfield.data import hipparcos
    load = Loader(str(skydata))
    ts = load.timescale()
    t = ts.from_datetime(when.astimezone(timezone.utc))
    eph = load(EPHEMERIS)
    with load.open(hipparcos.URL) as f:
        cat = read_hipparcos(f, maglim)
    star = Star(ra_hours=cat["ra"] / 15, dec_degrees=cat["dec"], ra_mas_per_year=cat["pmra"],
                dec_mas_per_year=cat["pmdec"], parallax_mas=cat["plx"], epoch=ts.J(1991.25))
    obs = (eph["earth"] + wgs84.latlon(lat, lon)).at(t)
    alt, az, _ = obs.observe(star).apparent().altaz(temperature_C=0, pressure_mbar=pressure_mbar)
    m = alt.degrees > 3
    return alt.degrees[m], az.degrees[m], cat["mag"][m], cat["hip"][m]


def read_hipparcos(f, maglim):
    """hip_main.dat ('|'-separated, CDS I/239) → arrays, without skyfield's pandas loader.
    Fields: 1 HIP, 5 Vmag, 8/9 RA/Dec (deg, ICRS, epoch J1991.25), 11 parallax,
    12/13 proper motion (mas/yr)."""
    rows = []
    for line in f:
        c = (line.decode("latin-1") if isinstance(line, bytes) else line).split("|")
        try:
            mag = float(c[5])
            if mag > maglim:
                continue
            rows.append((int(c[1]), mag, float(c[8]), float(c[9]),
                         float(c[11] or 0), float(c[12] or 0), float(c[13] or 0)))
        except (ValueError, IndexError):
            continue        # a few entries have no astrometry
    a = np.array(rows)
    return {"hip": a[:, 0].astype(int), "mag": a[:, 1], "ra": a[:, 2], "dec": a[:, 3],
            "plx": np.clip(a[:, 4], 0.001, None), "pmra": a[:, 5], "pmdec": a[:, 6]}


def project(p, alt, az):
    """Vectorised observatory.project(): p = (cx, cy, rot, k1, k2, k3)."""
    cx, cy, rot, k1, k2, k3 = p
    z = np.radians(90 - alt)
    a = np.radians(az + rot)
    r = k1 * z + k2 * z ** 3 + k3 * z ** 5
    return np.column_stack([cx - r * np.sin(a), cy - r * np.cos(a)])


def match(p, alt, az, det, radius):
    """(catalogue index, detection index) pairs within `radius` px, one-to-one."""
    pred = project(p, alt, az)
    d = np.hypot(pred[:, None, 0] - det[None, :, 0], pred[:, None, 1] - det[None, :, 1])
    j = d.argmin(1)
    best = d[np.arange(len(j)), j]
    used, pairs = set(), []
    for i in np.argsort(best):
        if best[i] < radius and j[i] not in used:
            used.add(j[i])
            pairs.append((i, j[i]))
    return np.array(pairs, dtype=int).reshape(-1, 2)


def seed_from_anchors(anchors, alt, az, hip, size):
    """Initial model from hand-identified stars: {HIP number: (x, y)}."""
    from scipy import optimize
    idx, xy = [], []
    for h, pos in anchors.items():
        i = np.flatnonzero(hip == h)
        if not len(i):
            raise ValueError(f"HIP {h} is not in the catalogue above 3° at that time")
        idx.append(int(i[0]))
        xy.append(pos)
    idx, xy = np.array(idx), np.array(xy, float)
    w, h = size
    res = optimize.least_squares(lambda q: (project(q, alt[idx], az[idx]) - xy).ravel(),
                                 [w / 2, h / 2, 0, 0.36 * min(w, h), 0, 0])
    return res.x


def seed_search(alt, az, mag, det, size):
    """Coarse grid over centre, rotation and scale, scored on bright stars.
    Scale and centre ranges are relative to the image, so other cameras work too."""
    w, h = size
    s = min(w, h)
    bright = mag < 3.0
    step = round(0.011 * s)
    best = None
    for rot in np.arange(-20, 20.1, 2):
        for k1 in np.linspace(0.27 * s, 0.39 * s, 10):
            for dx in range(-2 * step, 2 * step + 1, step):
                for dy in range(-2 * step, 2 * step + 1, step):
                    p = np.array([w / 2 + dx, h / 2 + dy, rot, k1, 0.0, 0.0])
                    n = len(match(p, alt[bright], az[bright], det[:200], 25))
                    if best is None or n > best[0]:
                        best = (n, p)
    log.info("coarse: %d bright matches", best[0])
    return best[1]


def refine(p, alt, az, det):
    """Grow the fit outward from the zenith so the edge isn't extrapolated blind."""
    from scipy import optimize

    def resid(q, ii, jj):
        return (project(q, alt[ii], az[ii]) - det[jj]).ravel()

    p = np.asarray(p, float)
    for zmax in (45, 55, 65, 75, 85):
        sel = np.flatnonzero(90 - alt <= zmax)
        for radius in (20, 12, 8):
            pr = match(p, alt[sel], az[sel], det, radius)
            if len(pr) < 8:
                return p, pr, np.inf
            res = optimize.least_squares(resid, p, args=(sel[pr[:, 0]], pr[:, 1]),
                                         loss="soft_l1", f_scale=3)
            p = res.x
        log.info("  z <= %d°: %d matches", zmax, len(pr))
    pr = match(p, alt, az, det, 6)
    e = np.hypot(*(project(p, alt[pr[:, 0]], az[pr[:, 0]]) - det[pr[:, 1]]).T)
    return p, pr, float(np.sqrt(np.mean(e ** 2)))


def residuals_by_altitude(p, alt, az, det, pairs):
    """RMS error in degrees per altitude band, to show where the model is weak."""
    e = np.hypot(*(project(p, alt[pairs[:, 0]], az[pairs[:, 0]]) - det[pairs[:, 1]]).T)
    a = alt[pairs[:, 0]]
    deg_per_px = 180 / np.pi / p[3]
    out = {}
    for lo, hi in ((3, 20), (20, 45), (45, 90.1)):
        m = (a >= lo) & (a < hi)
        if m.any():
            out[f"{lo}-{min(hi, 90):g}"] = {"stars": int(m.sum()),
                                            "rms_deg": round(float(np.sqrt(np.mean(e[m] ** 2)) * deg_per_px), 3)}
    return out


def fit(frame, when, lat, lon, skydata, anchors=None, maglim=5.5, pressure_mbar=900):
    """Fit the model to one frame.  Returns (lens dict, fit details for the check image)."""
    det, _, size = detect_stars(frame)
    log.info("detected %d star candidates", len(det))
    det = det[:1500]
    alt, az, mag, hip = catalogue_altaz(when, lat, lon, skydata, maglim, pressure_mbar)
    log.info("%d catalogue stars above 3°, mag <= %s", len(alt), maglim)
    seed = (seed_from_anchors(anchors, alt, az, hip, size) if anchors
            else seed_search(alt, az, mag, det, size))
    p, pairs, rms = refine(seed, alt, az, det)
    # A clear night here matches ~55 % of the catalogue; cloud, twilight or a wrong
    # seed match a few dozen chance coincidences, which can still give a low RMS.
    need = max(100, round(0.15 * len(alt)))
    if not np.isfinite(rms) or len(pairs) < need:
        raise ValueError(f"only {len(pairs)} of {len(alt)} catalogue stars matched (need {need}): "
                         "use a clear, dark, moonless frame, or seed it with --anchor")
    cx, cy, rot, k1, k2, k3 = (float(v) for v in p)
    w, h = size
    lens = {"width": w, "height": h, "cx": round(cx, 2), "cy": round(cy, 2),
            "rot_deg": round(rot, 4), "k1": round(k1, 3), "k2": round(k2, 4), "k3": round(k3, 5),
            "lat": lat, "lon": lon,
            "fit": {"frame": Path(frame).name, "time": when.astimezone(timezone.utc).isoformat(),
                    "matches": int(len(pairs)), "rms_px": round(rms, 2),
                    "rms_deg": round(rms * 180 / np.pi / k1, 3),
                    "by_altitude": residuals_by_altitude(p, alt, az, det, pairs),
                    "maglim": maglim, "seed": "anchors" if anchors else "search"}}
    return lens, {"p": p, "alt": alt, "az": az, "mag": mag, "det": det, "pairs": pairs}


def check_image(frame, details, out, width=1600):
    """Diagnostic overlay: red crosses = catalogue, green rings = matched detections,
    blue = the model's 0/30/60° altitude rings and cardinal spokes."""
    p, alt, az, mag, det, pairs = (details[k] for k in ("p", "alt", "az", "mag", "det", "pairs"))
    im = Image.open(frame).convert("RGB")
    w, h = im.size
    dr = ImageDraw.Draw(im)
    for (x, y), m in zip(project(p, alt, az), mag):
        if 0 <= x < w and 0 <= y < h:
            s = 14 if m < 3 else 8
            dr.line([x - s, y, x + s, y], fill=(255, 60, 60), width=2)
            dr.line([x, y - s, x, y + s], fill=(255, 60, 60), width=2)
    for _, j in pairs:
        x, y = det[j]
        dr.ellipse([x - 12, y - 12, x + 12, y + 12], outline=(60, 255, 60), width=2)
    for ring in (0, 30, 60):
        pts = project(p, np.full(361, ring), np.arange(361))
        dr.line([tuple(q) for q in pts], fill=(80, 140, 255), width=3)
    for az0 in (0, 90, 180, 270):
        pts = project(p, np.array([0, 90]), np.array([az0, az0]))
        dr.line([tuple(q) for q in pts], fill=(80, 140, 255), width=2)
    if w > width:
        im = im.resize((width, round(h * width / w)), Image.LANCZOS)
    im.save(out, quality=90)


def frame_time(store, loc, frame):
    """Mid-exposure time of a stored allsky frame, from its capture record.

    The record's allsky image_time (indi-allsky's exposure start, local time) plus
    half the exposure is the best estimate.  Without it, the capture stamp in the
    file name is used, which is typically 0–20 s late (up to ≈ 0.1° of sky)."""
    m = STAMP.search(Path(frame).name)
    if not m:
        return None, "no time: pass --time"
    stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    for day in {stamp.astimezone(loc.tz).date().isoformat(),
                (stamp.astimezone(loc.tz) - timedelta(days=1)).date().isoformat()}:
        for rec in store.iter_readings(day) or []:
            if rec.get("frame") != m.group(1) + ".jpg":
                continue
            a = rec.get("allsky") or {}
            if a.get("image_time"):
                start = datetime.fromisoformat(a["image_time"]).replace(tzinfo=loc.tz)
                return start + timedelta(seconds=(a.get("exposure") or 0) / 2), \
                    "allsky image_time + exposure/2"
    return stamp, "capture stamp (no allsky image_time recorded; typically 0–20 s late)"


def find_frame(store, name):
    """A stamp like 20261012T083800Z → the stored allsky source image, else the path."""
    p = Path(name)
    if p.exists():
        return p
    m = STAMP.search(name)
    if m:
        hits = sorted(store.sources.glob(f"*/*/{m.group(1)}.*"))
        if hits:
            return hits[0]
    raise SystemExit(f"{name}: no such file or stored frame")


def parse_anchors(specs):
    """["24608:1780,1203", …] → {24608: (1780.0, 1203.0)}"""
    out = {}
    for spec in specs or []:
        try:
            h, xy = spec.split(":")
            x, y = (float(v) for v in xy.split(","))
        except ValueError:
            raise SystemExit(f"--anchor {spec!r}: expected HIP:x,y")
        out[int(h)] = (x, y)
    return out


def run(cfg, loc, store, args):
    """`python -m cloudroll lensfit`: fit, write the model and check image, print the
    config snippet.  Never touches config.json."""
    try:
        import scipy, skyfield  # noqa: F401
    except ImportError:
        raise SystemExit("lensfit needs scipy and skyfield: pip install -r requirements-lensfit.txt")
    frame = find_frame(store, args.frame)
    if args.time:
        when = datetime.fromisoformat(args.time.replace("Z", "+00:00"))
        when = when if when.tzinfo else when.replace(tzinfo=loc.tz)
        how = "--time"
    else:
        when, how = frame_time(store, loc, frame)
        if when is None:
            raise SystemExit(f"{frame}: {how}")
    obs_lens = (cfg.get("observatory") or {}).get("lens") or {}
    lat = args.lat if args.lat is not None else obs_lens.get("lat", loc.latitude)
    lon = args.lon if args.lon is not None else obs_lens.get("lon", loc.longitude)
    print(f"Frame : {frame}")
    print(f"Time  : {when.astimezone(loc.tz):%Y-%m-%d %H:%M:%S %Z} ({how})")
    print(f"Site  : {lat:.5f}, {lon:.5f}")
    out_dir = Path(cfg["data_dir"]) / "lensfit"
    out_dir.mkdir(parents=True, exist_ok=True)
    lens, details = fit(frame, when, lat, lon, Path(cfg["data_dir"]) / "skydata",
                        parse_anchors(args.anchor), args.maglim, args.pressure)
    f = lens["fit"]
    print(f"Fit   : {f['matches']} stars, RMS {f['rms_px']} px ({f['rms_deg']}°)")
    for band, v in f["by_altitude"].items():
        print(f"        alt {band:>6}°: {v['stars']:4d} stars, RMS {v['rms_deg']}°")
    stem = Path(frame).stem
    (out_dir / f"{stem}.lens.json").write_text(json.dumps(lens, indent=2))
    check_image(frame, details, out_dir / f"{stem}.check.jpg")
    print(f"Wrote : {out_dir / (stem + '.lens.json')}")
    print(f"        {out_dir / (stem + '.check.jpg')}  (red = catalogue, green = matched)")
    if obs_lens.get("k1"):
        dx, dy = lens["cx"] - obs_lens["cx"], lens["cy"] - obs_lens["cy"]
        print(f"vs config: centre moved ({dx:+.1f}, {dy:+.1f}) px, rotation "
              f"{lens['rot_deg'] - obs_lens['rot_deg']:+.2f}°, k1 {lens['k1'] - obs_lens['k1']:+.1f}")
    snippet = {k: lens[k] for k in ("width", "height", "cx", "cy", "rot_deg", "k1", "k2", "k3")}
    print('\nTo use it, set observatory.lens in config.json to:\n"lens": ' + json.dumps(snippet))
