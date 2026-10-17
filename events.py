#!/usr/bin/env python3
"""
events — the calendar's non-task entries, as Taskwarrior 2.6.2 `holiday.*` entries,
so `task calendar`, tw-web and other clients all show the same days: public holidays
from holidata.net, and (computed, no network) the moon's phases and the solstices
and equinoxes.

Commands
  events init [--locale L] [--regions R] [--types T] [--years N]
              [--moon yes|no] [--seasons yes|no] [--json]
                                  Set up (or change) the settings, refresh, then
                                  add the includes, so none ever points at a
                                  missing file. Moves holidata 0.1 settings over.
                                  Safe to run again.
  events refresh [--source DIR] [--json]
                                  Fetch the holidays for this year and the next
                                  (events.years), keep national ones plus
                                  events.regions, rewrite config/holidays.rc; and
                                  compute config/sky.rc. A failed fetch keeps the
                                  old holidays.
  events show [--year Y] [--json] List what's installed (the default).
  events check [--days N] [--json]
                                  Read-only. Warn when the entries run out within
                                  N days (default 60), when the settings changed
                                  since the last refresh, or when task doesn't
                                  see them.

Settings live in config/events.rc, which config/included.rc includes and which
includes holidays.rc and sky.rc. All under the task dir: $TW_TASK_DIR, else
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
import urllib.error
import urllib.request
from pathlib import Path

__version__ = '0.2.0'

OK, WARN, FAIL = 'ok', 'warn', 'fail'
BASE_URL = 'https://holidata.net'
DEFAULTS = {'locale': 'en-US', 'regions': '', 'types': '', 'years': '2', 'moon': 'no', 'seasons': 'no'}
HEADER_RE = re.compile(r'^# (?:holidata|events|events-sky): (.*)$')
ENTRY_RE = re.compile(r'^holiday\.((?:hd|mn|ss)\d{8}[a-z]+)\.'
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
    }


def _plan_text(p):
    """The holidays file's header line, which check compares."""
    return (f"locale={','.join(p['locales'])} regions={','.join(p['regions'])} "
            f"types={''.join(p['types'])} years={','.join(map(str, p['years']))}")


def _sky_text(p):
    return (f"moon={'yes' if p['moon'] else 'no'} seasons={'yes' if p['seasons'] else 'no'} "
            f"years={','.join(map(str, p['years']))}")


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
        f'# generated {now.strftime("%Y-%m-%d %H:%M")}',
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



def sky_entries(p):
    """The moon's phases and the seasons for the planned years, as entries: the date is the local
    day of the moment (this machine's time zone), `at` the moment in UTC, `kind` moon or season."""
    rows = []
    for year in p['years']:
        if p['moon']:
            rows += [(when, name, 'moon', 'mn') for when, name in moon_phases(year)]
        if p['seasons']:
            rows += [(when, name, 'season', 'ss') for when, name in seasons(year)]
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
        '# Moon phases, solstices and equinoxes, computed by `events refresh` (Meeus, no network).',
        '# Do not edit: change events.moon / events.seasons in config/events.rc and run `events refresh`.',
        f'# events-sky: {_sky_text(p)}',
        f'# generated {now.strftime("%Y-%m-%d %H:%M")}',
        '',
    ]
    for r in rows:
        k = f"holiday.{r['key']}"
        out += [f"{k}.name={r['name']}", f"{k}.date={r['start'].strftime('%Y%m%d')}",
                f"{k}.kind={r['kind']}", f"{k}.at={r['at']}"]
    return '\n'.join(out) + '\n'


# ── commands ──────────────────────────────────────────────────────────────────

def _check(name, status, detail):
    return {'check': name, 'status': status, 'detail': detail}


def refresh(source=None, now=None, settings_path=None, out_path=None, sky_path=None):
    now = now or dt.datetime.now()
    p = plan(read_settings(settings_path), now.date())
    out_path = Path(out_path or data_file())
    sky_path = Path(sky_path or sky_file())
    checks, records = [], []
    for locale in p['locales']:
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
    if any(c['status'] == FAIL for c in checks):
        checks.append(_check(str(out_path), WARN, 'not changed (a fetch failed)'))
    else:
        rows = entries(records)
        write_atomic(out_path, render(rows, p, now))
        checks.append(_check(str(out_path), OK, f'{len(rows)} holidays written'))
    # The sky needs no network, so a failed fetch doesn't hold it back. Always written (empty when
    # both are off), so its include is always safe.
    sky = sky_entries(p)
    write_atomic(sky_path, render_sky(sky, p, now))
    what = ' and '.join(w for w, on in (('moon phases', p['moon']), ('seasons', p['seasons'])) if on)
    checks.append(_check(str(sky_path), OK, f'{len(sky)} {what} written' if what else 'moon and seasons off'))
    return checks


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
    rows = read_data(data_file())[1] + read_data(sky_file())[1]
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
    if p['moon'] or p['seasons']:
        if _strip_years(sky_header) != _strip_years(_sky_text(p)):
            checks.append(_check('sky', WARN, 'moon/seasons changed since the last refresh: run events refresh'))
        else:
            checks.append(_coverage('sky', sky, now, days, 'moon phases and seasons'))
    keys = task_keys() if task_keys else None
    ours = {r['key'] for r in rows + sky}
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
"""
INCLUDES = ('include holidays.rc', 'include sky.rc')   # relative: 2.6.2 resolves them from events.rc's folder


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


def init(locale=None, regions=None, types=None, years=None, moon=None, seasons=None, source=None, now=None):
    sp, dp, kp, legacy = settings_file(), data_file(), sky_file(), legacy_settings_file()
    checks = []
    if not sp.is_file() and legacy.is_file():
        old = read_settings(legacy)
        _set_settings(sp, {k: old[k] for k in ('locale', 'regions', 'types', 'years')})
        checks.append(_check(str(sp), OK, f'settings moved over from {legacy.name}'))
    _set_settings(sp, {'locale': locale, 'regions': regions, 'types': types,
                       'years': None if years is None else str(years),
                       'moon': _yes_no(moon), 'seasons': _yes_no(seasons)})
    checks.append(_check(str(sp), OK, 'settings written'))
    checks += refresh(source, now)
    if not dp.is_file():
        # A first install that couldn't fetch: an empty file, so the include below is safe.
        p = plan(read_settings(sp), (now or dt.datetime.now()).date())
        write_atomic(dp, render([], p, now or dt.datetime.now()))
        checks.append(_check(str(dp), WARN, 'written empty: run events refresh when online'))
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
            tags = ' '.join(x for x in (r.get('region', ''), r.get('type', '')) if x)
        print(f"{when:<23} {r.get('name', '')}" + (f'  ({tags})' if tags else ''))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='events', description=__doc__.split('\n\n')[0])
    p.add_argument('--version', action='version', version=f'events {__version__}')
    sub = p.add_subparsers(dest='cmd')
    rf = sub.add_parser('refresh', help='fetch the holidays, compute the sky, rewrite both files')
    rf.add_argument('--source', help='read DIR/<locale>/<year>.json instead of the site (tests, offline)')
    rf.add_argument('--json', action='store_true')
    it = sub.add_parser('init', help='set up or change the settings, refresh, add the includes')
    it.add_argument('--locale', help='holidata.net locale, or a comma list (default en-US)')
    it.add_argument('--regions', help='region codes to keep besides national holidays, comma list')
    it.add_argument('--types', help='keep only these type letters (N R F V)')
    it.add_argument('--years', type=int, help='this year and the next N-1 (default 2)')
    it.add_argument('--moon', metavar='yes|no', help='the moon\'s four phases (default no)')
    it.add_argument('--seasons', metavar='yes|no', help='solstices and equinoxes (default no)')
    it.add_argument('--source', help=argparse.SUPPRESS)
    it.add_argument('--json', action='store_true')
    sh = sub.add_parser('show', help='list what is installed')
    sh.add_argument('--year', type=int)
    sh.add_argument('--json', action='store_true')
    ck = sub.add_parser('check', help='warn when the entries run out or task does not see them')
    ck.add_argument('--days', type=int, default=60, help='warn when they end within this many days')
    ck.add_argument('--json', action='store_true')
    a = p.parse_args(argv)

    if a.cmd == 'refresh':
        return _report(refresh(a.source), a.json)
    if a.cmd == 'init':
        return _report(init(a.locale, a.regions, a.types, a.years, a.moon, a.seasons, a.source), a.json)
    if a.cmd == 'check':
        return _report(check(a.days), a.json)
    return _show_report(show(getattr(a, 'year', None)), getattr(a, 'json', False))


if __name__ == '__main__':
    sys.exit(main())
