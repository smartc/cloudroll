"""Background capture loop and timelapse scheduler."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from PIL import Image

from .render import compose_frame, compose_layout
from .sun import window_bounds, window_date_for
from .timelapse import build_timelapse

log = logging.getLogger(__name__)

HOUSEKEEP_EVERY_SEC = 600
# Grace period after a window closes before finalising it, so a capture that
# started just before the boundary has finished writing to the old day.
FINALISE_GRACE = timedelta(minutes=2)


def round_to_second(dt):
    """Round a datetime to the nearest whole second."""
    return (dt + timedelta(microseconds=500_000)).replace(microsecond=0)


class Worker:
    def __init__(self, cfg, loc, store, sensor, sources, settings, observatory=None):
        self.cfg, self.loc, self.store, self.sensor = cfg, loc, store, sensor
        self.sources, self.settings = sources, settings
        self.observatory = observatory if observatory and observatory.enabled else None
        self.last_observatory = None
        self.offset = cfg["timelapse"]["offset_after_sunrise_min"]
        self.last_error = None
        self.last_capture = None
        self.last_sources = {}
        self._stop = threading.Event()
        self._build_lock = threading.Lock()
        # Width/height of each source's last good image, so a missing image keeps
        # its usual shape in the layout.  Seeded from config "aspect" if given.
        self._aspects = {s["name"]: s["aspect"] for s in cfg["sources"] if s.get("aspect")}
        n = len(cfg["sources"]) + (1 if self.observatory else 0)
        self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="source") if n else None

    def source_aspect(self, name):
        """Width/height of a source's images: last seen, configured, else read
        from the newest stored image (e.g. right after a restart), else 16:9."""
        if name not in self._aspects:
            latest = self.store.latest_source(name)
            if latest is None:
                return 16 / 9
            try:
                with Image.open(latest) as im:   # reads the header only
                    self._aspects[name] = im.width / im.height
            except OSError:
                return 16 / 9
        return self._aspects[name]

    def color_scale(self):
        cs = self.cfg["color_scale"]
        if cs["mode"] == "sensor":
            try:
                return self.sensor.sensor_color_scale()
            except Exception as e:  # noqa: BLE001 – fall back to configured values
                log.warning("Could not read colour scale from sensor (%s); using config", e)
                return {**cs, "mode": "fixed"}
        return cs

    def capture_once(self):
        now = round_to_second(datetime.now(timezone.utc))
        # Cameras are fetched in parallel with the sensor reads; the observatory
        # snapshot alone can take ~5 s.
        futures = [self._pool.submit(self.sources.fetch, src) for src in self.cfg["sources"]]
        obs_f = self._pool.submit(self.observatory.read, now) if self.observatory else None
        matrix = self.sensor.thermal_matrix()
        readings = self.sensor.readings()
        scale = self.color_scale()
        panels = [f.result() for f in futures]
        obs = obs_f.result() if obs_f else None
        if obs:
            panels = [self._mark(p, obs, now) for p in panels]
        local = now.astimezone(self.loc.tz)
        wdate = window_date_for(self.loc, now, self.offset)
        title = self.cfg["web"]["title"]
        if panels:
            for p in panels:
                if p.data:
                    self.store.save_source(wdate, now, p.name, p.data, p.ext)
                if p.image is not None:
                    self._aspects[p.name] = p.image.width / p.image.height
            images = {layout: compose_layout(layout, panels, self._aspects, matrix, readings,
                                             scale, local, self.loc.name, title)
                      for layout in self.cfg["layouts"]}
        else:
            images = {None: compose_frame(matrix, readings, scale, local, self.loc.name, title)}
        paths = self.store.save_frame(wdate, now, images, self.cfg["capture"]["jpeg_quality"],
                                      latest=self.settings.dashboard_default)
        path = next(iter(paths.values()))
        record = {
            "time": local.isoformat(), "frame": path.name, "window": wdate.isoformat(),
            "readings": readings, "color_scale": scale,
            "ambient": matrix["ambient"], "sky": matrix["sky"], "pixels": matrix["pixels"],
        }
        if obs:
            record["observatory"] = obs.as_dict()
            self.last_observatory = {**obs.as_dict(), "time": local.isoformat()}
        if panels:
            record["sources"] = {p.name: p.status for p in panels}
            self.last_sources = {p.name: {"status": p.status, "seconds": round(p.seconds, 2)}
                                 for p in panels}
        self.store.append_reading(wdate, record)
        self.last_capture, self.last_error = local, None
        log.debug("Captured %s", path)
        return path

    def _mark(self, panel, obs, when):
        """The allsky panel with telescope markers and roof/rig status badges;
        other panels unchanged.  The archived original stays unmarked."""
        o = self.observatory
        if panel.name != o.allsky:
            return panel
        src = next((s for s in self.cfg["sources"] if s["name"] == panel.name), {})
        image = panel.image
        if image is not None:
            try:
                image = o.draw(image, obs, when, self.settings.scope_marker, src.get("crop"))
            except Exception:  # noqa: BLE001 – a drawing bug must not lose the capture
                log.exception("Drawing telescope markers failed")
        return replace(panel, image=image, badges=o.badges(obs))

    def layouts_to_build(self, layout=None):
        """Layouts to encode: an explicit one, 'both'/'all', or the runtime setting.
        [None] means the classic single layout (no image sources)."""
        if not self.cfg["layouts"]:
            return [None]
        if layout in ("both", "all"):
            return list(self.cfg["layouts"])
        if layout:
            if layout not in self.cfg["layouts"]:
                raise ValueError(f"unknown layout {layout!r}")
            return [layout]
        return self.settings.timelapse_layouts

    def build_due(self):
        """Finalise closed windows: build the timelapse(s) and compress the
        readings.  Frames and source images stay until retention prunes them."""
        now = datetime.now(self.loc.tz)
        with self._build_lock:
            days = set(self.store.frame_days())
            days |= {p.name[:10] for p in self.store.readings.glob("*.jsonl")}
            for day in sorted(days):
                _, end = window_bounds(self.loc, datetime.fromisoformat(day).date(), self.offset)
                if now < end + FINALISE_GRACE:
                    continue
                meta = self.store.timelapse_meta(day)
                done = meta and meta.get("complete") and self.store.timelapse_path(day).exists()
                if not done and self.store.has_frames(day):
                    for layout in self.layouts_to_build():
                        build_timelapse(self.store, self.loc, self.cfg, day, layout)
                    log.info("Finalised %s", day)
                self.store.compress_readings(day)

    def build(self, day, layout=None):
        """Build one day's timelapse in the given layout(s); returns the metadata."""
        layouts = self.layouts_to_build(layout)
        with self._build_lock:
            meta = None
            for lay in layouts:
                meta = build_timelapse(self.store, self.loc, self.cfg, day, lay) or meta
            return meta

    def housekeep(self):
        try:
            self.build_due()
            r = self.cfg["retention"]
            removed = self.store.prune(r, datetime.now(self.loc.tz).date())
            if removed:
                log.info("Pruned: %s", ", ".join(removed))
        except Exception:  # noqa: BLE001
            log.exception("Housekeeping failed")

    def run(self):
        interval = self.cfg["capture"]["interval_sec"]
        next_house = 0.0
        slot = None
        while not self._stop.is_set():
            # Wait for the next slot on the interval grid (e.g. the top of the
            # next minute) – including at startup, so restarts don't add
            # off-schedule captures.  Tracking the slot guarantees one capture
            # per slot even if the timer wakes a hair early.
            now = time.time()
            nxt = (now // interval + 1) * interval
            if slot is not None and nxt <= slot:
                nxt = slot + interval
            if self._stop.wait(nxt - now):
                break
            slot = nxt
            try:
                self.capture_once()
            except Exception as e:  # noqa: BLE001 – keep looping through sensor outages
                self.last_error = f"{datetime.now(self.loc.tz):%H:%M:%S} {e}"
                log.warning("Capture failed: %s", e)
            if time.monotonic() >= next_house:
                threading.Thread(target=self.housekeep, daemon=True).start()
                next_house = time.monotonic() + HOUSEKEEP_EVERY_SEC

    def start(self):
        threading.Thread(target=self.run, name="capture", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.observatory:
            self.observatory.stop()
        if self._pool:
            self._pool.shutdown(wait=False)
