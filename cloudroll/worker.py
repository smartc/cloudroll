"""Background capture loop and timelapse scheduler."""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .render import compose_frame
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
    def __init__(self, cfg, loc, store, sensor):
        self.cfg, self.loc, self.store, self.sensor = cfg, loc, store, sensor
        self.offset = cfg["timelapse"]["offset_after_sunrise_min"]
        self.last_error = None
        self.last_capture = None
        self._stop = threading.Event()
        self._build_lock = threading.Lock()

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
        matrix = self.sensor.thermal_matrix()
        readings = self.sensor.readings()
        scale = self.color_scale()
        local = now.astimezone(self.loc.tz)
        img = compose_frame(matrix, readings, scale, local, self.loc.name,
                            self.cfg["web"]["title"])
        wdate = window_date_for(self.loc, now, self.offset)
        path = self.store.save_frame(wdate, now, img, self.cfg["capture"]["jpeg_quality"])
        self.store.append_reading(wdate, {
            "time": local.isoformat(), "frame": path.name, "window": wdate.isoformat(),
            "readings": readings, "color_scale": scale,
            "ambient": matrix["ambient"], "sky": matrix["sky"], "pixels": matrix["pixels"],
        })
        self.last_capture, self.last_error = local, None
        log.debug("Captured %s", path)
        return path

    def build_due(self):
        """Finalise closed windows: build the timelapse, compress the readings,
        then drop the frame images (the video and raw readings are kept)."""
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
                if not done and self.store.frames_for(day):
                    meta = build_timelapse(self.store, self.loc, self.cfg, day)
                    done = meta and meta.get("complete")
                self.store.compress_readings(day)
                if done:
                    self.store.delete_frames(day)
                    log.info("Finalised %s: frames removed, readings kept", day)

    def build(self, day):
        with self._build_lock:
            return build_timelapse(self.store, self.loc, self.cfg, day)

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
