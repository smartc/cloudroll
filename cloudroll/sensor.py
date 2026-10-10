"""Read data from the SkyConditions_90640 sensor.

Sources:
  * HTTP  /thermalmatrix               – raw 32×24 °C frame + ambient + sky temp
  * Alpaca observingconditions/0/*     – cloud cover, temps, brightness, SQM, humidity
  * Alpaca safetymonitor/0/issafe      – rain relay (unsafe = wet)
  * WebSocket :81 text message         – pressure (not exposed via Alpaca)
  * HTTP  /setup                       – heatmap colour-scale settings (for mode "sensor")
"""

import json
import logging
import re
import time

import requests
import websocket

log = logging.getLogger(__name__)

_ALPACA_OC = {
    "cloud_cover": "cloudcover",
    "ambient_temp": "temperature",
    "sky_temp": "skytemperature",
    "brightness": "skybrightness",
    "sqm": "skyquality",
    "humidity": "humidity",
}


class SensorClient:
    def __init__(self, cfg):
        s = cfg["sensor"]
        self.host = s["host"]
        self.http = f"http://{s['host']}:{s['http_port']}"
        self.alpaca = f"http://{s['host']}:{s['alpaca_port']}/api/v1"
        self.ws_url = f"ws://{s['host']}:{s['websocket_port']}/"
        self.timeout = s["timeout_sec"]
        self.session = requests.Session()
        self._scale_cache = None
        self._scale_time = 0.0

    def web_url(self):
        return self.http + "/"

    def thermal_matrix(self):
        r = self.session.get(self.http + "/thermalmatrix", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _alpaca_get(self, path):
        try:
            r = self.session.get(f"{self.alpaca}/{path}", timeout=self.timeout)
            r.raise_for_status()
            j = r.json()
            if j.get("ErrorNumber", 0) != 0:
                return None
            return j.get("Value")
        except (requests.RequestException, ValueError) as e:
            log.debug("Alpaca %s failed: %s", path, e)
            return None

    def _ws_state(self):
        """Wait briefly for the sensor's JSON state broadcast on the WebSocket."""
        ws = None
        try:
            ws = websocket.create_connection(self.ws_url, timeout=self.timeout)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                msg = ws.recv()
                if isinstance(msg, str):
                    j = json.loads(msg)
                    if "amb" in j:
                        return j
        except Exception as e:  # noqa: BLE001 – any WS failure just means no pressure
            log.debug("WebSocket state read failed: %s", e)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        return {}

    def readings(self):
        out = {k: self._alpaca_get(f"observingconditions/0/{p}") for k, p in _ALPACA_OC.items()}
        safe = self._alpaca_get("safetymonitor/0/issafe")
        out["raining"] = None if safe is None else (not safe)
        ws = self._ws_state()
        out["pressure"] = ws.get("pressure")
        if out["humidity"] is None:
            out["humidity"] = ws.get("dht_hum")
        if out["ambient_temp"] is None:
            out["ambient_temp"] = ws.get("amb")
        if out["sqm"] is None:
            out["sqm"] = ws.get("sqm")
        if out["brightness"] is None:
            out["brightness"] = ws.get("lux")
        return out

    def sensor_color_scale(self, max_age=600):
        """Scrape heatmap settings from the sensor's /setup page (cached)."""
        now = time.monotonic()
        if self._scale_cache and now - self._scale_time < max_age:
            return self._scale_cache
        r = self.session.get(self.http + "/setup", timeout=self.timeout * 2)
        r.raise_for_status()
        html = r.text

        def num(name):
            m = re.search(rf"name='{name}'[^>]*value='(-?[0-9.]+)'", html)
            return float(m.group(1)) if m else None

        mode_m = re.search(r"name='heatMode'.*?<option value='(\d)' selected", html, re.S)
        scale = {
            "mode": "fixed" if (mode_m and mode_m.group(1) == "1") else "floating",
            "fixed_min": num("heatMin"),
            "fixed_max": num("heatMax"),
            "clear_delta": num("cloudClear"),
            "overcast_delta": num("cloudOvercast"),
            "hot_marker": bool(re.search(r"name='heatHot'[^>]*checked", html)),
        }
        if None in scale.values():
            raise ValueError(f"could not parse colour scale from sensor /setup: {scale}")
        self._scale_cache, self._scale_time = scale, now
        return scale
