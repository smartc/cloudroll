"""On-disk layout.

data/
  latest.jpg / latest.json              most recent capture
  frames/<window-date>/<UTC stamp>.jpg  composite frames (deleted once the day's timelapse is built)
  readings/<window-date>.jsonl          one JSON record per capture: readings + raw 32×24 matrix
  readings/<window-date>.jsonl.gz       same, gzipped once the window has closed
  timelapses/<window-date>.mp4          finished day timelapse
  timelapses/<window-date>.jpg          poster (middle frame)
  timelapses/<window-date>.json         metadata
"""

import gzip
import json
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STAMP_FMT = "%Y%m%dT%H%M%SZ"
SCALAR_FIELDS = ["cloud_cover", "ambient_temp", "sky_temp", "brightness", "sqm",
                 "pressure", "humidity", "raining"]


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.frames = self.root / "frames"
        self.readings = self.root / "readings"
        self.timelapses = self.root / "timelapses"
        for d in (self.frames, self.readings, self.timelapses):
            d.mkdir(parents=True, exist_ok=True)
        self._lines_cache = {}          # day -> (stat key, [raw JSON lines])
        self._summary_cache = {}        # day -> (stat key, summary)
        self._cache_lock = threading.Lock()

    # -- frames -------------------------------------------------------------
    def save_frame(self, window_date, when, img, quality):
        day_dir = self.frames / window_date.isoformat()
        day_dir.mkdir(exist_ok=True)
        stamp = when.astimezone(timezone.utc).strftime(STAMP_FMT)
        jpg = day_dir / f"{stamp}.jpg"
        tmp = jpg.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=quality)
        tmp.replace(jpg)
        latest_tmp = self.root / "latest.tmp"
        shutil.copyfile(jpg, latest_tmp)
        latest_tmp.replace(self.root / "latest.jpg")
        return jpg

    def frame_days(self):
        return sorted((p.name for p in self.frames.iterdir() if p.is_dir()), reverse=True)

    def frames_for(self, day):
        d = self.frames / day
        return sorted(p.name for p in d.glob("*.jpg")) if d.is_dir() else []

    def delete_frames(self, day):
        shutil.rmtree(self.frames / day, ignore_errors=True)

    def recent_frames(self, n):
        out = []
        for day in self.frame_days():
            for name in reversed(self.frames_for(day)):
                out.append((day, name))
                if len(out) >= n:
                    return out
        return out

    def latest_meta(self):
        try:
            return json.loads((self.root / "latest.json").read_text())
        except (OSError, ValueError):
            return None

    # -- readings (numeric data, kept long-term) ----------------------------
    def append_reading(self, window_date, record):
        with open(self.readings / f"{window_date.isoformat()}.jsonl", "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        summary = {k: v for k, v in record.items() if k != "pixels"}
        (self.root / "latest.json").write_text(json.dumps(summary))

    def readings_file(self, day):
        """Path of the day's readings file (.jsonl while open, .jsonl.gz once closed)."""
        for name in (f"{day}.jsonl.gz", f"{day}.jsonl"):
            p = self.readings / name
            if p.exists():
                return p
        return None

    def iter_readings(self, day):
        for line in self.reading_lines(day):
            yield json.loads(line)

    def reading_lines(self, day):
        """Raw JSONL lines for a day, cached until the file changes (2 days kept)."""
        p = self.readings_file(day)
        if p is None:
            return []
        st = p.stat()
        key = (p.name, st.st_mtime_ns, st.st_size)
        with self._cache_lock:
            hit = self._lines_cache.get(day)
            if hit and hit[0] == key:
                return hit[1]
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt") as f:
            lines = [ln for ln in f if ln.strip()]
        with self._cache_lock:
            self._lines_cache[day] = (key, lines)
            while len(self._lines_cache) > 2:
                self._lines_cache.pop(next(iter(self._lines_cache)))
        return lines

    def reading(self, day, index):
        lines = self.reading_lines(day)
        if not 0 <= index < len(lines):
            return None
        return json.loads(lines[index])

    def readings_days(self):
        return sorted({p.name[:10] for p in self.readings.glob("*.jsonl*")
                       if not p.name.endswith(".summary.json")}, reverse=True)

    def readings_summary(self, day):
        """Per-day stats for the data index.  Closed (gzipped) days persist theirs
        in <day>.summary.json so the index never has to re-read old files."""
        p = self.readings_file(day)
        if p is None:
            return None
        st = p.stat()
        key = (p.name, st.st_mtime_ns, st.st_size)
        with self._cache_lock:
            hit = self._summary_cache.get(day)
            if hit and hit[0] == key:
                return hit[1]
        side = self.readings / f"{day}.summary.json"
        summary = None
        if p.suffix == ".gz" and side.exists():
            try:
                summary = json.loads(side.read_text())
                if summary.get("file") != p.name or summary.get("bytes") != st.st_size:
                    summary = None
            except ValueError:
                summary = None
        if summary is None:
            summary = self._compute_summary(day, p, st.st_size)
            if p.suffix == ".gz":
                side.write_text(json.dumps(summary))
        with self._cache_lock:
            self._summary_cache[day] = (key, summary)
        return summary

    def _compute_summary(self, day, path, size):
        n = with_grid = 0
        first = last = None
        cloud, sky, sqm, rain = [], [], [], 0
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                n += 1
                first = first or rec["time"]
                last = rec["time"]
                with_grid += rec.get("pixels") is not None
                r = rec.get("readings", {})
                if r.get("cloud_cover") is not None:
                    cloud.append(r["cloud_cover"])
                if r.get("sky_temp") is not None:
                    sky.append(r["sky_temp"])
                if r.get("sqm") is not None:
                    sqm.append(r["sqm"])
                rain += bool(r.get("raining"))
        return {
            "date": day, "file": path.name, "bytes": size, "compressed": path.suffix == ".gz",
            "records": n, "with_grid": with_grid, "first": first, "last": last,
            "cloud_avg": round(sum(cloud) / len(cloud), 1) if cloud else None,
            "sky_min": min(sky) if sky else None,
            "sqm_max": max(sqm) if sqm else None,
            "rain_records": rain,
        }

    def compress_readings(self, day):
        src = self.readings / f"{day}.jsonl"
        if not src.exists():
            return
        dst = self.readings / f"{day}.jsonl.gz"
        tmp = dst.with_suffix(".tmp")
        with open(src, "rb") as fi, gzip.open(tmp, "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo)
        tmp.replace(dst)
        src.unlink()

    # -- timelapses ---------------------------------------------------------
    def timelapse_path(self, day):
        return self.timelapses / f"{day}.mp4"

    def timelapse_list(self):
        out = []
        for meta_path in sorted(self.timelapses.glob("*.json"), reverse=True):
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                continue
            if self.timelapse_path(meta["date"]).exists():
                out.append(meta)
        return out

    def timelapse_meta(self, day):
        try:
            return json.loads((self.timelapses / f"{day}.json").read_text())
        except (OSError, ValueError):
            return None

    # -- retention ----------------------------------------------------------
    def prune(self, retention, today):
        """Apply retention settings (0 = keep forever)."""
        removed = []
        frames_days = retention.get("frames_days", 0)
        if frames_days > 0:
            # Safety net for days whose timelapse never got built.
            cutoff = today - timedelta(days=frames_days)
            for day in self.frame_days():
                if date.fromisoformat(day) < cutoff:
                    self.delete_frames(day)
                    removed.append(f"frames/{day}")
        readings_days = retention.get("readings_days", 0)
        if readings_days > 0:
            cutoff = today - timedelta(days=readings_days)
            for p in self.readings.glob("*.*"):
                if date.fromisoformat(p.name[:10]) < cutoff:
                    p.unlink()
                    removed.append(f"readings/{p.name}")
        tl_days = retention.get("timelapses_days", 0)
        if tl_days > 0:
            cutoff = today - timedelta(days=tl_days)
            for meta in self.timelapse_list():
                if date.fromisoformat(meta["date"]) < cutoff:
                    for ext in (".mp4", ".jpg", ".json"):
                        (self.timelapses / f"{meta['date']}{ext}").unlink(missing_ok=True)
                    removed.append(f"timelapses/{meta['date']}")
        return removed


def stamp_to_datetime(name):
    return datetime.strptime(Path(name).stem, STAMP_FMT).replace(tzinfo=timezone.utc)
