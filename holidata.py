#!/usr/bin/env python3
"""
holidata — public holidays from holidata.net, as Taskwarrior 2.6.2 `holiday.*`
entries, so `task calendar`, tw-web and other clients all show the same days.

Commands
  holidata refresh [--source DIR] [--json]
                                  Fetch this year and the next (holidata.years),
                                  keep national holidays plus holidata.regions,
                                  and rewrite config/holidays.rc. A failed fetch
                                  keeps the old file.
  holidata init [--locale L] [--regions R] [--types T] [--years N] [--json]
                                  Set up (or change) the settings, refresh, then
                                  add the includes, so none ever points at a
                                  missing file. Safe to run again.
  holidata show [--year Y] [--json]
                                  List the installed holidays (the default).
  holidata check [--days N] [--json]
                                  Read-only. Warn when the holidays run out
                                  within N days (default 60), when the settings
                                  changed since the last refresh, or when task
                                  doesn't see them.

Settings live in config/holidata.rc (holidata.locale, holidata.regions,
holidata.types, holidata.years); the data goes to config/holidays.rc.
config/included.rc includes holidata.rc, which includes holidays.rc. Both are under the task dir: $TW_TASK_DIR, else
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

__version__ = '0.1.0'

OK, WARN, FAIL = 'ok', 'warn', 'fail'
BASE_URL = 'https://holidata.net'
DEFAULTS = {'locale': 'en-US', 'regions': '', 'types': '', 'years': '2'}
HEADER_RE = re.compile(r'^# holidata: (.*)$')
ENTRY_RE = re.compile(r'^holiday\.(hd\d{8}[a-z]+)\.(name|date|start|end|type|region|locale)=(.*)$')


# ── paths and settings ────────────────────────────────────────────────────────

def task_dir():
    return Path(os.environ.get('TW_TASK_DIR') or Path.home() / '.task').expanduser()


def settings_file():
    return task_dir() / 'config' / 'holidata.rc'


def data_file():
    return task_dir() / 'config' / 'holidays.rc'


def read_settings(path=None):
    """holidata.<key> = value lines; `#` starts a comment, as in Taskwarrior."""
    path = Path(path or settings_file())
    s = dict(DEFAULTS)
    if path.is_file():
        for line in path.read_text(errors='replace').splitlines():
            line = line.split('#', 1)[0].strip()
            if '=' not in line:
                continue
            k, v = (x.strip() for x in line.split('=', 1))
            if k.startswith('holidata.'):
                s[k[len('holidata.'):]] = v
    return s


def _list(value):
    return [x.strip() for x in value.split(',') if x.strip()]


def plan(settings, today):
    """What a refresh fetches and keeps, as one comparable dict."""
    try:
        years = max(1, int(settings.get('years') or 2))
    except ValueError:
        years = 2
    return {
        'locales': _list(settings.get('locale', '')) or [DEFAULTS['locale']],
        'regions': sorted(_list(settings.get('regions', ''))),
        'types':   sorted(set(''.join(_list(settings.get('types', ''))).upper())),
        'years':   [today.year + i for i in range(years)],
    }


def _plan_text(p):
    """The header line a refresh writes, and check compares."""
    return (f"locale={','.join(p['locales'])} regions={','.join(p['regions'])} "
            f"types={''.join(p['types'])} years={','.join(map(str, p['years']))}")


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
        req = urllib.request.Request(url, headers={'User-Agent': f'tw-holidata/{__version__}'})
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
        '# Public holidays from https://holidata.net, written by `holidata refresh`.',
        '# Do not edit: change config/holidata.rc and run `holidata refresh`.',
        f'# holidata: {_plan_text(p)}',
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
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.holidays.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── commands ──────────────────────────────────────────────────────────────────

def _check(name, status, detail):
    return {'check': name, 'status': status, 'detail': detail}


def refresh(source=None, now=None, settings_path=None, out_path=None):
    now = now or dt.datetime.now()
    p = plan(read_settings(settings_path), now.date())
    out_path = Path(out_path or data_file())
    checks, records = [], []
    for locale in p['locales']:
        for year in p['years']:
            try:
                recs = fetch(locale, year, source)
            except NotPublished:
                if year == now.year:
                    checks.append(_check(f'{locale} {year}', FAIL,
                                         'holidata has no file for this locale (check holidata.locale)'))
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
        return checks
    rows = entries(records)
    write_atomic(out_path, render(rows, p, now))
    checks.append(_check(str(out_path), OK, f'{len(rows)} holidays written'))
    return checks


def read_data(path=None):
    """The installed file: (header plan text or None, entries as dicts with start/end dates)."""
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
        rows.append({**e, 'start': start, 'end': end})
    rows.sort(key=lambda r: (r['start'], r.get('region', ''), r.get('name', '')))
    return header, rows


def show(year=None):
    _, rows = read_data()
    if year:
        rows = [r for r in rows if r['start'].year <= year <= r['end'].year]
    return rows


def _task_holiday_keys():
    """The hd* holiday keys task itself sees (all includes followed), or None when task can't run."""
    try:
        r = subprocess.run(['task', 'rc.hooks=off', '_show'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return {m.group(1) for m in map(ENTRY_RE.match, r.stdout.splitlines()) if m}


def check(days=60, now=None, settings_path=None, out_path=None, task_keys=_task_holiday_keys):
    now = now or dt.datetime.now()
    settings_path = Path(settings_path or settings_file())
    out_path = Path(out_path or data_file())
    if not settings_path.is_file():
        return [_check('settings', WARN, f'{settings_path} not found (install with tw -I holidata)')]
    p = plan(read_settings(settings_path), now.date())
    header, rows = read_data(out_path)
    checks = []
    if header is None and not rows:
        return [_check('holidays', FAIL, f'{out_path} missing or empty: run holidata refresh')]
    want = _plan_text(p)
    # A new year moves the years along on its own; only the other settings must match.
    strip = lambda t: re.sub(r'\s*years=\S*', '', t or '')
    if strip(header) != strip(want):
        checks.append(_check('settings', WARN, 'holidata.rc changed since the last refresh: run holidata refresh'))
    else:
        checks.append(_check('settings', OK, strip(want)))
    last = max(r['end'] for r in rows) if rows else None
    if last is None:
        checks.append(_check('coverage', WARN, 'no holidays in the file: run holidata refresh'))
    elif last < now.date() + dt.timedelta(days=days):
        checks.append(_check('coverage', WARN, f'holidays end {last}: run holidata refresh '
                                               '(holidata.net publishes about a year ahead)'))
    else:
        checks.append(_check('coverage', OK, f'{len(rows)} holidays, through {last}'))
    keys = task_keys() if task_keys else None
    if keys is None:
        checks.append(_check('task sees them', WARN, 'could not run task'))
    elif rows and not keys & {r['key'] for r in rows}:
        checks.append(_check('task sees them', WARN,
                             f'no include for {out_path}: add it to config/included.rc'))
    else:
        checks.append(_check('task sees them', OK, f'{len(keys)} holiday entries'))
    return checks


SETTINGS_TEMPLATE = '''\
# holidata settings: run `holidata refresh` after a change. See ~/.task/docs/holidata_README.md.
holidata.locale  = {locale}
holidata.regions = {regions}
holidata.types   = {types}
holidata.years   = {years}
'''
INCLUDE_DATA = 'include holidays.rc'          # relative: 2.6.2 resolves it from holidata.rc's folder


def _set_settings(path, values):
    """Write the given holidata.<key> values into the settings file, keeping everything else."""
    if not path.is_file():
        s = dict(DEFAULTS, **{k: v for k, v in values.items() if v is not None})
        write_atomic(path, SETTINGS_TEMPLATE.format(**s))
        return
    lines = path.read_text().splitlines()
    for k, v in values.items():
        if v is None:
            continue
        pat = re.compile(rf'^\s*holidata\.{k}\s*=')
        new = f'holidata.{k} = {v}'
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = new
                break
        else:
            lines.append(new)
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


def init(locale=None, regions=None, types=None, years=None, source=None, now=None):
    sp, dp = settings_file(), data_file()
    _set_settings(sp, {'locale': locale, 'regions': regions, 'types': types,
                       'years': None if years is None else str(years)})
    checks = [_check(str(sp), OK, 'settings written')]
    checks += refresh(source, now)
    if not dp.is_file():
        # A first install that couldn't fetch: an empty file, so the include below is safe.
        p = plan(read_settings(sp), (now or dt.datetime.now()).date())
        write_atomic(dp, render([], p, now or dt.datetime.now()))
        checks.append(_check(str(dp), WARN, 'written empty: run holidata refresh when online'))
    if _ensure_line(sp, INCLUDE_DATA):
        checks.append(_check(str(sp), OK, f'added: {INCLUDE_DATA}'))
    included = task_dir() / 'config' / 'included.rc'
    target = included if included.is_file() else \
        Path(os.environ.get('TASKRC') or Path.home() / '.taskrc').expanduser()
    home = str(Path.home())
    inc = 'include ' + (str(sp).replace(home, '~', 1) if str(sp).startswith(home + '/') else str(sp))
    if any(re.match(rf'^\s*include\s+\S*holidata\.rc\s*$', l)
           for l in (target.read_text().splitlines() if target.is_file() else [])):
        checks.append(_check(str(target), OK, 'already includes holidata.rc'))
    else:
        _ensure_line(target, inc)
        checks.append(_check(str(target), OK, f'added: {inc}'))
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


def _show_report(rows, as_json):
    if as_json:
        print(json.dumps([{**r, 'start': r['start'].isoformat(), 'end': r['end'].isoformat()}
                          for r in rows], indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print('No holidays installed. Run holidata refresh.')
        return 1
    for r in rows:
        when = str(r['start']) if r['start'] == r['end'] else f"{r['start']} – {r['end']}"
        tags = ' '.join(x for x in (r.get('region', ''), r.get('type', '')) if x)
        print(f"{when:<23} {r.get('name', '')}" + (f'  ({tags})' if tags else ''))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='holidata', description=__doc__.split('\n\n')[0])
    p.add_argument('--version', action='version', version=f'holidata {__version__}')
    sub = p.add_subparsers(dest='cmd')
    rf = sub.add_parser('refresh', help='fetch from holidata.net and rewrite config/holidays.rc')
    rf.add_argument('--source', help='read DIR/<locale>/<year>.json instead of the site (tests, offline)')
    rf.add_argument('--json', action='store_true')
    it = sub.add_parser('init', help='set up or change the settings, refresh, add the includes')
    it.add_argument('--locale', help='holidata.net locale, or a comma list (default en-US)')
    it.add_argument('--regions', help='region codes to keep besides national holidays, comma list')
    it.add_argument('--types', help='keep only these type letters (N R F V)')
    it.add_argument('--years', type=int, help='this year and the next N-1 (default 2)')
    it.add_argument('--source', help=argparse.SUPPRESS)
    it.add_argument('--json', action='store_true')
    sh = sub.add_parser('show', help='list the installed holidays')
    sh.add_argument('--year', type=int)
    sh.add_argument('--json', action='store_true')
    ck = sub.add_parser('check', help='warn when the holidays run out or task does not see them')
    ck.add_argument('--days', type=int, default=60, help='warn when they end within this many days')
    ck.add_argument('--json', action='store_true')
    a = p.parse_args(argv)

    if a.cmd == 'refresh':
        return _report(refresh(a.source), a.json)
    if a.cmd == 'init':
        return _report(init(a.locale, a.regions, a.types, a.years, a.source), a.json)
    if a.cmd == 'check':
        return _report(check(a.days), a.json)
    return _show_report(show(getattr(a, 'year', None)), getattr(a, 'json', False))


if __name__ == '__main__':
    sys.exit(main())
