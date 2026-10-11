"""On-disk layout.

data/
  latest.jpg / latest.json              most recent capture
  latest-<layout>.jpg                   most recent capture in each layout (image sources only)
  frames/<window-date>/<UTC stamp>.jpg  composite frames, classic layout (no image sources)
  frames/<window-date>/<layout>/<UTC stamp>.jpg
                                        composite frames, one folder per layout
  sources/<window-date>/<source>/<UTC stamp>.<ext>
                                        each source's original image as fetched
  readings/<window-date>.jsonl          one JSON record per capture: readings + raw 32×24 matrix
  readings/<window-date>.jsonl.gz       same, gzipped once the window has closed
  timelapses/<window-date>.mp4          finished day timelapse
  timelapses/<window-date>.jpg          poster (middle frame)
  timelapses/<window-date>.<layout>.mp4/.jpg
                                        further layouts of the same day
  timelapses/<window-date>.json         metadata

Frames and sources are kept for retention.frames_days, independent of the
timelapse build, so a day can be browsed or re-encoded in another layout.
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
        self.sources = self.root / "sources"
        for d in (self.frames, self.readings, self.timelapses, self.sources):
            d.mkdir(parents=True, exist_ok=True)
        self._lines_cache = {}          # day -> (stat key, [raw JSON lines])
        self._summary_cache = {}        # day -> (stat key, summary)
        self._cache_lock = threading.Lock()

    # -- frames -------------------------------------------------------------
    def save_frame(self, window_date, when, images, quality, latest=None):
        """Save one capture's composites.  images maps layout -> PIL image, with
        layout None for the classic single-layout frame.  latest.jpg gets the
        `latest` layout (or the only one)."""
        day_dir = self.frames / window_date.isoformat()
        stamp = when.astimezone(timezone.utc).strftime(STAMP_FMT)
        paths = {}
        for layout, img in images.items():
            d = day_dir / layout if layout else day_dir
            d.mkdir(parents=True, exist_ok=True)
            jpg = d / f"{stamp}.jpg"
            tmp = jpg.with_suffix(".tmp")
            img.save(tmp, "JPEG", quality=quality)
            tmp.replace(jpg)
            paths[layout] = jpg
            if layout:
                self._copy_atomic(jpg, self.root / f"latest-{layout}.jpg")
        main = latest if latest in paths else next(iter(paths))
        self._copy_atomic(paths[main], self.root / "latest.jpg")
        return paths

    def _copy_atomic(self, src, dst):
        tmp = dst.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)

    def save_source(self, window_date, when, name, data, ext="jpg"):
        d = self.sources / window_date.isoformat() / name
        d.mkdir(parents=True, exist_ok=True)
        stamp = when.astimezone(timezone.utc).strftime(STAMP_FMT)
        path = d / f"{stamp}.{ext}"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def latest_source(self, name):
        """Path of the newest stored image for a source, or None."""
        for day in sorted((p.name for p in self.sources.iterdir() if p.is_dir()), reverse=True):
            files = sorted((self.sources / day / name).glob("*.*"))
            if files:
                return files[-1]
        return None

    def frame_days(self):
        return sorted((p.name for p in self.frames.iterdir() if p.is_dir()), reverse=True)

    def frame_layouts(self, day):
        d = self.frames / day
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def frames_for(self, day, layout=None):
        """Frame names (UTC stamps) for a day in a layout.  Classic frames saved
        before image sources were configured count for every layout, so the
        switch-over day stays complete.  layout None = every frame of the day."""
        d = self.frames / day
        if not d.is_dir():
            return []
        names = {p.name for p in d.glob("*.jpg")}
        for sub in ([layout] if layout else self.frame_layouts(day)):
            names |= {p.name for p in (d / sub).glob("*.jpg")}
        return sorted(names)

    def frame_path(self, day, name, layout=None):
        """Where frame `name` lives: the layout's folder, else the classic file,
        else any layout that has it (None if nowhere)."""
        d = self.frames / day
        for sub in ([layout] if layout else []) + [""] + self.frame_layouts(day):
            p = d / sub / name if sub else d / name
            if p.is_file():
                return p
        return None

    def has_frames(self, day):
        return bool(self.frames_for(day))

    def delete_frames(self, day):
        shutil.rmtree(self.frames / day, ignore_errors=True)
        shutil.rmtree(self.sources / day, ignore_errors=True)

    def recent_frames(self, n, layout=None):
        out = []
        for day in self.frame_days():
            for name in reversed(self.frames_for(day, layout)):
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
    def timelapse_path(self, day, layout=None):
        """The day's main video, or the file for one layout (None if not built)."""
        if layout is None:
            return self.timelapses / f"{day}.mp4"
        meta = self.timelapse_meta(day) or {}
        video = (meta.get("videos") or {}).get(layout)
        return self.timelapses / video["mp4"] if video else None

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
            # Frames and source images are kept this long whether or not the
            # day's timelapse has been built.
            cutoff = today - timedelta(days=frames_days)
            days = set(self.frame_days()) | {p.name for p in self.sources.iterdir() if p.is_dir()}
            for day in sorted(days):
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
                    for p in self.timelapses.glob(f"{meta['date']}.*"):
                        p.unlink(missing_ok=True)
                    removed.append(f"timelapses/{meta['date']}")
        return removed


def stamp_to_datetime(name):
    return datetime.strptime(Path(name).stem, STAMP_FMT).replace(tzinfo=timezone.utc)
