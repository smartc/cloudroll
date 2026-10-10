"""CloudRoll command line.

  python -m cloudroll run            capture loop + scheduler + web server (normal mode)
  python -m cloudroll capture        capture a single frame and exit
  python -m cloudroll build [DATE]   (re)build the timelapse for DATE (default: last finished window)
  python -m cloudroll info           show resolved location and current/next windows
"""

import argparse
import logging
from datetime import datetime, timedelta

from .config import load_config, resolve_location
from .sensor import SensorClient
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
    sub.add_parser("info")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = load_config()
    loc = resolve_location(cfg)
    store = Store(cfg["data_dir"])
    sensor = SensorClient(cfg)
    worker = Worker(cfg, loc, store, sensor)
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
    elif args.cmd == "capture":
        print(worker.capture_once())
    elif args.cmd == "build":
        day = args.date or (window_date_for(loc, datetime.now(loc.tz), offset)
                            - timedelta(days=1)).isoformat()
        meta = worker.build(day)
        print(meta if meta else f"No timelapse built for {day}")
    elif args.cmd == "run":
        from waitress import serve
        from .web import create_app
        worker.start()
        app = create_app(cfg, loc, store, worker, sensor)
        w = cfg["web"]
        logging.info("Web UI on http://%s:%d/", w["host"], w["port"])
        serve(app, host=w["host"], port=w["port"], threads=6, ident="CloudRoll")


if __name__ == "__main__":
    main()
