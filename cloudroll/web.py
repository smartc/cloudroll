"""Flask web UI: live image, recent captures, timelapse gallery, day frame browser."""

import csv
import io
import json
import re
import threading
from datetime import datetime

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_from_directory, url_for)

from . import __version__
from .render import thermal_with_scale
from .store import SCALAR_FIELDS, stamp_to_datetime
from .sun import window_bounds, window_date_for

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
FRAME_RE = re.compile(r"^\d{8}T\d{6}Z\.jpg$")
TL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.(mp4|jpg)$")
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


def create_app(cfg, loc, store, worker, sensor):
    app = Flask(__name__)
    offset = cfg["timelapse"]["offset_after_sunrise_min"]

    def check_day(day):
        if not DAY_RE.match(day):
            abort(404)

    def local_time(name):
        return stamp_to_datetime(name).astimezone(loc.tz)

    @app.context_processor
    def globals_():
        return {"title": cfg["web"]["title"], "location": loc, "version": __version__,
                "sensor_url": sensor.web_url()}

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
                  for d, n in store.recent_frames(12)]
        current = window_date_for(loc, datetime.now(loc.tz), offset)
        start, end = window_bounds(loc, current, offset)
        return render_template("index.html", latest=store.latest_meta(), recent=recent,
                               timelapses=store.timelapse_list()[:4], worker=worker,
                               window_start=start, window_end=end, offset=offset,
                               interval=cfg["capture"]["interval_sec"])

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
                               poll_sec=min(15, cfg["capture"]["interval_sec"]))

    @app.route("/timelapse/<day>")
    def timelapse(day):
        check_day(day)
        meta = store.timelapse_meta(day)
        if not meta or not store.timelapse_path(day).exists():
            abort(404)
        return render_template("timelapse.html", meta=meta,
                               has_frames=bool(store.frames_for(day)),
                               readings=store.readings_file(day))

    @app.route("/day/<day>")
    def day_view(day):
        check_day(day)
        frames = store.frames_for(day)
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
        return jsonify({"meta": store.latest_meta(),
                        "last_error": worker.last_error})

    @app.route("/api/frames/<day>")
    def api_frames(day):
        check_day(day)
        return jsonify([{"name": n, "time": local_time(n).strftime("%H:%M"),
                         "url": url_for("media_frame", day=day, name=n)}
                        for n in store.frames_for(day)])

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
               "t": [], "grid": [], **{k: [] for k in SCALAR_FIELDS}}
        for line in lines[since:]:
            rec = json.loads(line)
            out["t"].append(datetime.fromisoformat(rec["time"]).timestamp() * 1000)
            out["grid"].append(rec.get("pixels") is not None)
            r = rec.get("readings", {})
            for k in SCALAR_FIELDS:
                out[k].append(r.get(k))
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
        """Readings for a day as CSV, one row per capture.  ?grid=1 appends the
        frame ambient/sky temps and the 32×24 matrix as columns r00c00..r23c31
        (blank for records captured before grids were stored)."""
        check_day(day)
        if store.readings_file(day) is None:
            abort(404)
        grid = request.args.get("grid") == "1"
        grid_cols = [f"r{r:02d}c{c:02d}" for r in range(GRID_ROWS) for c in range(GRID_COLS)]

        def rows():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time"] + SCALAR_FIELDS +
                       (["frame_ambient", "frame_sky"] + grid_cols if grid else []))
            for rec in store.iter_readings(day):
                r = rec.get("readings", {})
                row = [rec["time"]] + ["" if r.get(k) is None else r[k] for k in SCALAR_FIELDS]
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
        if not store.frames_for(day):
            abort(404)
        threading.Thread(target=worker.build, args=(day,), daemon=True).start()
        return jsonify({"status": "started", "day": day})

    # -- media --------------------------------------------------------------
    @app.route("/media/latest.jpg")
    def media_latest():
        resp = send_from_directory(store.root, "latest.jpg", max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/media/frames/<day>/<name>")
    def media_frame(day, name):
        check_day(day)
        if not FRAME_RE.match(name):
            abort(404)
        return send_from_directory(store.frames / day, name, max_age=86400)

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
