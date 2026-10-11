"""Build a day's timelapse MP4 from its captured frames with ffmpeg."""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone

from PIL import Image

from .store import stamp_to_datetime
from .sun import window_bounds

log = logging.getLogger(__name__)


def build_timelapse(store, loc, cfg, day, layout=None):
    """Encode a day's frames in one layout to MP4.  Returns the day's metadata or None.

    The first video built for a day is its main one (<day>.mp4/.jpg, whose
    details are the metadata's top-level fields); other layouts are written
    as <day>.<layout>.mp4/.jpg.  Every video is listed under "videos"."""
    tl = cfg["timelapse"]
    frames = store.frames_for(day, layout)
    label = f"{day}" + (f" [{layout}]" if layout else "")
    if len(frames) < 2:
        log.info("Timelapse %s: only %d frame(s), skipping", label, len(frames))
        return None

    meta = store.timelapse_meta(day) or {}
    key = layout or "classic"
    main_key = meta.get("layout") or "classic"
    is_main = (not meta or main_key == key or not store.timelapse_path(day).exists())
    stem = day if is_main else f"{day}.{layout}"
    out = store.timelapses / f"{stem}.mp4"
    tmp = store.timelapses / f"{stem}.tmp.mp4"
    list_file = store.timelapses / f"{stem}.ffconcat"
    paths = [store.frame_path(day, name, layout) for name in frames]
    dur = 1.0 / tl["fps"]
    with open(list_file, "w") as f:
        f.write("ffconcat version 1.0\n")
        for path in paths:
            f.write(f"file '{path}'\nduration {dur:.6f}\n")
        f.write(f"file '{paths[-1]}'\n")

    # Scale/pad everything to the last frame's size: a day can mix frame sizes
    # (e.g. the day image sources were first configured).
    with Image.open(paths[-1]) as im:
        w, h = im.size
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x1a1a2e,fps={tl['fps']},format=yuv420p")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", str(tl["crf"]),
           "-movflags", "+faststart", str(tmp)]
    log.info("Timelapse %s: encoding %d frames at %dx%d", label, len(frames), w, h)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg failed for %s: %s", label, e.stderr.strip())
        tmp.unlink(missing_ok=True)
        return None
    finally:
        list_file.unlink(missing_ok=True)
    tmp.replace(out)
    shutil.copyfile(paths[len(paths) // 2], store.timelapses / f"{stem}.jpg")

    start, end = window_bounds(loc, datetime.fromisoformat(day).date(),
                               tl["offset_after_sunrise_min"])
    now = datetime.now(timezone.utc)
    video = {
        "mp4": out.name,
        "jpg": f"{stem}.jpg",
        "frames": len(frames),
        "width": w,
        "height": h,
        "duration_sec": round(len(frames) / tl["fps"], 1),
        "size_bytes": out.stat().st_size,
        "built": now.isoformat(),
        "complete": datetime.now(loc.tz) >= end,
    }
    if is_main:
        meta.update({
            "date": day,
            "layout": layout,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "first_frame": stamp_to_datetime(frames[0]).astimezone(loc.tz).isoformat(),
            "last_frame": stamp_to_datetime(frames[-1]).astimezone(loc.tz).isoformat(),
            "frames": len(frames),
            "fps": tl["fps"],
            "duration_sec": video["duration_sec"],
            "size_bytes": video["size_bytes"],
            "built": video["built"],
            "complete": video["complete"],
        })
    meta.setdefault("videos", {})[key] = video
    (store.timelapses / f"{day}.json").write_text(json.dumps(meta, indent=2))
    log.info("Timelapse %s: %s (%.1f s, %.1f MB)", label, out.name,
             video["duration_sec"], video["size_bytes"] / 1e6)
    return meta
