"""Timelapse day windows, night event times and sun/moon positions.

A window labelled with date D runs from sunrise(D) + offset until
sunrise(D + 1) + offset, in the configured location's time zone.
"""

import math
from datetime import date, datetime, time, timedelta

import astral.moon
import astral.sun
from astral import Depression, Observer
from astral.sun import sunrise

SYNODIC_MONTH = 29.530589


def sunrise_on(loc, d):
    try:
        return sunrise(Observer(loc.latitude, loc.longitude), date=d, tzinfo=loc.tz)
    except ValueError:
        # Polar day/night: no sunrise – fall back to 06:00 local.
        return datetime.combine(d, time(6, 0), tzinfo=loc.tz)


def window_bounds(loc, d, offset_min):
    off = timedelta(minutes=offset_min)
    return sunrise_on(loc, d) + off, sunrise_on(loc, d + timedelta(days=1)) + off


def window_date_for(loc, when, offset_min):
    """Return the window date that the aware datetime `when` falls into."""
    local = when.astimezone(loc.tz)
    d = local.date()
    start, _ = window_bounds(loc, d, offset_min)
    return d if local >= start else d - timedelta(days=1)


def parse_date(s):
    return date.fromisoformat(s)


def _observer(loc):
    return Observer(loc.latitude, loc.longitude)


def night_events(loc, d):
    """Sunset and astronomical dusk on date d, then astronomical dawn and
    sunrise the next morning: the night inside window d.  An event that doesn't
    happen (e.g. no astronomical darkness near midsummer) is None."""
    obs, nxt = _observer(loc), d + timedelta(days=1)

    def safe(fn, day, **kw):
        try:
            return fn(obs, date=day, tzinfo=loc.tz, **kw)
        except ValueError:
            return None

    return {
        "sunset": safe(astral.sun.sunset, d),
        "dusk": safe(astral.sun.dusk, d, depression=Depression.ASTRONOMICAL),
        "dawn": safe(astral.sun.dawn, nxt, depression=Depression.ASTRONOMICAL),
        "sunrise": safe(astral.sun.sunrise, nxt),
    }


def sky_position(loc, when):
    """Sun and moon elevation (degrees) and the moon's illuminated fraction (%)
    at `when`.  Agrees with indi-allsky to within about 1°."""
    obs = _observer(loc)
    age = astral.moon.phase(when.date())
    return {
        "sun_alt": round(astral.sun.elevation(obs, when), 2),
        "moon_alt": round(astral.moon.elevation(obs, when), 2),
        "moon_illum": round(50 * (1 - math.cos(2 * math.pi * age / SYNODIC_MONTH)), 1),
    }
