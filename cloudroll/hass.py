"""Read-only Home Assistant REST client: entity states and recorder history.

Used for the allsky camera's statistics (star count, SQM, exposure, …) that
CloudRoll records with each capture and harvests nightly for calibration.
Only ever GETs.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)


def ha_token(ha):
    """The long-lived token from config: inline "token", else "token_file"."""
    token = ha.get("token") or ""
    if not token and ha.get("token_file"):
        try:
            token = Path(ha["token_file"]).expanduser().read_text().strip()
        except OSError as e:
            log.warning("Could not read Home Assistant token file: %s", e)
    return token


def parse_state(state):
    """An HA state string as a number, bool (on/off) or string; None if unavailable."""
    if state in (None, "", "unknown", "unavailable"):
        return None
    if state in ("on", "off"):
        return state == "on"
    try:
        v = float(state)
    except ValueError:
        return state
    return int(v) if v.is_integer() and "." not in state else v


class HomeAssistant:
    def __init__(self, cfg, timeout=5):
        ha = cfg["homeassistant"]
        self.url = (ha.get("url") or "").rstrip("/")
        self.verify = ha.get("verify_ssl", True)
        self.timeout = timeout
        token = ha_token(ha)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.session = requests.Session()

    @property
    def enabled(self):
        return bool(self.url and self.headers)

    def _get(self, path, params=None, timeout=None):
        r = self.session.get(f"{self.url}/api/{path}", params=params, headers=self.headers,
                             timeout=timeout or self.timeout, verify=self.verify)
        r.raise_for_status()
        return r.json()

    def state(self, entity):
        """(value, last_updated datetime) of one entity."""
        j = self._get(f"states/{entity}")
        when = j.get("last_updated") or j.get("last_changed")
        return parse_state(j.get("state")), (datetime.fromisoformat(when) if when else None)

    def history(self, entities, start, end):
        """{entity: [(datetime, value), …]} from the recorder, oldest first.
        HA only keeps about 10 days (recorder purge_keep_days) by default."""
        params = {"filter_entity_id": ",".join(entities),
                  "end_time": end.astimezone(timezone.utc).isoformat(),
                  "minimal_response": "", "no_attributes": "",
                  "significant_changes_only": "0"}
        start_iso = start.astimezone(timezone.utc).isoformat()
        data = self._get(f"history/period/{start_iso}", params=params, timeout=120)
        out = {e: [] for e in entities}
        for series in data:
            if not series:
                continue
            ent = series[0]["entity_id"]
            pts = out.setdefault(ent, [])
            for p in series:
                pts.append((datetime.fromisoformat(p["last_changed"]), parse_state(p.get("state"))))
        return out
