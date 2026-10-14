"""Build a day's timelapse MP4 from its captured frames with ffmpeg.

On a night with an imaging run (the roof opening and later closing, as seen
by the observatory status), the run is also cut out of each day video as
<stem>.run.mp4.  Keyframes are forced at the run's first frame and just past
its last, so the cut is an exact stream copy instead of a second encode."""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from PIL import Image

from .store import stamp_to_datetime
from .sun import sunrise_on, window_bounds

log = logging.getLogger(__name__)

ROOF_OPEN = ("opening", "open")


def imaging_run(store, loc, cfg, day):
    """The night's imaging run, or None if the roof never opened.

    It runs from the roof first opening until it last reads closed; brief
    closures in between (a safety event) stay inside the run.  If it never
    closes, the run ends at sunrise, or at the last record with a known roof
    state if the controller went offline before then.  "ended" is False while
    the run may still be going (roof open and before sunrise)."""
    opened = closed = last_known = None
    for rec in store.iter_readings(day):
        roof = (rec.get("observatory") or {}).get("roof")
        t = datetime.fromisoformat(rec["time"])
        if roof in ROOF_OPEN:
            opened = opened or t
            closed = None
        elif roof == "closed" and opened and not closed:
            closed = t
        if roof not in (None, "unknown"):
            last_known = t
    if not opened:
        return None
    sunrise = sunrise_on(loc, datetime.fromisoformat(day).date() + timedelta(days=1))
    end = closed or min(sunrise, last_known)
    pad = timedelta(minutes=cfg["timelapse"]["run"]["pad_min"])
    return {
        "roof_open": opened.isoformat(),
        "roof_close": closed.isoformat() if closed else None,
        "start": (opened - pad).isoformat(),
        "end": (end + pad).isoformat(),
        "ended": bool(closed) or datetime.now(loc.tz) >= sunrise,
    }


def run_frames(frames, run):
    """(first, last) index into the day's frames covered by the run, or None."""
    start, end = datetime.fromisoformat(run["start"]), datetime.fromisoformat(run["end"])
    idx = [i for i, name in enumerate(frames) if start <= stamp_to_datetime(name) <= end]
    return (idx[0], idx[-1]) if len(idx) >= 2 else None


def cut_run(src, out, first, count, fps):
    """Stream-copy `count` frames starting at keyframe `first` out of `src`."""
    tmp = out.with_name(out.stem + ".tmp.mp4")
    half = 0.5 / fps
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           # Seeking half a frame in lands on the keyframe forced at `first`;
           # shift back by that half frame, or players drop the keyframe as
           # being before the start.
           "-ss", f"{first / fps + half:.6f}", "-i", str(src),
           "-frames:v", str(count), "-c", "copy", "-output_ts_offset", f"{half:.6f}",
           "-movflags", "+faststart", str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg cut failed for %s: %s", out.name, e.stderr.strip())
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out)
    return True


def build_timelapse(store, loc, cfg, day, layout=None):
    """Encode a day's frames in one layout to MP4.  Returns the day's metadata or None.

    The first video built for a day is its main one (<day>.mp4/.jpg, whose
    details are the metadata's top-level fields); other layouts are written
    as <day>.<layout>.mp4/.jpg.  Every video is listed under "videos"."""
    tl = cfg["timelapse"]
    fps = tl["fps"]
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

    # Only cut a run that's over: a mid-night build gets one at finalisation.
    run = imaging_run(store, loc, cfg, day) if tl["run"]["enabled"] else None
    span = run_frames(frames, run) if run and run["ended"] else None

    dur = 1.0 / fps
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
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x1a1a2e,fps={fps},format=yuv420p")
    keys = []
    if span:
        # Frame i is at i/fps; half a frame early so rounding can't skip it.
        keys = ["-force_key_frames",
                ",".join(f"{max(0, i - 0.5) / fps:.6f}" for i in (span[0], span[1] + 1))]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", str(tl["crf"]), *keys,
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
        "duration_sec": round(len(frames) / fps, 1),
        "size_bytes": out.stat().st_size,
        "built": now.isoformat(),
        "complete": datetime.now(loc.tz) >= end,
    }

    run_out = store.timelapses / f"{stem}.run.mp4"
    run_jpg = store.timelapses / f"{stem}.run.jpg"
    count = span[1] - span[0] + 1 if span else 0
    if span and cut_run(out, run_out, span[0], count, fps):
        shutil.copyfile(paths[(span[0] + span[1]) // 2], run_jpg)
        video["run"] = {
            "mp4": run_out.name,
            "jpg": run_jpg.name,
            "frames": count,
            "duration_sec": round(count / fps, 1),
            "size_bytes": run_out.stat().st_size,
        }
        log.info("Timelapse %s: imaging run %s (%d frames, %.1f MB)", label, run_out.name,
                 count, video["run"]["size_bytes"] / 1e6)
    else:
        run_out.unlink(missing_ok=True)
        run_jpg.unlink(missing_ok=True)

    if is_main:
        meta.update({
            "date": day,
            "layout": layout,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "first_frame": stamp_to_datetime(frames[0]).astimezone(loc.tz).isoformat(),
            "last_frame": stamp_to_datetime(frames[-1]).astimezone(loc.tz).isoformat(),
            "frames": len(frames),
            "fps": fps,
            "duration_sec": video["duration_sec"],
            "size_bytes": video["size_bytes"],
            "built": video["built"],
            "complete": video["complete"],
        })
        if "run" in video:
            meta["run"] = {
                **{k: run[k] for k in ("roof_open", "roof_close", "start", "end")},
                "first_frame": stamp_to_datetime(frames[span[0]]).astimezone(loc.tz).isoformat(),
                "last_frame": stamp_to_datetime(frames[span[1]]).astimezone(loc.tz).isoformat(),
                "frames": count,
                "duration_sec": video["run"]["duration_sec"],
            }
        else:
            meta.pop("run", None)
    meta.setdefault("videos", {})[key] = video
    (store.timelapses / f"{day}.json").write_text(json.dumps(meta, indent=2))
    log.info("Timelapse %s: %s (%.1f s, %.1f MB)", label, out.name,
             video["duration_sec"], video["size_bytes"] / 1e6)
    return meta
