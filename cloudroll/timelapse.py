"""Build a day's timelapse MP4 from its captured frames with ffmpeg."""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone

from .store import stamp_to_datetime
from .sun import window_bounds

log = logging.getLogger(__name__)


def build_timelapse(store, loc, cfg, day):
    """Encode frames/<day>/*.jpg to timelapses/<day>.mp4.  Returns metadata or None."""
    tl = cfg["timelapse"]
    frames = store.frames_for(day)
    if len(frames) < 2:
        log.info("Timelapse %s: only %d frame(s), skipping", day, len(frames))
        return None

    src_dir = store.frames / day
    out = store.timelapse_path(day)
    tmp = out.with_suffix(".tmp.mp4")
    list_file = store.timelapses / f"{day}.ffconcat"
    dur = 1.0 / tl["fps"]
    with open(list_file, "w") as f:
        f.write("ffconcat version 1.0\n")
        for name in frames:
            f.write(f"file '{src_dir / name}'\nduration {dur:.6f}\n")
        f.write(f"file '{src_dir / frames[-1]}'\n")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-vf", f"fps={tl['fps']},format=yuv420p",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(tl["crf"]),
           "-movflags", "+faststart", str(tmp)]
    log.info("Timelapse %s: encoding %d frames", day, len(frames))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg failed for %s: %s", day, e.stderr.strip())
        tmp.unlink(missing_ok=True)
        return None
    finally:
        list_file.unlink(missing_ok=True)
    tmp.replace(out)

    shutil.copyfile(src_dir / frames[len(frames) // 2], store.timelapses / f"{day}.jpg")

    start, end = window_bounds(loc, datetime.fromisoformat(day).date(),
                               tl["offset_after_sunrise_min"])
    meta = {
        "date": day,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "first_frame": stamp_to_datetime(frames[0]).astimezone(loc.tz).isoformat(),
        "last_frame": stamp_to_datetime(frames[-1]).astimezone(loc.tz).isoformat(),
        "frames": len(frames),
        "fps": tl["fps"],
        "duration_sec": round(len(frames) / tl["fps"], 1),
        "size_bytes": out.stat().st_size,
        "built": datetime.now(timezone.utc).isoformat(),
        "complete": datetime.now(loc.tz) >= end,
    }
    (store.timelapses / f"{day}.json").write_text(json.dumps(meta, indent=2))
    log.info("Timelapse %s: %s (%.1f s, %.1f MB)", day, out.name,
             meta["duration_sec"], meta["size_bytes"] / 1e6)
    return meta
