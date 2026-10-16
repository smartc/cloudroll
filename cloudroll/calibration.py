"""Calibration dataset: the IR sensor's view of the sky next to a ground truth.

After each night's window closes, CloudRoll harvests a standalone record of
every dark minute (sun below calibration.sun_below_deg):

  data/calibration/
    nights/<night>.jsonl.gz       line 1: header (location, night times, entity
                                  map, sampled images, notes); then one line per
                                  dark minute: raw 32×24 frame, ambient, readings,
                                  sensor settings, sun/moon, allsky statistics
    nights/<night>.summary.json   compact per-minute columns for the review page
                                  (rebuilt from the .jsonl.gz whenever missing)
    images/<night>/<stamp>.jpg    a few downscaled allsky images for human review
    labels.json                   human labels for those images

The night files hold everything an analysis needs, so they stay usable however
CloudRoll's own readings format changes.  See CALIBRATION.md.

The allsky statistics come from the capture record when CloudRoll recorded them
live ("allsky_stats"), else they are rebuilt from Home Assistant's history,
which only reaches back about 10 days.
"""

import gzip
import json
import logging
import math
import random
import re
import threading
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from . import __version__
from .sun import night_events, sky_position, window_bounds

log = logging.getLogger(__name__)

FORMAT = 1
ROWS, COLS = 24, 32
# Region Δ (ambient − pixel) quantiles kept per minute: enough to replay the
# sensor's per-pixel cloud cover for any thresholds to within ~0.5 %.
QUANTILES = np.linspace(0, 100, 33)
LABELS = ("clear", "thin", "partly", "overcast", "unusable")
STAR_BINS = [(0, 50), (50, 300), (300, 800), (800, None)]
MIN_DISAGREEMENT = 25   # % points between MLX cloud and the star count's estimate
KEY_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
KEY_STAMP = re.compile(r"\d{8}T\d{6}Z")


def dew_point(t, rh):
    """Magnus formula, °C."""
    if t is None or rh is None or rh <= 0:
        return None
    a = math.log(min(rh, 100) / 100) + 17.62 * t / (243.12 + t)
    return round(243.12 * a / (17.62 - a), 2)


def load_history_file(path):
    """A saved HA /api/history response (optionally .gz) -> {entity: [(dt, value)]}."""
    from .hass import parse_state
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        data = json.load(f)
    out = {}
    for series in data:
        if series:
            out[series[0]["entity_id"]] = [(datetime.fromisoformat(p["last_changed"]),
                                            parse_state(p.get("state"))) for p in series]
    return out


class _AsOf:
    """Value of an HA entity at a given time from its change history."""

    def __init__(self, points):
        pts = sorted(points, key=lambda p: p[0])
        self.ts = [p[0].timestamp() for p in pts]
        self.vs = [p[1] for p in pts]

    def at(self, t):
        i = bisect_right(self.ts, t) - 1
        return (self.vs[i], t - self.ts[i]) if i >= 0 else (None, None)


class Calibration:
    def __init__(self, cfg, loc, store, ha=None, skystats=None):
        c = cfg["calibration"]
        self.cfg, self.loc, self.store, self.ha = cfg, loc, store, ha
        self.enabled = bool(c.get("enabled", True))
        self.sun_below = c.get("sun_below_deg", -18)
        self.n_images = c.get("images_per_night", 12)
        self.image_width = c.get("image_width", 1024)
        self.clear_stars = c.get("clear_stars", 1000)
        self.extra = dict(c.get("history_entities") or {})
        sc = cfg.get("allsky_stats") or {}
        self.entities = dict(sc.get("entities") or {})
        self.max_age = sc.get("max_age_sec", 300)
        names = [s["name"] for s in cfg["sources"]]
        src = c.get("allsky_source") or cfg["observatory"].get("allsky_source")
        self.allsky_source = src if src in names else None
        self.offset = cfg["timelapse"]["offset_after_sunrise_min"]
        self.root = Path(cfg["data_dir"]) / "calibration"
        self.nights = self.root / "nights"
        self.images = self.root / "images"
        self.labels_path = self.root / "labels.json"
        for d in (self.nights, self.images):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- paths ---------------------------------------------------------------
    def night_path(self, day):
        return self.nights / f"{day}.jsonl.gz"

    def has_night(self, day):
        return self.night_path(day).exists()

    def night_days(self):
        return sorted((p.name[:10] for p in self.nights.glob("*.jsonl.gz")), reverse=True)

    # -- harvest -------------------------------------------------------------
    def harvest(self, day, history=None, force=False):
        """Write data/calibration/nights/<day>.jsonl.gz (+ summary, images) for
        the night inside window `day`.  `history` ({entity: [(dt, value)]}, e.g.
        from load_history_file) is used when HA no longer has the night.
        Returns the header, or None if the day has no readings."""
        start, end = window_bounds(self.loc, datetime.fromisoformat(day).date(), self.offset)
        if not force and datetime.now(self.loc.tz) < end:
            raise ValueError(f"window {day} is still open (closes {end:%Y-%m-%d %H:%M})")
        if self.store.readings_file(day) is None:
            return None
        notes = []
        lookup, hist_source = self._history(start, end, history, notes)

        minutes = []
        for rec in self.store.iter_readings(day):
            when = datetime.fromisoformat(rec["time"])
            astro = rec.get("astro") or sky_position(self.loc, when)
            if astro["sun_alt"] >= self.sun_below:
                continue
            m = {"type": "minute", "time": rec["time"], "frame": rec.get("frame"),
                 "ambient": rec.get("ambient"), "sky": rec.get("sky"),
                 "pixels": rec.get("pixels"), "readings": rec.get("readings"),
                 "color_scale": rec.get("color_scale"), "astro": astro}
            if rec.get("sensor_cloud"):
                m["sensor_cloud"] = rec["sensor_cloud"]
            if rec.get("sources") and self.allsky_source:
                m["allsky_image"] = rec["sources"].get(self.allsky_source)
            if rec.get("allsky"):
                m["allsky"], m["allsky_from"] = rec["allsky"], "live"
            elif lookup:
                stats = self._stats_at(lookup, when.timestamp(), self.entities)
                if stats:
                    m["allsky"], m["allsky_from"] = stats, "history"
            if self.extra and lookup:
                m["extra"] = {k: v for k, v in
                              self._stats_at(lookup, when.timestamp(), self.extra).items()
                              if k != "age_sec"}
            minutes.append(m)

        if not minutes:
            notes.append("no dark minutes in this window")
        stars = [self.stars(m) for m in minutes]
        images = self._sample_images(day, minutes, stars, notes)
        ev = night_events(self.loc, datetime.fromisoformat(day).date())
        header = {
            "type": "header", "format": FORMAT, "night": day,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cloudroll_version": __version__,
            "location": {"name": self.loc.name, "latitude": self.loc.latitude,
                         "longitude": self.loc.longitude, "timezone": self.loc.tz.key},
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "night_events": {k: v.isoformat() if v else None for k, v in ev.items()},
            "sun_below_deg": self.sun_below,
            "allsky_entities": self.entities, "extra_entities": self.extra,
            "allsky_max_age_sec": self.max_age, "clear_stars": self.clear_stars,
            "history_source": hist_source,
            "minutes": len(minutes), "with_stars": sum(s is not None for s in stars),
            "with_pixels": sum(m["pixels"] is not None for m in minutes),
            "images": images, "notes": notes,
        }
        path = self.night_path(day)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", compresslevel=9) as f:
            f.write(json.dumps(header, separators=(",", ":")) + "\n")
            for m in minutes:
                f.write(json.dumps(m, separators=(",", ":")) + "\n")
        tmp.replace(path)
        self._write_summary(day, header, minutes)
        log.info("Calibration: night %s – %d dark minutes, %d with stars, %d images (%s)",
                 day, header["minutes"], header["with_stars"], len(images), hist_source)
        return header

    def _history(self, start, end, history, notes):
        """({entity: _AsOf}, source) covering the window, from HA or `history`."""
        wanted = list(self.entities.values()) + list(self.extra.values())
        if not wanted:
            return {}, "none"
        pts, source = {}, "none"
        if self.ha is not None and self.ha.enabled:
            try:
                got = self.ha.history(wanted, start - timedelta(minutes=30), end)
                if any(len(v) > 1 for v in got.values()):
                    pts, source = got, "homeassistant"
            except Exception as e:  # noqa: BLE001 – fall back to the supplied history
                if not history:
                    # Retry later rather than save a night without star counts.
                    raise RuntimeError(f"Home Assistant history failed: {e}") from e
                notes.append(f"Home Assistant history failed: {e}")
        if history:
            covered = {e: v for e, v in history.items() if e in wanted and v
                       and v[0][0] <= start + timedelta(hours=1)}
            if covered and (not pts or sum(map(len, covered.values())) > sum(map(len, pts.values()))):
                pts, source = covered, "file"
        if not pts:
            notes.append("no allsky history available for this window")
        return {e: _AsOf(v) for e, v in pts.items() if v}, source

    def _stats_at(self, lookup, t, entities):
        out, ages = {}, {}
        for name, ent in entities.items():
            if ent in lookup:
                v, age = lookup[ent].at(t)
                out[name] = v
                if age is not None:
                    ages[name] = round(age)
        if not out:
            return {}
        out["age_sec"] = ages
        it = out.get("image_time")
        if isinstance(it, str):
            try:
                img = datetime.fromisoformat(it)
                img = img if img.tzinfo else img.replace(tzinfo=self.loc.tz)
                out["image_age_sec"] = round(t - img.timestamp())
            except ValueError:
                pass
        return out

    def stars(self, m):
        """Fresh star count of a minute, else None."""
        from .skystats import fresh_value
        return fresh_value(m.get("allsky"), "stars", self.max_age)

    # -- image sample ----------------------------------------------------------
    def _sample_images(self, day, minutes, stars, notes):
        if not self.allsky_source or not self.n_images:
            return []
        folder = self.store.sources / day / self.allsky_source
        if not folder.is_dir():
            notes.append("allsky images no longer kept for this night; no sample saved")
            return []
        have = {p.stem: p for p in folder.iterdir() if p.suffix in (".jpg", ".png", ".webp")}
        rng = random.Random(day)
        cand = []
        for i, m in enumerate(minutes):
            stem = Path(m["frame"] or "").stem
            if stem in have and m.get("allsky_image") in (None, "ok"):
                cand.append(i)
        if not cand:
            notes.append("no allsky images match the dark minutes")
            return []
        third = max(1, self.n_images // 3)
        picked = {}
        # Images already labelled on an earlier harvest stay in the sample.
        labelled = {k.partition("/")[2] for k in self.labels() if k.startswith(day + "/")}
        for i in cand:
            if Path(minutes[i]["frame"]).stem in labelled:
                picked[i] = "labelled"

        def take(i, reason):
            if i not in picked and len(picked) < self.n_images + len(labelled):
                picked[i] = reason

        # 1. Disagreements: MLX cloud vs the star count's clear fraction (moon down).
        scored = []
        for i in cand:
            s, cc = stars[i], (minutes[i]["readings"] or {}).get("cloud_cover")
            moon = minutes[i]["astro"]
            if s is None or cc is None or (moon["moon_alt"] > 0 and moon["moon_illum"] > 10):
                continue
            diff = abs(cc - self.star_cloud(s))
            if diff >= MIN_DISAGREEMENT:
                scored.append((diff, i))
        scored.sort(reverse=True)
        chosen_t = []
        for _, i in scored:
            t = datetime.fromisoformat(minutes[i]["time"]).timestamp()
            if all(abs(t - u) >= 1200 for u in chosen_t):   # ≥ 20 min apart
                take(i, "disagreement")
                chosen_t.append(t)
            if len(chosen_t) >= third:
                break
        # 2. One per star-count band, so partly cloudy skies are represented.
        bands = [[i for i in cand if stars[i] is not None and lo <= stars[i] < (hi or 1e9)]
                 for lo, hi in STAR_BINS]
        for _ in range(third):
            for band in bands:
                pool = [i for i in band if i not in picked]
                if pool and sum(r == "band" for r in picked.values()) < third:
                    take(rng.choice(pool), "band")
        # 3. Random, to keep the sample honest; also fills any shortfall.
        pool = [i for i in cand if i not in picked]
        rng.shuffle(pool)
        for i in pool:
            take(i, "random")

        out_dir = self.images / day
        out_dir.mkdir(parents=True, exist_ok=True)
        keep = {Path(minutes[i]["frame"]).stem for i in picked}
        for old in out_dir.glob("*.jpg"):
            if old.stem not in keep and old.stem not in labelled:
                old.unlink()
        out = []
        for i, reason in sorted(picked.items(), key=lambda kv: minutes[kv[0]]["time"]):
            m = minutes[i]
            stem = Path(m["frame"]).stem
            dst = out_dir / f"{stem}.jpg"
            try:
                with Image.open(have[stem]) as im:
                    im.draft("RGB", (self.image_width, self.image_width))
                    im = im.convert("RGB")
                    if im.width > self.image_width:
                        im = im.resize((self.image_width,
                                        round(im.height * self.image_width / im.width)),
                                       Image.LANCZOS)
                    tmp = dst.with_suffix(".tmp")
                    im.save(tmp, "JPEG", quality=85)
                    tmp.replace(dst)
            except OSError as e:
                notes.append(f"could not save image {stem}: {e}")
                continue
            out.append({"stamp": stem, "file": f"{day}/{dst.name}", "reason": reason,
                        "time": m["time"], "stars": stars[i],
                        "cloud_cover": (m["readings"] or {}).get("cloud_cover"),
                        "moon_alt": m["astro"]["moon_alt"]})
        return out

    def star_cloud(self, stars):
        """Cloud % implied by a star count (crude: the clear-sky fraction)."""
        return max(0.0, min(1.0, 1 - stars / self.clear_stars)) * 100

    # -- summaries for the review page -------------------------------------------
    def _write_summary(self, day, header, minutes):
        cols = {k: [] for k in ("t", "ambient", "humidity", "dew", "sky_center", "d_med",
                                "dq", "cloud", "clear_delta", "overcast_delta", "stars",
                                "moon_alt", "moon_illum", "sun_alt", "edge")}
        for m in minutes:
            px = m.get("pixels")
            amb = m.get("ambient")
            r = m.get("readings") or {}
            sc = m.get("sensor_cloud") or {}
            scale = m.get("color_scale") or {}
            edge = sc.get("edge_exclude", 3)
            if px is not None and amb is not None:
                a = np.asarray(px, dtype=float)
                if sc.get("region") == "center":
                    reg = a[6:18, 8:24]
                else:
                    reg = a[edge:ROWS - edge, edge:COLS - edge]
                d = amb - reg.ravel()
                dq = np.round(np.percentile(d, QUANTILES), 1).tolist()
                dmed, sky_c = round(float(np.median(d)), 2), round(float(a[6:18, 8:24].mean()), 2)
            else:
                dq, dmed, sky_c = None, None, None
            cols["t"].append(datetime.fromisoformat(m["time"]).timestamp() * 1000)
            cols["ambient"].append(amb)
            cols["humidity"].append(r.get("humidity"))
            cols["dew"].append(dew_point(r.get("ambient_temp", amb), r.get("humidity")))
            cols["sky_center"].append(sky_c)
            cols["d_med"].append(dmed)
            cols["dq"].append(dq)
            cols["cloud"].append(r.get("cloud_cover"))
            cols["clear_delta"].append(sc.get("clear_delta", scale.get("clear_delta")))
            cols["overcast_delta"].append(sc.get("overcast_delta", scale.get("overcast_delta")))
            cols["stars"].append(self.stars(m))
            cols["moon_alt"].append(m["astro"]["moon_alt"])
            cols["moon_illum"].append(m["astro"]["moon_illum"])
            cols["sun_alt"].append(m["astro"]["sun_alt"])
            cols["edge"].append(edge)
        summary = {"night": day, "format": FORMAT, "created": header["created"],
                   "minutes": header["minutes"], "with_stars": header["with_stars"],
                   "history_source": header["history_source"], "notes": header["notes"],
                   "images": header["images"], "night_events": header["night_events"],
                   "clear_stars": self.clear_stars, **cols}
        p = self.nights / f"{day}.summary.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, separators=(",", ":")))
        tmp.replace(p)
        return summary

    def summary(self, day):
        """The night's page summary, rebuilt from the night file if missing."""
        p = self.nights / f"{day}.summary.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            pass
        np_ = self.night_path(day)
        if not np_.exists():
            return None
        with gzip.open(np_, "rt") as f:
            header = json.loads(f.readline())
            minutes = [json.loads(line) for line in f if line.strip()]
        return self._write_summary(day, header, minutes)

    def header(self, day):
        """A night file's header line (cheap: the first line only)."""
        p = self.night_path(day)
        if not p.exists():
            return None
        with gzip.open(p, "rt") as f:
            return json.loads(f.readline())

    def minute(self, day, stamp):
        """The night file's minute whose frame is <stamp>.jpg, or None."""
        p = self.night_path(day)
        if not p.exists():
            return None
        needle = f'"frame":"{stamp}.jpg"'
        with gzip.open(p, "rt") as f:
            f.readline()
            for line in f:
                if needle in line:
                    return json.loads(line)
        return None

    def image_path(self, day, name):
        p = self.images / day / name
        return p if p.is_file() else None

    # -- labels --------------------------------------------------------------------
    def labels(self):
        try:
            return json.loads(self.labels_path.read_text())
        except (OSError, ValueError):
            return {}

    def set_label(self, key, label, note=""):
        """Label image `key` ("<night>/<stamp>"); label None removes it."""
        day, _, stamp = key.partition("/")
        if not (KEY_DAY.fullmatch(day) and KEY_STAMP.fullmatch(stamp)) \
                or not (self.images / day / f"{stamp}.jpg").is_file():
            raise ValueError(f"no sampled image {key!r}")
        if label is not None and label not in LABELS:
            raise ValueError("label must be one of " + ", ".join(LABELS))
        with self._lock:
            data = self.labels()
            if label is None:
                data.pop(key, None)
            else:
                data[key] = {"label": label, "note": (note or "")[:500],
                             "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            tmp = self.labels_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(self.labels_path)
        return data.get(key)
