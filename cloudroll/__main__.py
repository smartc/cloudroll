"""CloudRoll command line.

  python -m cloudroll run            capture loop + scheduler + web server (normal mode)
  python -m cloudroll capture        capture a single frame and exit
  python -m cloudroll build [DATE] [--layout NAME|both]
                                     (re)build the timelapse for DATE (default: last finished
                                     window) in the given layout(s) (default: runtime setting)
  python -m cloudroll info           show resolved location, windows, image sources and
                                     observatory status (read-only)
  python -m cloudroll calibrate [DATE …|--all] [--history FILE] [--force]
                                     harvest the calibration dataset for finished nights
                                     (default: every finished night not yet harvested);
                                     --history uses a saved HA history export for nights
                                     HA no longer has (see CALIBRATION.md)
  python -m cloudroll lensfit FRAME [--time ISO] [--anchor HIP:x,y …]
                                     fit the allsky lens model from a clear-night frame
                                     (a file, or a stored frame's UTC stamp); see LENS.md
"""

import argparse
import logging
from datetime import datetime, timedelta

from .calibration import Calibration
from .config import load_config, resolve_location
from .hass import HomeAssistant
from .observatory import Observatory
from .sensor import SensorClient
from .settings import Settings
from .skystats import AllskyStats
from .sources import SourceClient
from .store import Store
from .sun import window_bounds, window_date_for
from .worker import Worker


def main():
    ap = argparse.ArgumentParser(prog="cloudroll")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("capture")
    b = sub.add_parser("build")
    b.add_argument("date", nargs="?")
    b.add_argument("--layout", help="layout name, or 'both' for every configured layout")
    sub.add_parser("info")
    c = sub.add_parser("calibrate")
    c.add_argument("dates", nargs="*")
    c.add_argument("--all", action="store_true", help="re-harvest every night with readings")
    c.add_argument("--history", help="saved HA /api/history JSON (.json or .json.gz)")
    c.add_argument("--force", action="store_true", help="also harvest a window still open")
    lf = sub.add_parser("lensfit")
    lf.add_argument("frame", help="image file, or the UTC stamp of a stored allsky frame")
    lf.add_argument("--time", help="mid-exposure time, ISO (default: from the capture record)")
    lf.add_argument("--anchor", action="append", help="HIP:x,y seed star (repeatable)")
    lf.add_argument("--maglim", type=float, default=5.5, help="faintest catalogue magnitude")
    lf.add_argument("--pressure", type=float, default=900, help="station pressure, hPa (refraction)")
    lf.add_argument("--lat", type=float, help="camera latitude (default: lens.lat or location)")
    lf.add_argument("--lon", type=float, help="camera longitude")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = load_config()
    loc = resolve_location(cfg)
    store = Store(cfg["data_dir"])
    sensor = SensorClient(cfg)
    sources = SourceClient(cfg)
    settings = Settings(cfg)
    observatory = Observatory(cfg, loc)
    ha = HomeAssistant(cfg)
    skystats = AllskyStats(cfg, ha)
    calibration = Calibration(cfg, loc, store, ha, skystats)
    worker = Worker(cfg, loc, store, sensor, sources, settings, observatory, skystats, calibration)
    offset = cfg["timelapse"]["offset_after_sunrise_min"]

    if args.cmd == "info":
        now = datetime.now(loc.tz)
        wd = window_date_for(loc, now, offset)
        print(f"Location : {loc.name} ({loc.latitude:.4f}, {loc.longitude:.4f}) {loc.tz.key}")
        print(f"Sensor   : {sensor.web_url()}")
        print(f"Data dir : {cfg['data_dir']}")
        for d in (wd - timedelta(days=1), wd, wd + timedelta(days=1)):
            s, e = window_bounds(loc, d, offset)
            mark = "  <- current" if d == wd else ""
            print(f"Window {d}: {s:%Y-%m-%d %H:%M %Z} -> {e:%Y-%m-%d %H:%M %Z}{mark}")
        if not cfg["sources"]:
            print("Sources  : none (classic thermal-only frames)")
        for src in cfg["sources"]:
            f = sources.fetch(src)
            size = f"{f.image.width}x{f.image.height} decoded, {len(f.data) / 1e3:.0f} KB" \
                if f.ok else ""
            print(f"Source {src['name']:<12}: {f.status} in {f.seconds:.2f} s  {size}")
        if observatory.enabled:
            obs = observatory.read(datetime.now(loc.tz))
            print(f"Roof     : {obs.roof}")
            for r in obs.rigs:
                where = (f"  RA {r.ra:.4f}h Dec {r.dec:+.3f} -> alt {r.alt:.1f} az {r.az:.1f}"
                         + (f" PA {r.pa:.1f}" if r.pa is not None else "")) if r.ra is not None else ""
                pier = f"{r.pier_label} {r.pier}, " if r.pier else ""
                print(f"Rig {r.name:<15}: {pier}NINA {r.status}"
                      f"{f' ({r.detail})' if r.detail else ''}{where}"
                      f"{'  [marker]' if r.marker else ''}")
            print(f"Markers  : {settings.scope_marker}"
                  + ("" if observatory.lens else " (no lens model: status only)"))
        if cfg["layouts"]:
            print(f"Layouts  : {', '.join(cfg['layouts'])}  (dashboard default "
                  f"{settings.dashboard_default}; timelapse {', '.join(settings.timelapse_layouts)})")
    elif args.cmd == "capture":
        print(worker.capture_once())
    elif args.cmd == "build":
        day = args.date or (window_date_for(loc, datetime.now(loc.tz), offset)
                            - timedelta(days=1)).isoformat()
        try:
            meta = worker.build(day, args.layout)
        except ValueError as e:
            raise SystemExit(str(e))
        print(meta if meta else f"No timelapse built for {day}")
    elif args.cmd == "calibrate":
        from .calibration import load_history_file
        history = load_history_file(args.history) if args.history else None
        current = window_date_for(loc, datetime.now(loc.tz), offset).isoformat()
        days = args.dates or [d for d in sorted(store.readings_days())
                              if (args.all or not calibration.has_night(d))
                              and (d != current or args.force)]
        for day in days:
            try:
                h = calibration.harvest(day, history=history, force=args.force)
            except ValueError as e:
                print(f"{day}: {e}")
                continue
            if h is None:
                print(f"{day}: no readings")
            else:
                print(f"{day}: {h['minutes']} dark minutes, {h['with_stars']} with stars, "
                      f"{len(h['images'])} images, history {h['history_source']}"
                      + (f"  ({'; '.join(h['notes'])})" if h["notes"] else ""))
    elif args.cmd == "lensfit":
        from .lensfit import run as lensfit
        try:
            lensfit(cfg, loc, store, args)
        except ValueError as e:
            raise SystemExit(str(e))
    elif args.cmd == "run":
        from waitress import serve
        from .web import create_app
        worker.start()
        app = create_app(cfg, loc, store, worker, sensor, settings, calibration)
        w = cfg["web"]
        logging.info("Web UI on http://%s:%d/", w["host"], w["port"])
        serve(app, host=w["host"], port=w["port"], threads=6, ident="CloudRoll")


if __name__ == "__main__":
    main()
