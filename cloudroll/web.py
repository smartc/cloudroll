"""Flask web UI: live image, recent captures, timelapse gallery, day frame browser."""

import csv
import io
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_from_directory, url_for)
from PIL import Image

from . import __version__
from .render import thermal_with_scale
from .store import SCALAR_FIELDS, stamp_to_datetime
from .sun import window_bounds, window_date_for

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
FRAME_RE = re.compile(r"^\d{8}T\d{6}Z\.jpg$")
SOURCE_RE = re.compile(r"^\d{8}T\d{6}Z\.(jpg|png|webp)$")
SOURCE_WIDTHS = (320, 480, 640, 800, 960, 1280)
TL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(\.[a-z0-9_-]+)?\.(mp4|jpg)$")
GRID_ROWS, GRID_COLS = 24, 32
READINGS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl(\.gz)?$")


def _blank(v):
    return "" if v is None else v


def month_nav(dates, requested):
    """Group ISO dates by month -> (month, months newest-first, counts, newer, older)."""
    counts = {}
    for d in dates:
        counts[d[:7]] = counts.get(d[:7], 0) + 1
    months = sorted(counts, reverse=True)
    month = requested or (months[0] if months else None)
    if month and not MONTH_RE.match(month):
        abort(404)
    i = months.index(month) if month in months else -1
    newer = months[i - 1] if i > 0 else None
    older = months[i + 1] if 0 <= i < len(months) - 1 else None
    return {"month": month, "months": months, "counts": counts, "newer": newer, "older": older}


def create_app(cfg, loc, store, worker, sensor, settings):
    app = Flask(__name__)
    offset = cfg["timelapse"]["offset_after_sunrise_min"]
    labels = {s["name"]: s.get("label") or s["name"].title() for s in cfg["sources"]}
    layouts = [{"name": n, "label": labels[n]} for n in cfg["layouts"]]
    rigs = cfg["observatory"]["rigs"]
    roof = cfg["observatory"].get("roof") or {}
    roof_link = [{"label": roof.get("label") or "Roof", "url": roof["link"]}] if roof.get("link") else []
    rig_labels = {r["name"]: r.get("label") or r["name"].title() for r in rigs}
    pier_labels = {r["name"]: (r["park_sensor"].get("label") or "Pier")
                   for r in rigs if r.get("park_sensor")}
    # Observatory lanes for the Data page: roof, then each rig's pier and NINA state.
    obs_rows = ([{"key": "roof", "label": "Roof", "kind": "roof"}] if rigs or
                cfg["observatory"].get("roof") else [])
    for r in rigs:
        n = r["name"]
        if n in pier_labels:
            obs_rows.append({"key": f"pier_{n}", "label": pier_labels[n], "kind": "pier"})
        obs_rows.append({"key": f"rig_{n}", "label": rig_labels[n], "kind": "rig",
                         "detail": f"rigd_{n}"})
    # Per-rig CSV columns: name suffix -> how to read it from the record's rig entry.
    RIG_CSV = [("pier", lambda e: (e.get("pier") or {}).get("status")),
               ("status", lambda e: e.get("status")),
               ("detail", lambda e: e.get("detail")),
               ("ra_h", lambda e: e.get("ra")), ("dec_deg", lambda e: e.get("dec")),
               ("alt_deg", lambda e: e.get("alt")), ("az_deg", lambda e: e.get("az")),
               ("pa_deg", lambda e: e.get("pa")),
               ("marker", lambda e: None if "marker" not in e else int(bool(e["marker"])))]

    def req_layout():
        """?layout= if it names a configured layout, else None."""
        lay = request.args.get("layout")
        return lay if lay in cfg["layouts"] else None

    def videos(meta):
        """{layout key: video info} for a day; older metadata has only the main video."""
        if meta.get("videos"):
            return meta["videos"]
        return {"classic": {"mp4": f"{meta['date']}.mp4", "jpg": f"{meta['date']}.jpg",
                            "size_bytes": meta.get("size_bytes")}}

    def check_day(day):
        if not DAY_RE.match(day):
            abort(404)

    def local_time(name):
        return stamp_to_datetime(name).astimezone(loc.tz)

    def static_url(filename):
        """Static file URL with its modification time, so browsers never run
        a stale script or stylesheet after an update."""
        try:
            v = int((Path(app.static_folder) / filename).stat().st_mtime)
        except OSError:
            v = __version__
        return url_for("static", filename=filename, v=v)

    cors_origins = set(cfg["web"].get("cors_origins") or [])

    @app.after_request
    def cors(resp):
        """Let configured origins read the JSON API and load static/ha/cloudroll-card.js
        (module scripts need CORS) from their own pages."""
        origin = request.headers.get("Origin")
        if origin in cors_origins and request.path.startswith(("/api/", "/static/")) \
                and request.method in ("GET", "HEAD", "OPTIONS"):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers.add("Vary", "Origin")
        return resp

    @app.context_processor
    def globals_():
        return {"title": cfg["web"]["title"], "location": loc, "version": __version__,
                "static_url": static_url,
                "sensor_url": sensor.web_url(), "layouts": layouts,
                "source_links": [{"label": s.get("label") or s["name"].title(), "url": s["link"]}
                                 for s in cfg["sources"] if s.get("link")] + roof_link,
                "layout_cfg": {"layouts": cfg["layouts"],
                               "default": settings.dashboard_default}}

    @app.template_filter("hm")
    def hm(iso):
        return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "--"

    @app.template_filter("nicedate")
    def nicedate(day):
        return datetime.fromisoformat(day).strftime("%a %b %-d, %Y")

    @app.template_filter("monthname")
    def monthname(month):
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")

    @app.template_filter("shortmonth")
    def shortmonth(month):
        return datetime.strptime(month, "%Y-%m").strftime("%b %Y")

    # -- pages --------------------------------------------------------------
    @app.route("/live")
    def live():
        recent = [{"day": d, "name": n, "time": local_time(n).strftime("%H:%M")}
                  for d, n in store.recent_frames(12, settings.dashboard_default)]
        current = window_date_for(loc, datetime.now(loc.tz), offset)
        start, end = window_bounds(loc, current, offset)
        return render_template("index.html", latest=store.latest_meta(), recent=recent,
                               timelapses=store.timelapse_list()[:4], worker=worker,
                               window_start=start, window_end=end, offset=offset,
                               interval=cfg["capture"]["interval_sec"],
                               settings=settings.as_dict(), labels=labels,
                               rig_labels=rig_labels,
                               has_markers=bool(worker.observatory and worker.observatory.rigs))

    @app.route("/timelapses")
    def timelapses():
        all_tl = store.timelapse_list()
        nav = month_nav([t["date"] for t in all_tl], request.args.get("month"))
        in_month = sorted((t for t in all_tl if t["date"].startswith(nav["month"] or "")),
                          key=lambda t: t["date"], reverse=True)
        built = {t["date"] for t in all_tl}
        pending = sorted(set(store.frame_days()) - built, reverse=True)
        return render_template("timelapses.html", timelapses=in_month, pending=pending,
                               endpoint="timelapses", **nav)

    @app.route("/")
    def data_latest():
        """Home page: the most recent day's data, following new days as they start."""
        days = store.readings_days()
        if not days:
            return data_index()
        return data_day(days[0], follow=True)

    @app.route("/data")
    def data_index():
        nav = month_nav(store.readings_days(), request.args.get("month"))
        days = [d for d in store.readings_days() if d.startswith(nav["month"] or "-")]
        summaries = [s for s in (store.readings_summary(d) for d in sorted(days, reverse=True)) if s]
        return render_template("data.html", summaries=summaries, endpoint="data_index",
                               fields=SCALAR_FIELDS, **nav)

    @app.route("/data/<day>")
    def data_day(day, follow=False):
        check_day(day)
        summary = store.readings_summary(day)
        if not summary:
            abort(404)
        days = sorted(store.readings_days())
        i = days.index(day)
        return render_template("data_day.html", day=day, s=summary,
                               prev_day=days[i - 1] if i > 0 else None,
                               next_day=days[i + 1] if i < len(days) - 1 else None,
                               has_timelapse=store.timelapse_path(day).exists(),
                               readings=store.readings_file(day), follow=follow,
                               poll_sec=min(15, cfg["capture"]["interval_sec"]),
                               cams=[{"name": n, "label": lb,
                                      "aspect": round(worker.source_aspect(n), 4)}
                                     for n, lb in labels.items()],
                               frames_days=cfg["retention"]["frames_days"],
                               obs_rows=obs_rows,
                               marked_source=(worker.observatory.allsky
                                              if worker.observatory and worker.observatory.lens
                                              else None))

    @app.route("/timelapse/<day>")
    def timelapse(day):
        check_day(day)
        meta = store.timelapse_meta(day)
        if not meta or not store.timelapse_path(day).exists():
            abort(404)
        vids = videos(meta)
        has_frames = store.has_frames(day)
        missing = [lay for lay in layouts if lay["name"] not in vids] if has_frames else []
        tabs = [{"key": k, "label": labels.get(k, "Classic"), **v} for k, v in vids.items()]
        return render_template("timelapse.html", meta=meta, tabs=tabs, missing=missing,
                               has_frames=has_frames, readings=store.readings_file(day))

    @app.route("/day/<day>")
    def day_view(day):
        check_day(day)
        frames = store.frames_for(day, settings.dashboard_default)
        if not frames:
            abort(404)
        start, end = window_bounds(loc, datetime.fromisoformat(day).date(), offset)
        return render_template("day.html", day=day, count=len(frames),
                               window_start=start, window_end=end,
                               has_timelapse=store.timelapse_path(day).exists(),
                               readings=store.readings_file(day))

    # -- API ----------------------------------------------------------------
    @app.route("/api/latest")
    def api_latest():
        meta = store.latest_meta()
        # Right after a restart the worker has no status yet: use the last record's.
        obs = worker.last_observatory
        if obs is None and meta and meta.get("observatory"):
            obs = {**meta["observatory"], "time": meta.get("time")}
        return jsonify({"meta": meta,
                        "last_error": worker.last_error,
                        "sources": worker.last_sources,
                        "observatory": obs})

    @app.route("/api/info")
    def api_info():
        """What a remote viewer (the Home Assistant card) needs to browse the data."""
        return jsonify({"title": cfg["web"]["title"], "version": __version__,
                        "location": {"name": loc.name, "latitude": loc.latitude,
                                     "longitude": loc.longitude},
                        "tz": loc.tz.key, "days": store.readings_days(),
                        "cams": [{"name": n, "label": lb,
                                  "aspect": round(worker.source_aspect(n), 4)}
                                 for n, lb in labels.items()],
                        "frames_days": cfg["retention"]["frames_days"],
                        "sensor_url": sensor.web_url(),
                        "poll_sec": min(15, cfg["capture"]["interval_sec"])})

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "GET":
            return jsonify(settings.as_dict())
        try:
            return jsonify(settings.update(request.get_json(force=True) or {}))
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/frames/<day>")
    def api_frames(day):
        check_day(day)
        lay = req_layout() or settings.dashboard_default
        return jsonify([{"name": n, "time": local_time(n).strftime("%H:%M"),
                         "url": url_for("media_frame", day=day, name=n, layout=lay)}
                        for n in store.frames_for(day, lay)])

    @app.route("/api/readings/<day>.json")
    def api_readings_series(day):
        """Column-oriented scalar series for the charts (no matrices).
        ?since=N returns only records N.. so a live page can poll cheaply."""
        check_day(day)
        lines = store.reading_lines(day)
        if not lines:
            abort(404)
        since = max(0, request.args.get("since", 0, type=int))
        p = store.readings_file(day)
        out = {"since": since, "count": len(lines), "open": p.suffix != ".gz",
               "latest_day": store.readings_days()[0],
               "t": [], "grid": [], "frame": [], **{k: [] for k in SCALAR_FIELDS},
               **{f"src_{n}": [] for n in labels},
               **{row["key"]: [] for row in obs_rows},
               **{row["detail"]: [] for row in obs_rows if row.get("detail")}}
        for line in lines[since:]:
            rec = json.loads(line)
            out["t"].append(datetime.fromisoformat(rec["time"]).timestamp() * 1000)
            out["grid"].append(rec.get("pixels") is not None)
            out["frame"].append(rec.get("frame"))
            src = rec.get("sources") or {}
            for n in labels:
                out[f"src_{n}"].append(src.get(n))
            r = rec.get("readings", {})
            for k in SCALAR_FIELDS:
                out[k].append(r.get(k))
            if obs_rows:
                obs = rec.get("observatory") or {}
                out["roof"].append(obs.get("roof"))
                for n in rig_labels:
                    e = (obs.get("rigs") or {}).get(n) or {}
                    if n in pier_labels:
                        out[f"pier_{n}"].append((e.get("pier") or {}).get("status"))
                    out[f"rig_{n}"].append(e.get("status"))
                    out[f"rigd_{n}"].append(e.get("detail"))
        return jsonify(out)

    @app.route("/api/readings/<day>/<int:index>.json")
    def api_reading(day, index):
        check_day(day)
        rec = store.reading(day, index)
        if rec is None:
            abort(404)
        return jsonify(rec)

    @app.route("/api/readings/<day>/<int:index>.png")
    def api_reading_image(day, index):
        """Re-render a record's stored temperature grid with its colour scale."""
        check_day(day)
        rec = store.reading(day, index)
        if rec is None or rec.get("pixels") is None:
            abort(404)
        img = thermal_with_scale({"pixels": rec["pixels"], "ambient": rec["ambient"]},
                                 rec["color_scale"])
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=False)
        resp = Response(buf.getvalue(), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.route("/api/readings/<day>.csv")
    def api_readings_csv(day):
        """Readings for a day as CSV, one row per capture.  With an observatory
        configured, the roof state and each rig's pier / NINA status, pointing and
        marker follow the readings.  ?grid=1 appends the frame ambient/sky temps
        and the 32×24 matrix as columns r00c00..r23c31 (blank for records captured
        before grids were stored)."""
        check_day(day)
        if store.readings_file(day) is None:
            abort(404)
        grid = request.args.get("grid") == "1"
        grid_cols = [f"r{r:02d}c{c:02d}" for r in range(GRID_ROWS) for c in range(GRID_COLS)]

        def rows():
            buf = io.StringIO()
            w = csv.writer(buf)
            obs_cols = (["roof"] + [f"{n}_{c}" for n in rig_labels for c, _ in RIG_CSV]
                        if obs_rows else [])
            w.writerow(["time"] + SCALAR_FIELDS + obs_cols +
                       (["frame_ambient", "frame_sky"] + grid_cols if grid else []))
            for rec in store.iter_readings(day):
                r = rec.get("readings", {})
                row = [rec["time"]] + ["" if r.get(k) is None else r[k] for k in SCALAR_FIELDS]
                if obs_rows:
                    obs = rec.get("observatory") or {}
                    row.append(_blank(obs.get("roof")))
                    for n in rig_labels:
                        e = (obs.get("rigs") or {}).get(n) or {}
                        row += [_blank(get(e)) for _, get in RIG_CSV]
                if grid:
                    px = rec.get("pixels")
                    row += [_blank(rec.get("ambient")), _blank(rec.get("sky"))]
                    row += [v for line in px for v in line] if px else [""] * len(grid_cols)
                w.writerow(row)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
            yield buf.getvalue()

        name = f"cloudroll-{day}{'-grids' if grid else ''}.csv"
        return Response(rows(), mimetype="text/csv", headers={
            "Content-Disposition": f"attachment; filename={name}"})

    @app.route("/api/readings/<day>/<int:index>.csv")
    def api_reading_grid_csv(day, index):
        """One record's temperature grid as a 24-row × 32-column CSV (°C)."""
        check_day(day)
        rec = store.reading(day, index)
        if rec is None or rec.get("pixels") is None:
            abort(404)
        buf = io.StringIO()
        csv.writer(buf).writerows(rec["pixels"])
        stamp = datetime.fromisoformat(rec["time"]).strftime("%Y%m%d-%H%M%S")
        return Response(buf.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f"attachment; filename=cloudroll-grid-{stamp}.csv"})

    @app.route("/api/build/<day>", methods=["POST"])
    def api_build(day):
        check_day(day)
        if not store.has_frames(day):
            abort(404)
        lay = request.args.get("layout")
        if lay and lay not in cfg["layouts"] and lay != "both":
            abort(400)
        threading.Thread(target=worker.build, args=(day, lay), daemon=True).start()
        return jsonify({"status": "started", "day": day, "layout": lay})

    # -- media --------------------------------------------------------------
    @app.route("/media/latest.jpg")
    def media_latest():
        lay = req_layout()
        name = f"latest-{lay}.jpg" if lay and (store.root / f"latest-{lay}.jpg").exists() \
            else "latest.jpg"
        resp = send_from_directory(store.root, name, max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/media/frames/<day>/<name>")
    def media_frame(day, name):
        check_day(day)
        if not FRAME_RE.match(name):
            abort(404)
        path = store.frame_path(day, name, req_layout())
        if path is None:
            abort(404)
        return send_from_directory(path.parent, name, max_age=86400)

    def record_observatory(day, frame):
        """(capture time, "observatory" field) of the record whose frame is
        `frame`, or None.  Searches the day's cached lines from the end."""
        needles = (f'"frame":"{frame}"', f'"frame": "{frame}"')   # compact as stored, or spaced
        for line in reversed(store.reading_lines(day) or []):
            if any(n in line for n in needles):
                rec = json.loads(line)
                if rec.get("observatory"):
                    return datetime.fromisoformat(rec["time"]), rec["observatory"]
                return None
        return None

    @app.route("/media/sources/<day>/<source>/<name>")
    def media_source(day, source, name):
        """A source's original image as fetched (kept for retention.frames_days)."""
        check_day(day)
        if source not in labels or not SOURCE_RE.match(name):
            abort(404)
        folder = store.sources / day / source
        path = folder / name
        if not path.is_file():
            # Records only know the stamp; the source may have been a PNG/WebP.
            path = next(iter(sorted(folder.glob(Path(name).stem + ".*"))), None)
            if path is None or not SOURCE_RE.match(path.name):
                abort(404)
        w = request.args.get("w", type=int)
        # ?markers=1 on the allsky source: telescope markers drawn from the
        # matching record (the stored original stays unmarked).
        obs = worker.observatory
        rec_obs = None
        if request.args.get("markers") and obs and obs.lens and source == obs.allsky:
            rec_obs = record_observatory(day, Path(name).stem + ".jpg")
        if not w and rec_obs is None:
            return send_from_directory(folder, path.name, max_age=86400)
        # Downscaled copy for the data page: originals are up to 4K / ~600 KB.
        if w:
            w = min(SOURCE_WIDTHS, key=lambda v: abs(v - w))
        with Image.open(path) as im:
            if w:
                im.draft("RGB", (w, w))
            im = im.convert("RGB")
            if rec_obs is not None:
                rec_time, rec_o = rec_obs
                im = obs.draw(im, obs.state_from_record(rec_o), rec_time,
                              rec_o.get("style") or settings.scope_marker)
            if w and im.width > w:
                im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        resp = Response(buf.getvalue(), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.route("/media/readings/<name>")
    def media_readings(name):
        if not READINGS_RE.match(name):
            abort(404)
        return send_from_directory(store.readings, name, as_attachment=True, max_age=0)

    @app.route("/media/timelapses/<name>")
    def media_timelapse(name):
        if not TL_RE.match(name):
            abort(404)
        return send_from_directory(store.timelapses, name, max_age=3600, conditional=True)

    return app
