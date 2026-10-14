"""Timelapse day windows.

A window labelled with date D runs from sunrise(D) + offset until
sunrise(D + 1) + offset, in the configured location's time zone.
"""

from datetime import date, datetime, time, timedelta

from astral import Observer
from astral.sun import sunrise


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
