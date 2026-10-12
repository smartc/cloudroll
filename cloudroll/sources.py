"""Fetch extra images (allsky, observatory cams, …) to composite into frames.

Source types (config.json "sources"):
  homeassistant  image.* entity via /api/image_proxy, camera.* via /api/camera_proxy
  url            plain HTTP(S) GET of a JPEG/PNG
  rtsp           one frame grabbed from an RTSP stream with ffmpeg

A fetch never raises: failures come back as a status string so a camera outage
only blanks that panel instead of losing the capture.
"""

import io
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from PIL import Image

log = logging.getLogger(__name__)

# Decode big camera JPEGs at a reduced DCT scale that still covers the largest
# slot a frame layout uses (1280×720 wide / 988×988 square).
DECODE_SIZE = (1280, 1024)


@dataclass
class Fetched:
    name: str
    label: str
    data: bytes | None = None       # original bytes as received, for archiving
    image: Image.Image | None = None
    status: str = "ok"              # "ok" | "stale" | "error: …"
    ext: str = "jpg"
    seconds: float = 0.0
    badges: list = field(default_factory=list)   # [(text, rgb)] shown right of the caption

    @property
    def ok(self):
        return self.image is not None


class SourceClient:
    def __init__(self, cfg):
        self.sources = cfg["sources"]
        self.timeout = cfg["source_timeout_sec"]
        ha = cfg["homeassistant"]
        self.ha_url = (ha.get("url") or "").rstrip("/")
        self.verify = ha.get("verify_ssl", True)
        self.session = requests.Session()
        token = ha.get("token") or ""
        if not token and ha.get("token_file"):
            try:
                token = Path(ha["token_file"]).expanduser().read_text().strip()
            except OSError as e:
                log.warning("Could not read Home Assistant token file: %s", e)
        self._ha_headers = {"Authorization": f"Bearer {token}"} if token else {}

    def fetch(self, src):
        out = Fetched(src["name"], src.get("label") or src["name"].title())
        t0 = time.monotonic()
        try:
            kind = src["type"]
            if kind == "homeassistant":
                out.data, modified = self._ha(src)
            elif kind == "url":
                out.data, modified = self._url(src)
            else:
                out.data, modified = self._rtsp(src), None
            out.image = decode(out.data, src.get("crop"))
            out.ext = _ext_for(out.data)
            max_age = src.get("max_age_sec")
            if max_age and modified is not None:
                age = (datetime.now(timezone.utc) - modified).total_seconds()
                if age > max_age:
                    out.status = "stale"
                    log.info("Source %s is stale (%.0f s old)", src["name"], age)
        except Exception as e:  # noqa: BLE001 – any failure just blanks this panel
            out.data = out.image = None
            out.status = f"error: {e}"
            log.warning("Source %s failed: %s", src["name"], e)
        out.seconds = time.monotonic() - t0
        return out

    def _ha(self, src):
        if not self.ha_url:
            raise ValueError("homeassistant.url is not set")
        if not self._ha_headers:
            raise ValueError("no Home Assistant token (homeassistant.token or token_file)")
        entity = src["entity"]
        proxy = "image_proxy" if entity.startswith("image.") else "camera_proxy"
        r = self.session.get(f"{self.ha_url}/api/{proxy}/{entity}", headers=self._ha_headers,
                             timeout=self.timeout, verify=self.verify)
        r.raise_for_status()
        modified = None
        if src.get("max_age_sec") and entity.startswith("image."):
            # An image entity's state is the time its image last changed.
            s = self.session.get(f"{self.ha_url}/api/states/{entity}",
                                 headers=self._ha_headers, timeout=self.timeout,
                                 verify=self.verify)
            s.raise_for_status()
            try:
                modified = datetime.fromisoformat(s.json()["state"])
            except (KeyError, ValueError):
                modified = None
        return r.content, modified

    def _url(self, src):
        r = self.session.get(src["url"], timeout=self.timeout,
                             verify=src.get("verify_ssl", True))
        r.raise_for_status()
        lm = r.headers.get("Last-Modified")
        return r.content, (parsedate_to_datetime(lm) if lm else None)

    def _rtsp(self, src):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
               "-i", src["url"], "-frames:v", "1", "-q:v", "2", "-f", "image2pipe",
               "-c:v", "mjpeg", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=self.timeout, check=False)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[-200:] or "no frame")
        return r.stdout


def decode(data, crop=None):
    """Bytes -> RGB image, decoded at reduced scale and optionally cropped.

    crop is [left, top, right, bottom] as fractions of the image (0–1)."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", DECODE_SIZE)
    im = im.convert("RGB")
    if crop:
        l, t, r, b = crop
        im = im.crop((round(l * im.width), round(t * im.height),
                      round(r * im.width), round(b * im.height)))
    return im


def _ext_for(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"
