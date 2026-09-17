#!/usr/bin/env python3
"""
solar.py - sunrise and sunset for a latitude and longitude, no dependencies.

The standard "sunrise equation". Accurate to a few minutes, which is plenty for
deciding when to start an evening show.
"""

from datetime import date, datetime
from math import acos, asin, cos, degrees, radians, sin


def sun_times_utc(day, lat, lon):
    """(sunrise, sunset) as naive UTC datetimes for `day`, or (None, None) when
    the sun never rises or sets there that day. `lon` is east-positive."""
    n = day.toordinal() - date(2000, 1, 1).toordinal()
    j_star = n - lon / 360.0                       # mean solar noon, days since J2000
    m = (357.5291 + 0.98560028 * j_star) % 360     # solar mean anomaly
    mr = radians(m)
    c = 1.9148 * sin(mr) + 0.0200 * sin(2 * mr) + 0.0003 * sin(3 * mr)
    lam = (m + c + 180 + 102.9372) % 360           # ecliptic longitude
    lr = radians(lam)
    j_transit = 2451545.0 + j_star + 0.0053 * sin(mr) - 0.0069 * sin(2 * lr)
    decl = asin(sin(lr) * sin(radians(23.4397)))
    cos_w = ((sin(radians(-0.833)) - sin(radians(lat)) * sin(decl))
             / (cos(radians(lat)) * cos(decl)))
    if cos_w > 1 or cos_w < -1:
        return None, None
    w = degrees(acos(cos_w))

    def to_utc(jd):
        return datetime.utcfromtimestamp((jd - 2440587.5) * 86400.0)

    return to_utc(j_transit - w / 360.0), to_utc(j_transit + w / 360.0)


def sun_times(day, lat, lon):
    """Same, as naive local datetimes in the system time zone."""
    rise, sset = sun_times_utc(day, lat, lon)
    if rise is None:
        return None, None
    to_local = lambda dt: datetime.fromtimestamp((dt - datetime(1970, 1, 1)).total_seconds())
    return to_local(rise), to_local(sset)


if __name__ == "__main__":
    import sys
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 32.78
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -96.80
    day = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.today()
    rise, sset = sun_times(day, lat, lon)
    print("sunrise %s  sunset %s (local time)" % (rise, sset))
