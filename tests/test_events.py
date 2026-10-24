"""events: fixtures are real holidata.net files (en-CA 2026, 2027). Tests never touch the
network or ~/.task: TW_TASK_DIR points at a temp folder and refresh reads --source."""
import datetime as dt
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
spec = importlib.util.spec_from_file_location("events", ROOT / "events.py")
events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(events)

NOW = dt.datetime(2026, 10, 17, 9, 0)


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    monkeypatch.setenv('TW_TASK_DIR', str(tmp_path / 'task'))
    (tmp_path / 'task' / 'config').mkdir(parents=True)
    monkeypatch.setattr(events.urllib.request, 'urlopen',
                        lambda *a, **k: pytest.fail('a test reached the network'))
    return tmp_path / 'task'


@pytest.fixture
def tz(monkeypatch):
    """Set this process's local zone for one test, and put it back even if the test fails."""
    import time
    def set_(name):
        monkeypatch.setenv('TZ', name)
        time.tzset()
    yield set_
    monkeypatch.undo()
    time.tzset()


def settings(tdir, body):
    (tdir / 'config' / 'events.rc').write_text(body)


def source(tmp_path, records_by_year, locale='xx-XX'):
    d = tmp_path / 'src' / locale
    d.mkdir(parents=True, exist_ok=True)
    for year, recs in records_by_year.items():
        (d / f'{year}.json').write_text(''.join(json.dumps(r) + '\n' for r in recs))
    return tmp_path / 'src'


def rec(date, name, region='', type_='NF', locale='xx-XX'):
    return {'locale': locale, 'region': region, 'date': date, 'description': name, 'type': type_, 'notes': ''}


# ── refresh ───────────────────────────────────────────────────────────────────

def test_refresh_keeps_national_and_configured_regions(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON   # Ontario\n')
    checks = events.refresh(FIXTURES, now=NOW)
    assert events._worst(checks) == events.OK, checks
    _, rows = events.read_data()
    names_2026 = [r['name'] for r in rows if r['start'].year == 2026]
    assert names_2026 == ["New Year's Day", 'Family Day', 'Good Friday', 'Victoria Day', 'Canada Day',
                          'Labour Day', 'Thanksgiving Day', 'Christmas Day', 'Boxing Day']
    assert {r.get('region', '') for r in rows} == {'', 'ON'}
    assert {r['start'].year for r in rows} == {2026, 2027}


def test_types_filter(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\nevents.types = N\n')
    events.refresh(FIXTURES, now=NOW)
    _, rows = events.read_data()
    assert rows and all('N' in r['type'] for r in rows)


def test_written_file_is_taskwarrior_holiday_entries(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\n')
    events.refresh(FIXTURES, now=NOW)
    text = (tdir / 'config' / 'holidays.rc').read_text()
    assert '# events: locale=en-CA regions=ON types= years=2026,2027' in text
    assert "holiday.hd20260101a.name=New Year's Day\nholiday.hd20260101a.date=20260101\n" in text
    assert 'holiday.hd20261012a.region=ON' in text
    assert 'holiday.hd20260101a.type=NF' in text


def test_same_day_gets_distinct_keys(tmp_path, tdir):
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-03-01', 'One'), rec('2026-03-01', 'Two')]})
    events.refresh(src, now=NOW)
    _, rows = events.read_data()
    assert [r['key'] for r in rows] == ['hd20260301a', 'hd20260301b']


def test_consecutive_days_become_a_range(tmp_path, tdir):
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-12-24', 'Break'), rec('2026-12-25', 'Break'),
                                   rec('2026-12-26', 'Break'), rec('2026-12-28', 'Break')]})
    events.refresh(src, now=NOW)
    text = (tdir / 'config' / 'holidays.rc').read_text()
    assert 'holiday.hd20261224a.start=20261224\nholiday.hd20261224a.end=20261226\n' in text
    assert 'holiday.hd20261228a.date=20261228' in text


def test_names_cannot_break_out_of_their_line(tmp_path, tdir):
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-05-01', 'Day\ninclude /etc/passwd'),
                                   rec('2026-05-02', 'Day #2')]})
    events.refresh(src, now=NOW)
    lines = (tdir / 'config' / 'holidays.rc').read_text().splitlines()
    assert not any(l.startswith('include') for l in lines)
    assert 'holiday.hd20260501a.name=Day include /etc/passwd' in lines
    assert 'holiday.hd20260502a.name=Day ＃2' in lines


def test_next_year_not_published_is_fine(tmp_path, tdir):
    settings(tdir, 'events.locale = xx-XX\n')
    src = source(tmp_path, {2026: [rec('2026-01-01', 'New Year')]})
    checks = events.refresh(src, now=NOW)
    assert events._worst(checks) == events.OK
    assert any('not published yet' in c['detail'] for c in checks)


def test_unknown_locale_fails_and_keeps_the_old_file(tmp_path, tdir):
    old = tdir / 'config' / 'holidays.rc'
    old.write_text('# old\n')
    settings(tdir, 'events.locale = zz-ZZ\n')
    checks = events.refresh(tmp_path / 'empty', now=NOW)
    assert events._worst(checks) == events.FAIL
    assert old.read_text() == '# old\n'


def test_fetch_error_keeps_the_old_file(tdir, monkeypatch):
    old = tdir / 'config' / 'holidays.rc'
    old.write_text('# old\n')
    settings(tdir, 'events.locale = en-CA\n')

    def boom(*a, **k):
        raise events.urllib.error.URLError('offline')
    monkeypatch.setattr(events.urllib.request, 'urlopen', boom)
    assert events.main(['refresh']) == 2
    assert old.read_text() == '# old\n'


def test_two_locales(tmp_path, tdir):
    settings(tdir, 'events.locale = xx-XX, yy-YY\nevents.years = 1\n')
    source(tmp_path, {2026: [rec('2026-07-01', 'Fête')]}, 'xx-XX')
    src = source(tmp_path, {2026: [rec('2026-07-01', 'Feast', locale='yy-YY')]}, 'yy-YY')
    events.refresh(src, now=NOW)
    _, rows = events.read_data()
    assert [(r['name'], r['locale']) for r in rows] == [('Feast', 'yy-YY'), ('Fête', 'xx-XX')]


# ── show and check ────────────────────────────────────────────────────────────

def test_show_year_json(tdir, capsys):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\n')
    events.refresh(FIXTURES, now=NOW)
    assert events.main(['show', '--year', '2027', '--json']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]['start'] == '2027-01-01' and all(r['start'].startswith('2027') for r in out)


def test_check_ok_after_refresh(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\n')
    events.refresh(FIXTURES, now=NOW)
    checks = events.check(now=NOW, task_keys=lambda: {'hd20260101a'})
    assert events._worst(checks) == events.OK, checks


def test_check_warns_when_holidays_run_out(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\n')
    events.refresh(FIXTURES, now=NOW)
    late = dt.datetime(2027, 11, 15)
    checks = events.check(now=late, task_keys=lambda: {'hd20260101a'})
    cov = next(c for c in checks if c['check'] == 'coverage')
    assert cov['status'] == events.WARN and '2027-12-26' in cov['detail']
    # A new year alone doesn't count as changed settings.
    assert next(c for c in checks if c['check'] == 'settings')['status'] == events.OK


def test_check_warns_on_changed_settings(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\n')
    events.refresh(FIXTURES, now=NOW)
    settings(tdir, 'events.locale = en-CA\nevents.regions = QC\n')
    checks = events.check(now=NOW, task_keys=lambda: {'hd20260101a'})
    assert next(c for c in checks if c['check'] == 'settings')['status'] == events.WARN


def test_check_warns_when_task_does_not_see_them(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\n')
    events.refresh(FIXTURES, now=NOW)
    checks = events.check(now=NOW, task_keys=lambda: set())
    c = next(c for c in checks if c['check'] == 'task sees them')
    assert c['status'] == events.WARN and 'include' in c['detail']


def test_check_before_init_says_run_init(tdir):
    # Installed (this is events running) but never set up: tw -I alone writes nothing.
    checks = events.check(now=NOW, task_keys=None)
    assert events._worst(checks) == events.WARN
    assert 'events init --locale' in checks[0]['detail'] and 'tw -I' not in checks[0]['detail']


def test_show_before_init_says_run_init(tdir, capsys):
    assert events.main(['show']) == 1
    assert 'events init' in capsys.readouterr().out


# ── the real binary ───────────────────────────────────────────────────────────

@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_task_reads_the_file(tmp_path, tdir, monkeypatch):
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-10-12', 'Thanks'), rec('2026-12-24', 'Break'),
                                   rec('2026-12-25', 'Break')]})
    events.refresh(src, now=NOW)
    rc = tmp_path / 'taskrc'
    (tmp_path / 'data').mkdir()
    rc.write_text(f"data.location={tmp_path / 'data'}\nhooks=off\nconfirmation=no\n"
                  f"calendar.holidays=full\ninclude {tdir / 'config' / 'holidays.rc'}\n")
    monkeypatch.setenv('TASKRC', str(rc))
    out = subprocess.run(['task', 'rc.color=off', 'calendar', '12', '2026'],
                         capture_output=True, text=True, timeout=15).stdout
    assert 'Start of Break' in out and 'End of Break' in out
    keys = events._task_holiday_keys()
    assert keys == {'hd20261012a', 'hd20261224a'}


# ── init ──────────────────────────────────────────────────────────────────────

def _init(tdir, *args):
    (tdir / 'config' / 'included.rc').touch()
    return events.init(*args, source=FIXTURES, now=NOW)


def test_init_writes_settings_data_and_includes(tdir):
    checks = _init(tdir, 'en-CA', 'ON')
    assert events._worst(checks) == events.OK, checks
    s = events.read_settings()
    assert (s['locale'], s['regions']) == ('en-CA', 'ON')
    assert (tdir / 'config' / 'holidays.rc').is_file()
    assert 'include holidays.rc' in (tdir / 'config' / 'events.rc').read_text().splitlines()
    assert f"include {tdir / 'config' / 'events.rc'}" in (tdir / 'config' / 'included.rc').read_text()


def test_init_twice_changes_only_what_is_given(tdir):
    _init(tdir, 'en-CA', 'ON')
    sp = tdir / 'config' / 'events.rc'
    sp.write_text(sp.read_text() + '# my note\n')
    _init(tdir, None, 'QC')
    text = sp.read_text()
    assert 'events.locale  = en-CA' in text and 'events.regions = QC' in text and '# my note' in text
    assert text.count('include holidays.rc') == 1
    assert (tdir / 'config' / 'included.rc').read_text().count('events.rc') == 1


def test_init_reenables_a_commented_include(tdir):
    (tdir / 'config' / 'included.rc').write_text(
        f"#include {tdir / 'config' / 'events.rc'}   # removed with tw -r\n")
    events.init('en-CA', 'ON', source=FIXTURES, now=NOW)
    lines = (tdir / 'config' / 'included.rc').read_text().splitlines()
    assert lines == [f"include {tdir / 'config' / 'events.rc'}"]


def test_init_offline_still_leaves_no_dangling_include(tmp_path, tdir):
    (tdir / 'config' / 'included.rc').touch()
    checks = events.init('en-CA', 'ON', source=tmp_path / 'nothing', now=NOW)
    assert events._worst(checks) == events.FAIL
    assert (tdir / 'config' / 'holidays.rc').is_file()
    assert 'include holidays.rc' in (tdir / 'config' / 'events.rc').read_text()


@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_task_sees_holidays_after_init(tmp_path, tdir, monkeypatch):
    rc = tmp_path / 'taskrc'
    (tmp_path / 'data').mkdir()
    rc.write_text(f"data.location={tmp_path / 'data'}\nhooks=off\nconfirmation=no\n"
                  f"include {tdir / 'config' / 'included.rc'}\n")
    monkeypatch.setenv('TASKRC', str(rc))
    _init(tdir, 'en-CA', 'ON')
    assert 'hd20261012a' in events._task_holiday_keys()
    assert events._worst(events.check(now=NOW)) == events.OK


# ── the sky: moon phases, solstices and equinoxes (computed) ──────────────────

def test_moon_and_seasons_match_pyephem():
    ref = json.loads((FIXTURES / 'sky-ephem-2026-2027.json').read_text())['events']
    ours = sorted((when.strftime('%Y-%m-%dT%H:%M:%SZ'), name)
                  for year in (2026, 2027) for when, name in events.moon_phases(year) + events.seasons(year))
    assert [n for _, n in ours] == [n for _, n in ref]
    parse = lambda s: dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ')
    worst = max(abs((parse(a) - parse(b)).total_seconds()) for (a, _), (b, _) in zip(ours, ref))
    assert worst < 120, f'{worst:.0f} s off'


def test_sky_off_by_default_but_file_written(tmp_path, tdir):
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-01-01', 'New Year')]})
    checks = events.refresh(src, now=NOW)
    _, sky = events.read_data(tdir / 'config' / 'sky.rc')
    assert sky == [] and any('moon and seasons off' in c['detail'] for c in checks)


def test_sky_entries(tmp_path, tdir, tz):
    tz('America/Toronto')
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\nevents.moon = yes\nevents.seasons = yes\n')
    events.refresh(source(tmp_path, {2026: []}), now=NOW)
    text = (tdir / 'config' / 'sky.rc').read_text()
    assert '# events-sky: moon=yes seasons=yes years=2026' in text
    # The full moon of 2026-10-26 04:11 UTC is the 26th at 00:11 in Toronto
    assert 'holiday.mn20261026a.name=Full Moon\nholiday.mn20261026a.date=20261026\n' \
           'holiday.mn20261026a.kind=moon\nholiday.mn20261026a.at=20261026T0411' in text
    assert 'holiday.ss20261221a.name=December Solstice\nholiday.ss20261221a.date=20261221\n' in text
    _, sky = events.read_data(tdir / 'config' / 'sky.rc')
    assert sum(r['kind'] == 'moon' for r in sky) == 50 and sum(r['kind'] == 'season' for r in sky) == 4


def test_local_day_crosses_midnight(tmp_path, tdir, tz):
    # The new moon of 2026-10-10 15:49 UTC is 00:49 on the 11th in Tokyo: the entry's date is local
    tz('Asia/Tokyo')
    settings(tdir, 'events.locale = xx-XX\nevents.years = 1\nevents.moon = yes\n')
    events.refresh(source(tmp_path, {2026: []}), now=NOW)
    text = (tdir / 'config' / 'sky.rc').read_text()
    assert 'holiday.mn20261011a.name=New Moon' in text and 'at=20261010T1549' in text


def test_sky_written_when_the_fetch_fails(tmp_path, tdir):
    settings(tdir, 'events.locale = zz-ZZ\nevents.years = 1\nevents.moon = yes\n')
    checks = events.refresh(tmp_path / 'nothing', now=NOW)
    assert events._worst(checks) == events.FAIL
    assert len(events.read_data(tdir / 'config' / 'sky.rc')[1]) > 40


def test_check_sky(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\nevents.moon = yes\n')
    events.refresh(FIXTURES, now=NOW)
    ok = events.check(now=NOW, task_keys=lambda: {'hd20260101a', 'mn20261026a'})
    assert events._worst(ok) == events.OK, ok
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\nevents.moon = yes\nevents.seasons = yes\n')
    changed = events.check(now=NOW, task_keys=lambda: {'hd20260101a'})
    assert next(c for c in changed if c['check'] == 'sky')['status'] == events.WARN


def test_show_lists_the_sky_with_local_times(tdir, capsys, tz):
    tz('America/Toronto')
    settings(tdir, 'events.locale = en-CA\nevents.regions = ON\nevents.moon = yes\nevents.seasons = yes\n')
    events.refresh(FIXTURES, now=NOW)
    events.main(['show', '--year', '2026'])
    out = capsys.readouterr().out
    assert '2026-10-26 00:11' in out and 'Full Moon  (moon)' in out
    assert 'Thanksgiving Day  (ON V)' in out


# ── moving over from holidata 0.1 ─────────────────────────────────────────────

def test_init_moves_holidata_settings_and_include(tdir):
    (tdir / 'config' / 'holidata.rc').write_text(
        'holidata.locale  = en-CA\nholidata.regions = ON\ninclude holidays.rc\n')
    (tdir / 'config' / 'holidays.rc').write_text('# holidata: old\n')
    inc = tdir / 'config' / 'included.rc'
    inc.write_text(f"include ~/.task/config/annn.rc\ninclude {tdir / 'config' / 'holidata.rc'}\n")
    assert 'holidata 0.1' in events.check(now=NOW, task_keys=None)[0]['detail']
    checks = events.init(moon='yes', source=FIXTURES, now=NOW)
    assert events._worst(checks) == events.OK, checks
    s = events.read_settings()
    assert (s['locale'], s['regions'], s['moon']) == ('en-CA', 'ON', 'yes')
    assert not (tdir / 'config' / 'holidata.rc').exists()
    assert inc.read_text().splitlines() == ['include ~/.task/config/annn.rc',
                                            f"include {tdir / 'config' / 'events.rc'}"]
    rc = (tdir / 'config' / 'events.rc').read_text().splitlines()
    assert rc[-3:] == ['include holidays.rc', 'include sky.rc', 'include mine.rc']
    assert rc.index('events.moon    = yes') < rc.index('include holidays.rc')


@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_task_sees_the_sky(tmp_path, tdir, monkeypatch):
    rc = tmp_path / 'taskrc'
    (tmp_path / 'data').mkdir()
    rc.write_text(f"data.location={tmp_path / 'data'}\nhooks=off\nconfirmation=no\ncalendar.holidays=full\n"
                  f"include {tdir / 'config' / 'included.rc'}\n")
    monkeypatch.setenv('TASKRC', str(rc))
    _init(tdir, 'en-CA', 'ON', None, None, 'yes', 'yes')
    keys = events._task_holiday_keys()
    assert 'hd20261012a' in keys and any(k.startswith('mn') for k in keys) and any(k.startswith('ss') for k in keys)
    out = subprocess.run(['task', 'rc.color=off', 'calendar', '12', '2026'], capture_output=True, text=True).stdout
    assert 'Full Moon' in out and 'December Solstice' in out and 'Christmas Day' in out


# ── my events (config/my-events.txt → mine.rc) and ghosts (events add) ────────

def mine(tdir, body):
    (tdir / 'config' / 'my-events.txt').write_text(body)


def mine_rows(tdir):
    return events.read_data(tdir / 'config' / 'mine.rc')[1]


def test_my_events_lines_and_date_blocks(tdir, tz):
    tz('America/Toronto')
    settings(tdir, 'events.locale = en-CA\nevents.years = 1\n')
    mine(tdir, "# a comment\n; another\n* an org heading\n\n"
               "2026-10-29 14:30  Dentist\n"
               "2026-11-05\n"
               "  09:00  Board meeting\n"
               "    Library books due\n"
               "2026-12-24..2026-12-27  Cottage\n")
    checks = events.refresh(FIXTURES, now=NOW)
    assert events._worst(checks) != events.FAIL, checks
    rows = mine_rows(tdir)
    got = [(str(r['start']), str(r['end']), r['name'], r.get('at', '')) for r in rows]
    assert got == [('2026-10-29', '2026-10-29', 'Dentist', '20261029T183000Z'),
                   ('2026-11-05', '2026-11-05', 'Library books due', ''),
                   ('2026-11-05', '2026-11-05', 'Board meeting', '20261105T140000Z'),
                   ('2026-12-24', '2026-12-27', 'Cottage', '')]
    assert {r['kind'] for r in rows} == {'mine'}
    assert [r['key'] for r in rows] == ['me20261029a', 'me20261105a', 'me20261105b', 'me20261224a']


def test_yearly_events_repeat_over_the_years(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.years = 2\n')
    mine(tdir, "--04-12  Anne's birthday\n--02-29  Leap day\n--12-31..--01-01  Turn of the year\n")
    events.refresh(FIXTURES, now=NOW)
    got = [(str(r['start']), str(r['end']), r['name']) for r in mine_rows(tdir)]
    assert got == [('2026-02-28', '2026-02-28', 'Leap day'),
                   ('2026-04-12', '2026-04-12', "Anne's birthday"),
                   ('2026-12-31', '2027-01-01', 'Turn of the year'),
                   ('2027-02-28', '2027-02-28', 'Leap day'),
                   ('2027-04-12', '2027-04-12', "Anne's birthday"),
                   ('2027-12-31', '2028-01-01', 'Turn of the year')]


def test_bad_lines_are_reported_and_skipped(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.years = 1\n')
    mine(tdir, "  09:00  Orphan, no date yet\n2026-02-30  No such day\n2026-10-29 25:00  Bad time\n"
               "2026-12-24..2026-12-26 10:00  Timed range\n10/29  Not a date\n2026-10-30  Fine #2\n")
    checks = events.refresh(FIXTURES, now=NOW)
    bad = [c for c in checks if c['check'].startswith('my-events.txt:')]
    assert [c['check'] for c in bad] == [f'my-events.txt:{n}' for n in (1, 2, 3, 4, 5)]
    assert all(c['status'] == events.WARN for c in bad)
    assert [(r['name']) for r in mine_rows(tdir)] == ['Fine ＃2']
    assert [c['check'] for c in events.check(now=NOW, task_keys=None)
            if c['check'].startswith('my-events.txt:')] == [c['check'] for c in bad]


def test_no_my_events_file_writes_an_empty_mine_rc(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.years = 1\n')
    events.refresh(FIXTURES, now=NOW)
    assert (tdir / 'config' / 'mine.rc').is_file() and mine_rows(tdir) == []


def test_refresh_adds_the_mine_include_once_the_file_exists(tdir):
    settings(tdir, 'events.locale = en-CA\ninclude holidays.rc\ninclude sky.rc\n')   # a 0.2 install
    events.refresh(FIXTURES, now=NOW)
    events.refresh(FIXTURES, now=NOW)
    assert (tdir / 'config' / 'events.rc').read_text().splitlines().count('include mine.rc') == 1


def test_check_warns_when_my_events_changed(tdir):
    settings(tdir, 'events.locale = en-CA\n')
    mine(tdir, '2026-10-29  Dentist\n')
    events.refresh(FIXTURES, now=NOW)
    assert all(c['check'] != 'my events' or c['status'] == events.OK
               for c in events.check(now=NOW, task_keys=None))
    mine(tdir, '2026-10-29  Dentist\n2026-10-30  Again\n')
    c = next(c for c in events.check(now=NOW, task_keys=None) if c['check'] == 'my events')
    assert c['status'] == events.WARN and 'events refresh' in c['detail']


def test_show_lists_my_events(tdir, capsys, tz):
    tz('America/Toronto')
    settings(tdir, 'events.locale = en-CA\nevents.years = 1\n')
    mine(tdir, '2026-10-29 14:30  Dentist\n2026-12-24..2026-12-27  Cottage\n')
    events.refresh(FIXTURES, now=NOW)
    events.main(['show', '--year', '2026'])
    out = capsys.readouterr().out
    assert '2026-10-29 14:30        Dentist  (mine)' in out
    assert '2026-12-24 – 2026-12-27 Cottage  (mine)' in out


def _task_env(tmp_path, tdir, monkeypatch):
    rc = tmp_path / 'taskrc'
    (tmp_path / 'data').mkdir()
    rc.write_text(f"data.location={tmp_path / 'data'}\nhooks=off\nconfirmation=no\n"
                  f"include {tdir / 'config' / 'events.rc'}\n")
    monkeypatch.setenv('TASKRC', str(rc))
    settings(tdir, 'events.locale = en-CA\nevents.years = 1\n')
    def task(*args):
        return subprocess.run(['task', *args], capture_output=True, text=True, timeout=15)
    return task


def _uuid(task, n):
    return task(str(n), '_uuid').stdout.strip()


@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_add_makes_a_ghost_that_outlives_its_task(tmp_path, tdir, monkeypatch, tz):
    tz('America/Toronto')
    task = _task_env(tmp_path, tdir, monkeypatch)
    task('add', 'due:2026-10-29T14:30', '--', 'Dentist #2')
    task('1', 'annotate', '--', 'called the clinic')
    task('add', 'scheduled:2026-11-02', '--', 'Pick up glasses')
    u1, u2 = _uuid(task, 1), _uuid(task, 2)
    checks = events.add(['1', u2], now=NOW)
    assert events._worst(checks) == events.OK, checks
    text = (tdir / 'config' / 'my-events.txt').read_text()
    assert f'# ghost of {u1}' in text and '# annotation ' in text and 'called the clinic' in text
    assert '2026-10-29 14:30  Dentist #2' in text
    assert '2026-11-02  Pick up glasses' in text
    task('1', 'done')
    task(u2, 'delete')
    assert {'me20261029a', 'me20261102a'} <= events._task_holiday_keys()
    rows = mine_rows(tdir)
    assert [(r['name'], r.get('at', '')) for r in rows] == [('Dentist ＃2', '20261029T183000Z'),
                                                            ('Pick up glasses', '')]


@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_add_refuses_twice_no_date_and_templates(tmp_path, tdir, monkeypatch):
    task = _task_env(tmp_path, tdir, monkeypatch)
    task('add', 'due:2026-10-29', '--', 'Once')
    task('add', '--', 'No date')
    task('add', 'due:2026-10-29', 'recur:weekly', '--', 'Weekly')
    assert events._worst(events.add(['1'], now=NOW)) == events.OK
    checks = events.add(['1', '2', '3'], now=NOW)
    assert [c['status'] for c in checks if c['check'].startswith('task ')] == [events.WARN] * 3, checks
    assert 'already' in checks[0]['detail'] and 'due or scheduled' in checks[1]['detail']
    assert (tdir / 'config' / 'my-events.txt').read_text().count('# ghost of') == 1


def test_add_refuses_anything_but_ids_and_uuids(tdir, monkeypatch):
    monkeypatch.setattr(events.subprocess, 'run', lambda *a, **k: pytest.fail('ran task'))
    checks = events.add(['rc.data.location=/tmp', 'project:x'], now=NOW)
    assert [c['status'] for c in checks] == [events.FAIL, events.FAIL]


# ── two stations, one repo (gittw): outputs must be byte-identical for the same input ──

def test_outputs_do_not_depend_on_when_they_were_written(tdir):
    settings(tdir, 'events.locale = en-CA\nevents.moon = yes\n')
    mine(tdir, '--04-12  Birthday\n')
    files = [tdir / 'config' / f for f in ('holidays.rc', 'sky.rc', 'mine.rc')]
    events.refresh(FIXTURES, now=NOW)
    first = [f.read_bytes() for f in files]
    events.refresh(FIXTURES, now=NOW + dt.timedelta(hours=5))
    assert [f.read_bytes() for f in files] == first


def test_refresh_no_fetch_keeps_holidays_and_rebuilds_the_rest(tdir, monkeypatch):
    settings(tdir, 'events.locale = en-CA\n')
    events.refresh(FIXTURES, now=NOW)
    before = (tdir / 'config' / 'holidays.rc').read_bytes()
    mine(tdir, '2026-10-29  Dentist\n')
    monkeypatch.setattr(events, 'fetch', lambda *a, **k: pytest.fail('fetched'))
    checks = events.refresh(now=NOW, fetch_holidays=False)
    assert events._worst(checks) == events.OK, checks
    assert (tdir / 'config' / 'holidays.rc').read_bytes() == before
    assert [r['name'] for r in mine_rows(tdir)] == ['Dentist']
    assert events.main(['refresh', '--no-fetch']) == 0
