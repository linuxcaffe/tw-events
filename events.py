#!/usr/bin/env python3
"""
events — the calendar's non-task entries, as Taskwarrior 2.6.2 `holiday.*` entries,
so `task calendar`, tw-web and other clients all show the same days: public holidays
from holidata.net, and (computed, no network) the moon's phases and the solstices
and equinoxes.

Commands
  events init [--locale L] [--regions R] [--types T] [--years N]
              [--moon yes|no] [--seasons yes|no] [--clock yes|no] [--json]
                                  Set up (or change) the settings, refresh, then
                                  add the includes, so none ever points at a
                                  missing file. Moves holidata 0.1 settings over.
                                  Safe to run again.
  events refresh [--source DIR] [--no-fetch] [--json]
                                  Fetch the holidays for this year and the next
                                  (events.years), keep national ones plus
                                  events.regions, rewrite config/holidays.rc; and
                                  compute config/sky.rc (moon, seasons, clock
                                  changes). A failed fetch keeps the
                                  old holidays; --no-fetch doesn't fetch them.
  events show [--year Y] [--json] List what's installed (the default).
  events check [--days N] [--json]
                                  Read-only. Warn when the entries run out within
                                  N days (default 60), when the settings changed
                                  since the last refresh, or when task doesn't
                                  see them.
  events add <id|uuid>… [--json]  A ghost of each task: its description at its
                                  due (else scheduled) date and time, appended to
                                  config/my-events.txt, so it stays on the calendar
                                  after the task is done or deleted.

Settings live in config/events.rc, which config/included.rc includes and which
includes holidays.rc, sky.rc and mine.rc. Your own events are config/my-events.txt
(timedot style: a date, then its events; --MM-DD every year), which refresh and add
turn into mine.rc. All under the task dir: $TW_TASK_DIR, else
~/.task. Exit 0 ok, 1 warnings, 2 failures. Standard library only.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

__version__ = '0.4.0'

OK, WARN, FAIL = 'ok', 'warn', 'fail'
BASE_URL = 'https://holidata.net'
DEFAULTS = {'locale': 'en-US', 'regions': '', 'types': '', 'years': '2', 'moon': 'no', 'seasons': 'no', 'clock': 'no'}
HEADER_RE = re.compile(r'^# (?:holidata|events|events-sky|events-mine): (.*)$')
ENTRY_RE = re.compile(r'^holiday\.((?:hd|mn|ss|me|ck)\d{8}[a-z]+)\.'
                      r'(name|date|start|end|type|region|locale|kind|at)=(.*)$')
YES = ('yes', 'on', 'true', '1', 'y')


# ── paths and settings ────────────────────────────────────────────────────────

def task_dir():
    return Path(os.environ.get('TW_TASK_DIR') or Path.home() / '.task').expanduser()


def settings_file():
    return task_dir() / 'config' / 'events.rc'


def legacy_settings_file():
    """holidata 0.1's settings; `events init` moves them into events.rc."""
    return task_dir() / 'config' / 'holidata.rc'


def data_file():
    return task_dir() / 'config' / 'holidays.rc'


def sky_file():
    return task_dir() / 'config' / 'sky.rc'


def my_events_file():
    """Yours: hand-edited; `events add` only ever appends to it. Taskwarrior doesn't read it."""
    return task_dir() / 'config' / 'my-events.txt'


def mine_file():
    return task_dir() / 'config' / 'mine.rc'


def read_settings(path=None):
    """events.<key> = value lines (holidata.<key> in a holidata 0.1 file); `#` starts a comment."""
    path = Path(path) if path else settings_file()
    if path == settings_file() and not path.is_file() and legacy_settings_file().is_file():
        path = legacy_settings_file()
    s = dict(DEFAULTS)
    if path.is_file():
        for line in path.read_text(errors='replace').splitlines():
            line = line.split('#', 1)[0].strip()
            if '=' not in line:
                continue
            k, v = (x.strip() for x in line.split('=', 1))
            for prefix in ('events.', 'holidata.'):
                if k.startswith(prefix):
                    s[k[len(prefix):]] = v
    return s


def _list(value):
    return [x.strip() for x in value.split(',') if x.strip()]


def _yes(value):
    return str(value).strip().lower() in YES


def plan(settings, today):
    """What a refresh fetches, keeps and computes, as one comparable dict."""
    try:
        years = max(1, int(settings.get('years') or 2))
    except ValueError:
        years = 2
    return {
        'locales': _list(settings.get('locale', '')) or [DEFAULTS['locale']],
        'regions': sorted(_list(settings.get('regions', ''))),
        'types':   sorted(set(''.join(_list(settings.get('types', ''))).upper())),
        'years':   [today.year + i for i in range(years)],
        'moon':    _yes(settings.get('moon', 'no')),
        'seasons': _yes(settings.get('seasons', 'no')),
        'clock':   _yes(settings.get('clock', 'no')),
    }


def _plan_text(p):
    """The holidays file's header line, which check compares."""
    return (f"locale={','.join(p['locales'])} regions={','.join(p['regions'])} "
            f"types={''.join(p['types'])} years={','.join(map(str, p['years']))}")


def _sky_text(p):
    """clock= only when on, so a 0.3 file's header still matches settings without it."""
    return (f"moon={'yes' if p['moon'] else 'no'} seasons={'yes' if p['seasons'] else 'no'} "
            + ('clock=yes ' if p['clock'] else '') + f"years={','.join(map(str, p['years']))}")


def _strip_years(text):
    """A new year moves the years along on its own; only the other settings must match."""
    return re.sub(r'\s*years=\S*', '', text or '')


# ── fetching and filtering ────────────────────────────────────────────────────

class NotPublished(Exception):
    """holidata has no file for that locale and year (HTTP 404)."""


def fetch(locale, year, source=None):
    """The year's records. `source` is a folder laid out like the site (DIR/<locale>/<year>.json)."""
    if source:
        f = Path(source) / locale / f'{year}.json'
        if not f.is_file():
            raise NotPublished(f'{locale}/{year}')
        text = f.read_text()
    else:
        url = f'{BASE_URL}/{locale}/{year}.json'
        req = urllib.request.Request(url, headers={'User-Agent': f'tw-events/{__version__}'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotPublished(f'{locale}/{year}') from None
            raise
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def keep(rec, p):
    """National holidays always; regional ones only for the configured regions; then the type filter."""
    region = rec.get('region') or ''
    if region and region not in p['regions']:
        return False
    if p['types'] and not set(rec.get('type') or '') & set(p['types']):
        return False
    return True


def clean(text):
    """One rc value: no control characters (a newline would start a new rc line), and no `#`
    (2.6.2 reads the rest of the line as a comment, verified)."""
    text = ''.join(ch if ch.isprintable() else ' ' for ch in str(text))
    return ' '.join(text.replace('#', '＃').split())


def entries(records):
    """Records → holiday entries. Consecutive days with the same name, region and locale become one
    range. Sorted by start date; keys hd<YYYYMMDD><a,b,…> are unique and stable for a given input."""
    rows = []
    for r in records:
        try:
            d = dt.date.fromisoformat(r['date'])
        except (KeyError, ValueError):
            continue
        rows.append({'start': d, 'end': d, 'name': clean(r.get('description', '')),
                     'type': clean(r.get('type', '')), 'region': clean(r.get('region', '')),
                     'locale': clean(r.get('locale', ''))})
    rows = [r for r in rows if r['name']]
    rows.sort(key=lambda r: (r['locale'], r['region'], r['name'], r['start']))
    merged, seen = [], set()
    for r in rows:
        ident = (r['locale'], r['region'], r['name'], r['start'])
        if ident in seen:
            continue                      # the same day listed twice
        seen.add(ident)
        last = merged[-1] if merged else None
        if (last and (last['locale'], last['region'], last['name']) == ident[:3]
                and r['start'] == last['end'] + dt.timedelta(days=1)):
            last['end'] = r['start']
        else:
            merged.append(dict(r))
    merged.sort(key=lambda r: (r['start'], r['region'], r['name']))
    counts = {}
    for r in merged:
        day = r['start'].strftime('%Y%m%d')
        n = counts.get(day, 0)
        counts[day] = n + 1
        r['key'] = 'hd' + day + _suffix(n)
    return merged


def _suffix(n):
    s = ''
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(ord('a') + rem) + s
    return s


def render(rows, p, now):
    out = [
        '# Public holidays from https://holidata.net, written by `events refresh`.',
        '# Do not edit: change config/events.rc and run `events refresh`.',
        f'# events: {_plan_text(p)}',
        '',
    ]
    for r in rows:
        k = f"holiday.{r['key']}"
        out.append(f"{k}.name={r['name']}")
        if r['start'] == r['end']:
            out.append(f"{k}.date={r['start'].strftime('%Y%m%d')}")
        else:
            out.append(f"{k}.start={r['start'].strftime('%Y%m%d')}")
            out.append(f"{k}.end={r['end'].strftime('%Y%m%d')}")
        for field in ('type', 'region', 'locale'):
            if r[field]:
                out.append(f'{k}.{field}={r[field]}')
    return '\n'.join(out) + '\n'


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.events.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── astronomy: moon phases and the solstices and equinoxes (Meeus, Astronomical Algorithms) ──
# Computed, not fetched. Accurate to a minute or two against PyEphem (tests/test_astro.py), which is
# plenty for a calendar mark. Times come out in UTC.

from math import sin, cos, radians

MOON_PHASES = ('New Moon', 'First Quarter', 'Full Moon', 'Last Quarter')
SEASONS = ('March Equinox', 'June Solstice', 'September Equinox', 'December Solstice')

# Meeus ch. 49: periodic terms (coefficient, E power, multiples of M, M', F, Ω)
_NEW = [(-0.40720, 0, 0, 1, 0, 0), (0.17241, 1, 1, 0, 0, 0), (0.01608, 0, 0, 2, 0, 0),
        (0.01039, 0, 0, 0, 2, 0), (0.00739, 1, -1, 1, 0, 0), (-0.00514, 1, 1, 1, 0, 0),
        (0.00208, 2, 2, 0, 0, 0), (-0.00111, 0, 0, 1, -2, 0), (-0.00057, 0, 0, 1, 2, 0),
        (0.00056, 1, 1, 2, 0, 0), (-0.00042, 0, 0, 3, 0, 0), (0.00042, 1, 1, 0, 2, 0),
        (0.00038, 1, 1, 0, -2, 0), (-0.00024, 1, -1, 2, 0, 0), (-0.00017, 0, 0, 0, 0, 1)]
_FULL = [(-0.40614, 0, 0, 1, 0, 0), (0.17302, 1, 1, 0, 0, 0), (0.01614, 0, 0, 2, 0, 0),
         (0.01043, 0, 0, 0, 2, 0), (0.00734, 1, -1, 1, 0, 0), (-0.00515, 1, 1, 1, 0, 0),
         (0.00209, 2, 2, 0, 0, 0), (-0.00111, 0, 0, 1, -2, 0), (-0.00057, 0, 0, 1, 2, 0),
         (0.00056, 1, 1, 2, 0, 0), (-0.00042, 0, 0, 3, 0, 0), (0.00042, 1, 1, 0, 2, 0),
         (0.00038, 1, 1, 0, -2, 0), (-0.00024, 1, -1, 2, 0, 0), (-0.00017, 0, 0, 0, 0, 1)]
_NEW_FULL_SMALL = [(-0.00007, 0, 2, 1, 0, 0), (0.00004, 0, 0, 2, -2, 0), (0.00004, 0, 3, 0, 0, 0),
                   (0.00003, 0, 1, 1, -2, 0), (0.00003, 0, 0, 2, 2, 0), (-0.00003, 0, 1, 1, 2, 0),
                   (0.00003, 0, -1, 1, 2, 0), (-0.00002, 0, -1, 1, -2, 0), (-0.00002, 0, 1, 3, 0, 0),
                   (0.00002, 0, 0, 4, 0, 0)]
_QUARTER = [(-0.62801, 0, 0, 1, 0, 0), (0.17172, 1, 1, 0, 0, 0), (-0.01183, 1, 1, 1, 0, 0),
            (0.00862, 0, 0, 2, 0, 0), (0.00804, 0, 0, 0, 2, 0), (0.00454, 1, -1, 1, 0, 0),
            (0.00204, 2, 2, 0, 0, 0), (-0.00180, 0, 0, 1, -2, 0), (-0.00070, 0, 0, 1, 2, 0),
            (-0.00040, 0, 0, 3, 0, 0), (-0.00034, 1, -1, 2, 0, 0), (0.00032, 1, 1, 0, 2, 0),
            (0.00032, 1, 1, 0, -2, 0), (-0.00028, 2, 2, 1, 0, 0), (0.00027, 1, 1, 2, 0, 0),
            (-0.00017, 0, 0, 0, 0, 1), (-0.00005, 0, -1, 1, -2, 0), (0.00004, 0, 0, 2, 2, 0),
            (-0.00004, 0, 1, 1, 2, 0), (0.00004, 0, -2, 1, 0, 0), (0.00003, 0, 1, 1, -2, 0),
            (0.00003, 0, 3, 0, 0, 0), (0.00002, 0, 0, 2, -2, 0), (0.00002, 0, -1, 1, 2, 0),
            (-0.00002, 0, 1, 3, 0, 0)]
_PLANETARY = [(299.77, 0.107408, 0.000325), (251.88, 0.016321, 0.000165), (251.83, 26.651886, 0.000164),
              (349.42, 36.412478, 0.000126), (84.66, 18.206239, 0.000110), (141.74, 53.303771, 0.000062),
              (207.14, 2.453732, 0.000060), (154.84, 7.306860, 0.000056), (34.52, 27.261239, 0.000047),
              (207.19, 0.121824, 0.000042), (291.34, 1.844379, 0.000040), (161.72, 24.198154, 0.000037),
              (239.56, 25.513099, 0.000035), (331.55, 3.592518, 0.000023)]


def _moon_phase_jde(k):
    """Julian Ephemeris Day of the phase k (an integer for a new moon, +.25/.5/.75 for the others)."""
    T = k / 1236.85
    jde = (2451550.09766 + 29.530588861 * k + 0.00015437 * T**2
           - 0.000000150 * T**3 + 0.00000000073 * T**4)
    E = 1 - 0.002516 * T - 0.0000074 * T**2
    M = radians(2.5534 + 29.10535670 * k - 0.0000014 * T**2 - 0.00000011 * T**3)
    Mp = radians(201.5643 + 385.81693528 * k + 0.0107582 * T**2 + 0.00001238 * T**3 - 0.000000058 * T**4)
    F = radians(160.7108 + 390.67050284 * k - 0.0016118 * T**2 - 0.00000227 * T**3 + 0.000000011 * T**4)
    Om = radians(124.7746 - 1.56375588 * k + 0.0020672 * T**2 + 0.00000215 * T**3)
    frac = round((k % 1) * 4) % 4
    terms = {0: _NEW + _NEW_FULL_SMALL, 2: _FULL + _NEW_FULL_SMALL}.get(frac, _QUARTER)
    jde += sum(c * E**e * sin(m * M + mp * Mp + f * F + o * Om) for c, e, m, mp, f, o in terms)
    if frac in (1, 3):
        W = (0.00306 - 0.00038 * E * cos(M) + 0.00026 * cos(Mp) - 0.00002 * cos(Mp - M)
             + 0.00002 * cos(Mp + M) + 0.00002 * cos(2 * F))
        jde += W if frac == 1 else -W
    A1 = radians(299.77 + 0.107408 * k - 0.009173 * T**2)
    jde += 0.000325 * sin(A1) + sum(c * sin(radians(a + b * k)) for a, b, c in _PLANETARY[1:])
    return jde


# Meeus ch. 27: mean equinox/solstice (years 1000–3000), then the periodic correction
_SEASON_MEAN = [(2451623.80984, 365242.37404, 0.05169, -0.00411, -0.00057),
                (2451716.56767, 365241.62603, 0.00325, 0.00888, -0.00030),
                (2451810.21715, 365242.01767, -0.11575, 0.00337, 0.00078),
                (2451900.05952, 365242.74049, -0.06223, -0.00823, 0.00032)]
_SEASON_TERMS = [(485, 324.96, 1934.136), (203, 337.23, 32964.467), (199, 342.08, 20.186),
                 (182, 27.85, 445267.112), (156, 73.14, 45036.886), (136, 171.52, 22518.443),
                 (77, 222.54, 65928.934), (74, 296.72, 3034.906), (70, 243.58, 9037.513),
                 (58, 119.81, 33718.147), (52, 297.17, 150.678), (50, 21.02, 2281.226),
                 (45, 247.54, 29929.562), (44, 325.15, 31555.956), (29, 60.93, 4443.417),
                 (18, 155.12, 67555.328), (17, 288.79, 4562.452), (16, 198.04, 62894.029),
                 (14, 199.76, 31436.921), (12, 95.39, 14577.848), (12, 287.11, 31931.756),
                 (12, 320.81, 34777.259), (9, 227.73, 1222.114), (8, 15.45, 16859.074)]


def _season_jde(year, i):
    Y = (year - 2000) / 1000
    a, b, c, d, e = _SEASON_MEAN[i]
    jde0 = a + b * Y + c * Y**2 + d * Y**3 + e * Y**4
    T = (jde0 - 2451545.0) / 36525
    W = radians(35999.373 * T - 2.47)
    dl = 1 + 0.0334 * cos(W) + 0.0007 * cos(2 * W)
    S = sum(A * cos(radians(B + C * T)) for A, B, C in _SEASON_TERMS)
    return jde0 + 0.00001 * S / dl


def _jde_to_utc(jde):
    """Dynamical time → UTC: ΔT from NASA's polynomial for 2005–2050 (about 70 s now)."""
    t = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=jde - 2440587.5)
    y = t.year + (t.timetuple().tm_yday - 0.5) / 365.25 - 2000
    return (t - dt.timedelta(seconds=62.92 + 0.32217 * y + 0.005589 * y * y)).replace(microsecond=0)


def moon_phases(year):
    """[(utc datetime, name)] for the principal phases whose instant falls in that UTC year."""
    out = []
    k0 = int((year - 2000) * 12.3685) - 2
    for k in range(k0, k0 + 16):
        for q in range(4):
            when = _jde_to_utc(_moon_phase_jde(k + q / 4))
            if when.year == year:
                out.append((when, MOON_PHASES[q]))
    return sorted(out)


def seasons(year):
    return [(_jde_to_utc(_season_jde(year, i)), SEASONS[i]) for i in range(4)]



# ── clock changes: where this machine's UTC offset changes (its own zone: TZ, /etc/localtime) ──

def _offset(t):
    return time.localtime(t).tm_gmtoff


def clock_changes(year):
    """[(utc datetime, name)] for the moments in that UTC year when the local offset changes: checked
    a day apart, then narrowed to the second. Named by direction and the new zone's abbreviation."""
    start = int(dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    end = int(dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    out, t = [], start
    while t < end:
        nxt = min(t + 86400, end)
        if _offset(t) != _offset(nxt):
            lo, hi = t, nxt                        # offset(lo) is the old one, offset(hi) the new
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if _offset(mid) == _offset(t) else (lo, mid)
            name = 'Clocks forward' if _offset(hi) > _offset(t) else 'Clocks back'
            out.append((dt.datetime.fromtimestamp(hi, dt.timezone.utc), f'{name} ({time.localtime(hi).tm_zone})'))
        t = nxt
    return out


def sky_entries(p):
    """The moon's phases and the seasons for the planned years, as entries: the date is the local
    day of the moment (this machine's time zone), `at` the moment in UTC, `kind` moon or season."""
    rows = []
    for year in p['years']:
        if p['moon']:
            rows += [(when, name, 'moon', 'mn') for when, name in moon_phases(year)]
        if p['seasons']:
            rows += [(when, name, 'season', 'ss') for when, name in seasons(year)]
        if p['clock']:
            rows += [(when, name, 'clock', 'ck') for when, name in clock_changes(year)]
    rows.sort()
    out, counts = [], {}
    for when, name, kind, prefix in rows:
        day = when.astimezone().date()
        key = prefix + day.strftime('%Y%m%d')
        n = counts.get(key, 0)
        counts[key] = n + 1
        out.append({'key': key + _suffix(n), 'start': day, 'end': day, 'name': name, 'kind': kind,
                    'at': when.strftime('%Y%m%dT%H%M%SZ')})
    return out


def render_sky(rows, p, now):
    out = [
        '# Moon phases, solstices and equinoxes (Meeus) and clock changes (this machine\'s time zone),',
        '# computed by `events refresh`, no network.',
        '# Do not edit: change events.moon / .seasons / .clock in config/events.rc and run `events refresh`.',
        f'# events-sky: {_sky_text(p)}',
        '',
    ]
    for r in rows:
        k = f"holiday.{r['key']}"
        out += [f"{k}.name={r['name']}", f"{k}.date={r['start'].strftime('%Y%m%d')}",
                f"{k}.kind={r['kind']}", f"{k}.at={r['at']}"]
    return '\n'.join(out) + '\n'


# ── my events: config/my-events.txt (timedot style) → mine.rc ──────────────────
# A line starting (column 0) with a date sets the current date; an event may follow on the same
# line, and indented lines under it are more events that day: `[HH:MM]  name`. A date is
# YYYY-MM-DD, or --MM-DD for every year (29 February falls on the 28th in common years); A..B is
# a run of days, untimed. Lines starting with #, ; or * are comments.

_DAY = r'(?:\d{4}|-)-\d{2}-\d{2}'
_DATE_LINE_RE = re.compile(rf'^({_DAY})(?:\.\.({_DAY}))?(?:\s+(.*))?$')
_EVENT_RE = re.compile(r'^(?:(\d{1,2}:\d{2})(?:\s+|$))?(.*)$')


def _day(spec, year):
    """A date spec on a given year (the spec's own year if it has one); ValueError if no such day."""
    y, m, d = spec.split('-')[-3:] if not spec.startswith('--') else (year, *spec[2:].split('-'))
    y, m, d = int(y), int(m), int(d)
    if spec.startswith('--') and (m, d) == (2, 29):
        try:
            return dt.date(y, 2, 29)
        except ValueError:
            return dt.date(y, 2, 28)
    return dt.date(y, m, d)


def parse_my_events(text):
    """(events, problems): events as {'line', 'start', 'end', 'time', 'name'} with date specs
    (strings); problems as (line number, reason)."""
    out, problems, current = [], [], None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip()[0] in '#;*':
            continue
        if not line[0].isspace():
            m = _DATE_LINE_RE.match(line)
            if not m:
                problems.append((n, 'not a date (YYYY-MM-DD or --MM-DD)'))
                current = None
                continue
            start, end, rest = m.group(1), m.group(2) or m.group(1), (m.group(3) or '').strip()
            try:
                _day(start, 2024), _day(end, 2024)
                if start.startswith('--') != end.startswith('--'):
                    raise ValueError
                if not start.startswith('--') and _day(end, 0) < _day(start, 0):
                    raise ValueError
            except ValueError:
                problems.append((n, 'no such date (or a range that runs backwards)'))
                current = None
                continue
            current = (start, end)
            if not rest:
                continue
        elif current is None:
            problems.append((n, 'an indented event with no date line above it'))
            continue
        else:
            rest = line.strip()
        m = _EVENT_RE.match(rest)
        time_, name = m.group(1), m.group(2).strip()
        if time_:
            try:
                time_ = dt.datetime.strptime(time_, '%H:%M').strftime('%H:%M')
            except ValueError:
                problems.append((n, f'no such time: {time_}'))
                continue
            if current[0] != current[1]:
                problems.append((n, 'a run of days takes no time'))
                continue
        if not name:
            problems.append((n, 'no name'))
            continue
        out.append({'line': n, 'start': current[0], 'end': current[1], 'time': time_, 'name': name})
    return out, problems


def mine_entries(events_, years):
    """Parsed events → entries for the planned years (one-offs whatever their year), keys me<date><a…>."""
    rows = []
    for e in events_:
        for year in (years if e['start'].startswith('--') else [None]):
            start = _day(e['start'], year)
            end = _day(e['end'], year)
            if end < start:                        # --12-31..--01-01
                end = _day(e['end'], year + 1)
            at = ''
            if e['time']:
                local = dt.datetime.combine(start, dt.datetime.strptime(e['time'], '%H:%M').time())
                at = local.astimezone().astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            rows.append({'start': start, 'end': end, 'name': clean(e['name']), 'at': at, 'kind': 'mine'})
    rows = [r for r in rows if r['name']]
    rows.sort(key=lambda r: (r['start'], r['at'], r['name']))   # a day's all-day ones first
    counts = {}
    for r in rows:
        day = r['start'].strftime('%Y%m%d')
        n = counts.get(day, 0)
        counts[day] = n + 1
        r['key'] = 'me' + day + _suffix(n)
    return rows


def _mine_text(source_text, p):
    import hashlib
    digest = hashlib.sha256(source_text.encode()).hexdigest()[:12]
    return f"source={digest} years={','.join(map(str, p['years']))}"


def render_mine(rows, header, now):
    out = [
        '# Your own events, from config/my-events.txt, written by `events refresh` and `events add`.',
        '# Do not edit: change config/my-events.txt and run `events refresh`.',
        f'# events-mine: {header}',
        '',
    ]
    for r in rows:
        k = f"holiday.{r['key']}"
        out.append(f"{k}.name={r['name']}")
        if r['start'] == r['end']:
            out.append(f"{k}.date={r['start'].strftime('%Y%m%d')}")
        else:
            out.append(f"{k}.start={r['start'].strftime('%Y%m%d')}")
            out.append(f"{k}.end={r['end'].strftime('%Y%m%d')}")
        out.append(f'{k}.kind=mine')
        if r['at']:
            out.append(f"{k}.at={r['at']}")
    return '\n'.join(out) + '\n'


def _read_my_events():
    f = my_events_file()
    return f.read_text(errors='replace') if f.is_file() else ''


def _problem_checks(problems):
    return [_check(f'{my_events_file().name}:{n}', WARN, f'{why} (line skipped)') for n, why in problems]


def write_mine(p, now):
    """Rebuild mine.rc from my-events.txt (no network). Always written, empty with no events."""
    text = _read_my_events()
    parsed, problems = parse_my_events(text)
    rows = mine_entries(parsed, p['years'])
    write_atomic(mine_file(), render_mine(rows, _mine_text(text, p), now))
    return _problem_checks(problems) + [_check(str(mine_file()), OK, f'{len(rows)} of your events written')]


# ── commands ──────────────────────────────────────────────────────────────────

def _check(name, status, detail):
    return {'check': name, 'status': status, 'detail': detail}


def refresh(source=None, now=None, settings_path=None, out_path=None, sky_path=None, fetch_holidays=True):
    """Outputs depend only on the settings, the sources and the year, never on when they're written:
    two stations sharing ~/.task (gittw) then write identical files, which git merges as no change."""
    now = now or dt.datetime.now()
    p = plan(read_settings(settings_path), now.date())
    out_path = Path(out_path or data_file())
    sky_path = Path(sky_path or sky_file())
    checks, records = [], []
    for locale in (p['locales'] if fetch_holidays else []):
        for year in p['years']:
            try:
                recs = fetch(locale, year, source)
            except NotPublished:
                if year == now.year:
                    checks.append(_check(f'{locale} {year}', FAIL,
                                         'holidata has no file for this locale (check events.locale)'))
                else:
                    checks.append(_check(f'{locale} {year}', OK, 'not published yet'))
                continue
            except (OSError, ValueError) as e:
                checks.append(_check(f'{locale} {year}', FAIL, f'fetch failed: {e}'))
                continue
            kept = [r for r in recs if keep(r, p)]
            records += kept
            checks.append(_check(f'{locale} {year}', OK, f'{len(kept)} of {len(recs)} kept'))
    if not fetch_holidays:
        checks.append(_check(str(out_path), OK, 'not fetched (--no-fetch)'))
    elif any(c['status'] == FAIL for c in checks):
        checks.append(_check(str(out_path), WARN, 'not changed (a fetch failed)'))
    else:
        rows = entries(records)
        write_atomic(out_path, render(rows, p, now))
        checks.append(_check(str(out_path), OK, f'{len(rows)} holidays written'))
    # The sky needs no network, so a failed fetch doesn't hold it back. Always written (empty when
    # both are off), so its include is always safe.
    sky = sky_entries(p)
    write_atomic(sky_path, render_sky(sky, p, now))
    what = ', '.join(w for w, on in (('moon phases', p['moon']), ('seasons', p['seasons']),
                                     ('clock changes', p['clock'])) if on)
    checks.append(_check(str(sky_path), OK, f'{len(sky)} {what} written' if what else 'moon, seasons and clock off'))
    if out_path == data_file():
        checks += write_mine(p, now)
        checks += _include_written(settings_path)
    return checks


def _include_written(settings_path=None):
    """An installed events.rc gets the include of each file that exists (mine.rc for a 0.2 install).
    A missing file's include waits, so none ever dangles."""
    sp = Path(settings_path or settings_file())
    if not sp.is_file():
        return []
    return [_check(str(sp), OK, f'added: {line}') for line in INCLUDES
            if (sp.parent / line.split()[1]).is_file() and _ensure_line(sp, line)]


def read_data(path=None):
    """One of our files: (header text or None, entries as dicts with start/end dates)."""
    path = Path(path or data_file())
    if not path.is_file():
        return None, []
    header, by_key = None, {}
    for line in path.read_text(errors='replace').splitlines():
        m = HEADER_RE.match(line)
        if m:
            header = m.group(1).strip()
            continue
        m = ENTRY_RE.match(line)
        if m:
            by_key.setdefault(m.group(1), {'key': m.group(1)})[m.group(2)] = m.group(3)
    rows = []
    for e in by_key.values():
        try:
            start = dt.datetime.strptime(e.get('start') or e['date'], '%Y%m%d').date()
            end = dt.datetime.strptime(e.get('end') or e.get('date') or e['start'], '%Y%m%d').date()
        except (KeyError, ValueError):
            continue
        rows.append({'kind': 'holiday', **e, 'start': start, 'end': end})
    rows.sort(key=lambda r: (r['start'], r.get('at', ''), r.get('region', ''), r.get('name', '')))
    return header, rows


def show(year=None):
    rows = read_data(data_file())[1] + read_data(sky_file())[1] + read_data(mine_file())[1]
    rows.sort(key=lambda r: (r['start'], r.get('at', ''), r.get('name', '')))
    if year:
        rows = [r for r in rows if r['start'].year <= year <= r['end'].year]
    return rows


def _task_holiday_keys():
    """Our holiday keys task itself sees (all includes followed), or None when task can't run."""
    try:
        r = subprocess.run(['task', 'rc.hooks=off', '_show'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return {m.group(1) for m in map(ENTRY_RE.match, r.stdout.splitlines()) if m}


def _coverage(name, rows, now, days, what):
    last = max(r['end'] for r in rows) if rows else None
    if last is None:
        return _check(name, WARN, f'no {what} in the file: run events refresh')
    if last < now.date() + dt.timedelta(days=days):
        return _check(name, WARN, f'{what} end {last}: run events refresh'
                                  + (' (holidata.net publishes about a year ahead)' if what == 'holidays' else ''))
    return _check(name, OK, f'{len(rows)} {what}, through {last}')


def check(days=60, now=None, settings_path=None, out_path=None, sky_path=None, task_keys=_task_holiday_keys):
    now = now or dt.datetime.now()
    settings_path = Path(settings_path or settings_file())
    out_path = Path(out_path or data_file())
    sky_path = Path(sky_path or sky_file())
    if not settings_path.is_file():
        if legacy_settings_file().is_file():
            return [_check('settings', WARN, 'holidata 0.1 settings found: run events init to move them '
                                             'to events.rc')]
        return [_check('settings', WARN, 'not set up yet: run events init --locale <xx-XX> '
                                         '[--regions R] [--moon yes] [--seasons yes] (installing fetches nothing)')]
    p = plan(read_settings(settings_path), now.date())
    header, rows = read_data(out_path)
    if header is None and not rows:
        return [_check('holidays', FAIL, f'{out_path} missing or empty: run events refresh')]
    checks = []
    if _strip_years(header) != _strip_years(_plan_text(p)):
        checks.append(_check('settings', WARN, 'events.rc changed since the last refresh: run events refresh'))
    else:
        checks.append(_check('settings', OK, _strip_years(_plan_text(p))))
    checks.append(_coverage('coverage', rows, now, days, 'holidays'))
    sky_header, sky = read_data(sky_path)
    if p['moon'] or p['seasons'] or p['clock']:
        if _strip_years(sky_header) != _strip_years(_sky_text(p)):
            checks.append(_check('sky', WARN, 'moon/seasons/clock changed since the last refresh: run events refresh'))
        elif p['moon'] or p['seasons']:
            checks.append(_coverage('sky', sky, now, days, 'moon phases and seasons'))
        else:
            checks.append(_check('sky', OK, f'{len(sky)} clock changes'))   # a zone without DST has none
    mine_header, mine_rows = read_data(mine_file())
    text = _read_my_events()
    checks += _problem_checks(parse_my_events(text)[1])
    if text.strip():
        if _strip_years(mine_header) != _strip_years(_mine_text(text, p)):
            checks.append(_check('my events', WARN, 'my-events.txt changed since mine.rc was written: '
                                                    'run events refresh'))
        else:
            checks.append(_check('my events', OK, f'{len(mine_rows)} entries'))
    keys = task_keys() if task_keys else None
    ours = {r['key'] for r in rows + sky + mine_rows}
    if keys is None:
        checks.append(_check('task sees them', WARN, 'could not run task'))
    elif ours and not keys & ours:
        checks.append(_check('task sees them', WARN,
                             f'no include for {settings_path}: run events init'))
    else:
        checks.append(_check('task sees them', OK, f'{len(keys & ours)} entries'))
    return checks


SETTINGS_TEMPLATE = """\
# events settings: run `events refresh` after a change. See ~/.task/docs/events_README.md.
events.locale  = {locale}
events.regions = {regions}
events.types   = {types}
events.years   = {years}
events.moon    = {moon}
events.seasons = {seasons}
events.clock   = {clock}
"""
MY_EVENTS_TEMPLATE = """\
# Your own events. Run `events refresh` after a change; `events add <id>` appends ghosts of tasks.
# A date, an optional time, the name; or a date on its own line with that day's events indented:
#
#   2026-10-29 14:30  Dentist
#   --04-12           Anne's birthday            (--MM-DD: every year)
#   2026-12-24..2026-12-27  Cottage              (a run of days)
#   2026-11-05
#     09:00  Board meeting
#     Library books due
#
# Lines starting with #, ; or * are comments.
"""
INCLUDES = ('include holidays.rc', 'include sky.rc', 'include mine.rc')   # relative: 2.6.2 resolves them from events.rc's folder


def _set_settings(path, values):
    """Write the given events.<key> values into the settings file, keeping everything else."""
    if not path.is_file():
        s = dict(DEFAULTS, **{k: v for k, v in values.items() if v is not None})
        write_atomic(path, SETTINGS_TEMPLATE.format(**s))
        return
    lines = path.read_text().splitlines()
    for k, v in values.items():
        if v is None:
            continue
        pat = re.compile(rf'^\s*events\.{k}\s*=')
        new = f'events.{k:<7} = {v}'
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = new
                break
        else:
            # before the includes, so the settings stay together at the top
            at = next((i for i, l in enumerate(lines) if l.strip().startswith('include ')), len(lines))
            lines.insert(at, new)
    write_atomic(path, '\n'.join(lines) + '\n')


def _ensure_line(path, line):
    """Append an active `line` unless it's there; a commented-out copy is re-enabled."""
    lines = path.read_text().splitlines() if path.is_file() else []
    if any(l.strip() == line for l in lines):
        return False
    for i, l in enumerate(lines):
        if re.match(rf'^\s*#\s*{re.escape(line)}\s*(#.*)?$', l):
            lines[i] = line
            break
    else:
        lines.append(line)
    write_atomic(path, '\n'.join(lines) + '\n')
    return True


def _drop_includes_of(path, name):
    """Remove active `include …/<name>` lines; returns how many."""
    if not path.is_file():
        return 0
    lines = path.read_text().splitlines()
    keep_ = [l for l in lines if not re.match(rf'^\s*include\s+\S*\b{re.escape(name)}\s*$', l)]
    if len(keep_) != len(lines):
        write_atomic(path, '\n'.join(keep_) + '\n')
    return len(lines) - len(keep_)


def _yes_no(v):
    return None if v is None else ('yes' if _yes(v) else 'no')


def init(locale=None, regions=None, types=None, years=None, moon=None, seasons=None, clock=None, source=None,
         now=None):
    sp, dp, kp, legacy = settings_file(), data_file(), sky_file(), legacy_settings_file()
    checks = []
    if not sp.is_file() and legacy.is_file():
        old = read_settings(legacy)
        _set_settings(sp, {k: old[k] for k in ('locale', 'regions', 'types', 'years')})
        checks.append(_check(str(sp), OK, f'settings moved over from {legacy.name}'))
    _set_settings(sp, {'locale': locale, 'regions': regions, 'types': types,
                       'years': None if years is None else str(years),
                       'moon': _yes_no(moon), 'seasons': _yes_no(seasons), 'clock': _yes_no(clock)})
    checks.append(_check(str(sp), OK, 'settings written'))
    checks += refresh(source, now)
    if not dp.is_file():
        # A first install that couldn't fetch: an empty file, so the include below is safe.
        p = plan(read_settings(sp), (now or dt.datetime.now()).date())
        write_atomic(dp, render([], p, now or dt.datetime.now()))
        checks.append(_check(str(dp), WARN, 'written empty: run events refresh when online'))
    if not my_events_file().exists():
        write_atomic(my_events_file(), MY_EVENTS_TEMPLATE)
        checks.append(_check(str(my_events_file()), OK, 'written (empty, with examples): your own events'))
        checks += write_mine(plan(read_settings(sp), (now or dt.datetime.now()).date()), now or dt.datetime.now())
    for line in INCLUDES:
        if _ensure_line(sp, line):
            checks.append(_check(str(sp), OK, f'added: {line}'))
    included = task_dir() / 'config' / 'included.rc'
    taskrc = Path(os.environ.get('TASKRC') or Path.home() / '.taskrc').expanduser()
    target = included if included.is_file() else taskrc
    home = str(Path.home())
    inc = 'include ' + (str(sp).replace(home, '~', 1) if str(sp).startswith(home + '/') else str(sp))
    if any(re.match(r'^\s*include\s+\S*\bevents\.rc\s*$', l)
           for l in (target.read_text().splitlines() if target.is_file() else [])):
        checks.append(_check(str(target), OK, 'already includes events.rc'))
    else:
        _ensure_line(target, inc)
        checks.append(_check(str(target), OK, f'added: {inc}'))
    # holidata 0.1: its include goes only once events.rc is in, then its settings file
    dropped = sum(_drop_includes_of(f, 'holidata.rc') for f in {included, taskrc})
    if legacy.is_file():
        legacy.unlink()
        checks.append(_check(str(legacy), OK, f'removed (holidata 0.1){", and its include" if dropped else ""}'))
    return checks


# ── ghosts: events add <id|uuid> ──────────────────────────────────────────────

_TASK_REF_RE = re.compile(r'^(?:\d+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F-]{4,})?)$')


def _export(refs):
    r = subprocess.run(['task', 'rc.hooks=off', 'rc.context=none', 'rc.confirmation=no', 'rc.verbose=nothing',
                        *refs, 'export'], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or f'task exited {r.returncode}')
    return json.loads(r.stdout or '[]')


def _local(stamp):
    return dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc).astimezone()


def ghost_lines(t):
    """A task → its lines in my-events.txt: the UUID and annotations as comments, then the event.
    Midnight (and 23:59:59, an end-of-day date) means no time."""
    when = _local(t.get('due') or t['scheduled'])
    time_ = '' if when.strftime('%H:%M:%S') in ('00:00:00', '23:59:59') else ' ' + when.strftime('%H:%M')
    name = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in t.get('description', '')).split())
    lines = [f"# ghost of {t['uuid']}  (events add, {dt.date.today().isoformat()})"]
    for a in t.get('annotations', []):
        note = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in a.get('description', '')).split())
        lines.append(f"# annotation {_local(a['entry']).strftime('%Y-%m-%d')}: {note}")
    lines.append(f"{when.strftime('%Y-%m-%d')}{time_}  {name}")
    return lines


def add(refs, now=None):
    """Append a ghost of each task to my-events.txt, then rebuild mine.rc."""
    now = now or dt.datetime.now()
    checks = [_check(f'task {r}', FAIL, 'not a task ID or UUID') for r in refs if not _TASK_REF_RE.match(r)]
    good = [r for r in refs if _TASK_REF_RE.match(r)]
    if not good:
        return checks
    try:
        tasks = _export(good)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return checks + [_check('task', FAIL, f'could not read the tasks: {e}')]
    text = _read_my_events()
    new = []
    for t in tasks:
        ref, label = t['uuid'], f"task {t.get('id') or t['uuid'][:8]}"
        if f'# ghost of {ref}' in text or any(f'# ghost of {ref}' in l for l in new):
            checks.append(_check(label, WARN, f'already a ghost: edit {my_events_file().name}'))
        elif t.get('status') == 'recurring':
            checks.append(_check(label, WARN, 'a recurring template: its dates are a rule, not a day'))
        elif not (t.get('due') or t.get('scheduled')):
            checks.append(_check(label, WARN, 'has no due or scheduled date'))
        else:
            lines = ghost_lines(t)
            new += [''] + lines
            checks.append(_check(label, OK, lines[-1]))
    found = {t['uuid'] for t in tasks} | {str(t.get('id')) for t in tasks} | {t['uuid'][:8] for t in tasks}
    checks += [_check(f'task {r}', WARN, 'no such task') for r in good if r not in found]
    if new:
        body = text if not text or text.endswith('\n') else text + '\n'
        write_atomic(my_events_file(), body + '\n'.join(new).lstrip('\n' if not body else '') + '\n')
        checks += [c for c in write_mine(plan(read_settings(), now.date()), now) if c['status'] != OK]
        checks += _include_written()
    return checks


# ── CLI ───────────────────────────────────────────────────────────────────────

def _worst(checks):
    return FAIL if any(c['status'] == FAIL for c in checks) else \
        WARN if any(c['status'] == WARN for c in checks) else OK


def _report(checks, as_json):
    worst = _worst(checks)
    if as_json:
        print(json.dumps({'status': worst, 'checks': checks}, indent=2))
    else:
        mark = {OK: '  ok ', WARN: ' warn', FAIL: ' FAIL'}
        for c in checks:
            print(f'{mark[c["status"]]}  {c["check"]} — {c["detail"]}')
    return {OK: 0, WARN: 1, FAIL: 2}[worst]


def _local_time(at):
    try:
        t = dt.datetime.strptime(at, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return ''
    return t.astimezone().strftime('%H:%M')


def _show_report(rows, as_json):
    if as_json:
        print(json.dumps([{**r, 'start': r['start'].isoformat(), 'end': r['end'].isoformat()}
                          for r in rows], indent=2, ensure_ascii=False))
        return 0
    if not rows:
        if settings_file().is_file():
            print('Nothing installed. Run events refresh.')
        else:
            print('Not set up yet. Run events init --locale <xx-XX> [--regions R] [--moon yes] [--seasons yes],'
                  ' e.g. --locale en-CA --regions ON.')
        return 1
    for r in rows:
        if r.get('at'):
            when = f"{r['start']} {_local_time(r['at'])}"
            tags = r.get('kind', '')
        else:
            when = str(r['start']) if r['start'] == r['end'] else f"{r['start']} – {r['end']}"
            tags = r['kind'] if r.get('kind', 'holiday') != 'holiday' else \
                ' '.join(x for x in (r.get('region', ''), r.get('type', '')) if x)
        print(f"{when:<23} {r.get('name', '')}" + (f'  ({tags})' if tags else ''))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='events', description=__doc__.split('\n\n')[0])
    p.add_argument('--version', action='version', version=f'events {__version__}')
    sub = p.add_subparsers(dest='cmd')
    rf = sub.add_parser('refresh', help='fetch the holidays, compute the sky, rewrite both files')
    rf.add_argument('--source', help='read DIR/<locale>/<year>.json instead of the site (tests, offline)')
    rf.add_argument('--no-fetch', action='store_true',
                    help='keep the holidays as they are; rebuild the sky and your events (no network)')
    rf.add_argument('--json', action='store_true')
    it = sub.add_parser('init', help='set up or change the settings, refresh, add the includes')
    it.add_argument('--locale', help='holidata.net locale, or a comma list (default en-US)')
    it.add_argument('--regions', help='region codes to keep besides national holidays, comma list')
    it.add_argument('--types', help='keep only these type letters (N R F V)')
    it.add_argument('--years', type=int, help='this year and the next N-1 (default 2)')
    it.add_argument('--moon', metavar='yes|no', help='the moon\'s four phases (default no)')
    it.add_argument('--seasons', metavar='yes|no', help='solstices and equinoxes (default no)')
    it.add_argument('--clock', metavar='yes|no', help='the days the clocks change (default no)')
    it.add_argument('--source', help=argparse.SUPPRESS)
    it.add_argument('--json', action='store_true')
    sh = sub.add_parser('show', help='list what is installed')
    sh.add_argument('--year', type=int)
    sh.add_argument('--json', action='store_true')
    ck = sub.add_parser('check', help='warn when the entries run out or task does not see them')
    ck.add_argument('--days', type=int, default=60, help='warn when they end within this many days')
    ck.add_argument('--json', action='store_true')
    ad = sub.add_parser('add', help='a ghost of each task: stays on the calendar after the task is gone')
    ad.add_argument('tasks', nargs='+', metavar='id|uuid')
    ad.add_argument('--json', action='store_true')
    a = p.parse_args(argv)

    if a.cmd == 'refresh':
        return _report(refresh(a.source, fetch_holidays=not a.no_fetch), a.json)
    if a.cmd == 'init':
        return _report(init(a.locale, a.regions, a.types, a.years, a.moon, a.seasons, a.clock,
                            source=a.source), a.json)
    if a.cmd == 'add':
        return _report(add(a.tasks), a.json)
    if a.cmd == 'check':
        return _report(check(a.days), a.json)
    return _show_report(show(getattr(a, 'year', None)), getattr(a, 'json', False))


if __name__ == '__main__':
    sys.exit(main())
