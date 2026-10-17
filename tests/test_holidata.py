"""holidata: fixtures are real holidata.net files (en-CA 2026, 2027). Tests never touch the
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
spec = importlib.util.spec_from_file_location('holidata', ROOT / 'holidata.py')
holidata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(holidata)

NOW = dt.datetime(2026, 10, 17, 9, 0)


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    monkeypatch.setenv('TW_TASK_DIR', str(tmp_path / 'task'))
    (tmp_path / 'task' / 'config').mkdir(parents=True)
    monkeypatch.setattr(holidata.urllib.request, 'urlopen',
                        lambda *a, **k: pytest.fail('a test reached the network'))
    return tmp_path / 'task'


def settings(tdir, body):
    (tdir / 'config' / 'holidata.rc').write_text(body)


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
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON   # Ontario\n')
    checks = holidata.refresh(FIXTURES, now=NOW)
    assert holidata._worst(checks) == holidata.OK, checks
    _, rows = holidata.read_data()
    names_2026 = [r['name'] for r in rows if r['start'].year == 2026]
    assert names_2026 == ["New Year's Day", 'Family Day', 'Good Friday', 'Victoria Day', 'Canada Day',
                          'Labour Day', 'Thanksgiving Day', 'Christmas Day', 'Boxing Day']
    assert {r.get('region', '') for r in rows} == {'', 'ON'}
    assert {r['start'].year for r in rows} == {2026, 2027}


def test_types_filter(tdir):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\nholidata.types = N\n')
    holidata.refresh(FIXTURES, now=NOW)
    _, rows = holidata.read_data()
    assert rows and all('N' in r['type'] for r in rows)


def test_written_file_is_taskwarrior_holiday_entries(tdir):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\n')
    holidata.refresh(FIXTURES, now=NOW)
    text = (tdir / 'config' / 'holidays.rc').read_text()
    assert '# holidata: locale=en-CA regions=ON types= years=2026,2027' in text
    assert "holiday.hd20260101a.name=New Year's Day\nholiday.hd20260101a.date=20260101\n" in text
    assert 'holiday.hd20261012a.region=ON' in text
    assert 'holiday.hd20260101a.type=NF' in text


def test_same_day_gets_distinct_keys(tmp_path, tdir):
    settings(tdir, 'holidata.locale = xx-XX\nholidata.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-03-01', 'One'), rec('2026-03-01', 'Two')]})
    holidata.refresh(src, now=NOW)
    _, rows = holidata.read_data()
    assert [r['key'] for r in rows] == ['hd20260301a', 'hd20260301b']


def test_consecutive_days_become_a_range(tmp_path, tdir):
    settings(tdir, 'holidata.locale = xx-XX\nholidata.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-12-24', 'Break'), rec('2026-12-25', 'Break'),
                                   rec('2026-12-26', 'Break'), rec('2026-12-28', 'Break')]})
    holidata.refresh(src, now=NOW)
    text = (tdir / 'config' / 'holidays.rc').read_text()
    assert 'holiday.hd20261224a.start=20261224\nholiday.hd20261224a.end=20261226\n' in text
    assert 'holiday.hd20261228a.date=20261228' in text


def test_names_cannot_break_out_of_their_line(tmp_path, tdir):
    settings(tdir, 'holidata.locale = xx-XX\nholidata.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-05-01', 'Day\ninclude /etc/passwd'),
                                   rec('2026-05-02', 'Day #2')]})
    holidata.refresh(src, now=NOW)
    lines = (tdir / 'config' / 'holidays.rc').read_text().splitlines()
    assert not any(l.startswith('include') for l in lines)
    assert 'holiday.hd20260501a.name=Day include /etc/passwd' in lines
    assert 'holiday.hd20260502a.name=Day ＃2' in lines


def test_next_year_not_published_is_fine(tmp_path, tdir):
    settings(tdir, 'holidata.locale = xx-XX\n')
    src = source(tmp_path, {2026: [rec('2026-01-01', 'New Year')]})
    checks = holidata.refresh(src, now=NOW)
    assert holidata._worst(checks) == holidata.OK
    assert any('not published yet' in c['detail'] for c in checks)


def test_unknown_locale_fails_and_keeps_the_old_file(tmp_path, tdir):
    old = tdir / 'config' / 'holidays.rc'
    old.write_text('# old\n')
    settings(tdir, 'holidata.locale = zz-ZZ\n')
    checks = holidata.refresh(tmp_path / 'empty', now=NOW)
    assert holidata._worst(checks) == holidata.FAIL
    assert old.read_text() == '# old\n'


def test_fetch_error_keeps_the_old_file(tdir, monkeypatch):
    old = tdir / 'config' / 'holidays.rc'
    old.write_text('# old\n')
    settings(tdir, 'holidata.locale = en-CA\n')

    def boom(*a, **k):
        raise holidata.urllib.error.URLError('offline')
    monkeypatch.setattr(holidata.urllib.request, 'urlopen', boom)
    assert holidata.main(['refresh']) == 2
    assert old.read_text() == '# old\n'


def test_two_locales(tmp_path, tdir):
    settings(tdir, 'holidata.locale = xx-XX, yy-YY\nholidata.years = 1\n')
    source(tmp_path, {2026: [rec('2026-07-01', 'Fête')]}, 'xx-XX')
    src = source(tmp_path, {2026: [rec('2026-07-01', 'Feast', locale='yy-YY')]}, 'yy-YY')
    holidata.refresh(src, now=NOW)
    _, rows = holidata.read_data()
    assert [(r['name'], r['locale']) for r in rows] == [('Feast', 'yy-YY'), ('Fête', 'xx-XX')]


# ── show and check ────────────────────────────────────────────────────────────

def test_show_year_json(tdir, capsys):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\n')
    holidata.refresh(FIXTURES, now=NOW)
    assert holidata.main(['show', '--year', '2027', '--json']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]['start'] == '2027-01-01' and all(r['start'].startswith('2027') for r in out)


def test_check_ok_after_refresh(tdir):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\n')
    holidata.refresh(FIXTURES, now=NOW)
    checks = holidata.check(now=NOW, task_keys=lambda: {'hd20260101a'})
    assert holidata._worst(checks) == holidata.OK, checks


def test_check_warns_when_holidays_run_out(tdir):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\n')
    holidata.refresh(FIXTURES, now=NOW)
    late = dt.datetime(2027, 11, 15)
    checks = holidata.check(now=late, task_keys=lambda: {'hd20260101a'})
    cov = next(c for c in checks if c['check'] == 'coverage')
    assert cov['status'] == holidata.WARN and '2027-12-26' in cov['detail']
    # A new year alone doesn't count as changed settings.
    assert next(c for c in checks if c['check'] == 'settings')['status'] == holidata.OK


def test_check_warns_on_changed_settings(tdir):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\n')
    holidata.refresh(FIXTURES, now=NOW)
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = QC\n')
    checks = holidata.check(now=NOW, task_keys=lambda: {'hd20260101a'})
    assert next(c for c in checks if c['check'] == 'settings')['status'] == holidata.WARN


def test_check_warns_when_task_does_not_see_them(tdir):
    settings(tdir, 'holidata.locale = en-CA\nholidata.regions = ON\n')
    holidata.refresh(FIXTURES, now=NOW)
    checks = holidata.check(now=NOW, task_keys=lambda: set())
    c = next(c for c in checks if c['check'] == 'task sees them')
    assert c['status'] == holidata.WARN and 'include' in c['detail']


def test_check_before_init_says_run_init(tdir):
    # Installed (this is holidata running) but never set up: tw -I alone writes nothing.
    checks = holidata.check(now=NOW, task_keys=None)
    assert holidata._worst(checks) == holidata.WARN
    assert 'holidata init --locale' in checks[0]['detail'] and 'tw -I' not in checks[0]['detail']


def test_show_before_init_says_run_init(tdir, capsys):
    assert holidata.main(['show']) == 1
    assert 'holidata init' in capsys.readouterr().out


# ── the real binary ───────────────────────────────────────────────────────────

@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_task_reads_the_file(tmp_path, tdir, monkeypatch):
    settings(tdir, 'holidata.locale = xx-XX\nholidata.years = 1\n')
    src = source(tmp_path, {2026: [rec('2026-10-12', 'Thanks'), rec('2026-12-24', 'Break'),
                                   rec('2026-12-25', 'Break')]})
    holidata.refresh(src, now=NOW)
    rc = tmp_path / 'taskrc'
    (tmp_path / 'data').mkdir()
    rc.write_text(f"data.location={tmp_path / 'data'}\nhooks=off\nconfirmation=no\n"
                  f"calendar.holidays=full\ninclude {tdir / 'config' / 'holidays.rc'}\n")
    monkeypatch.setenv('TASKRC', str(rc))
    out = subprocess.run(['task', 'rc.color=off', 'calendar', '12', '2026'],
                         capture_output=True, text=True, timeout=15).stdout
    assert 'Start of Break' in out and 'End of Break' in out
    keys = holidata._task_holiday_keys()
    assert keys == {'hd20261012a', 'hd20261224a'}


# ── init ──────────────────────────────────────────────────────────────────────

def _init(tdir, *args):
    (tdir / 'config' / 'included.rc').touch()
    return holidata.init(*args, source=FIXTURES, now=NOW)


def test_init_writes_settings_data_and_includes(tdir):
    checks = _init(tdir, 'en-CA', 'ON')
    assert holidata._worst(checks) == holidata.OK, checks
    s = holidata.read_settings()
    assert (s['locale'], s['regions']) == ('en-CA', 'ON')
    assert (tdir / 'config' / 'holidays.rc').is_file()
    assert 'include holidays.rc' in (tdir / 'config' / 'holidata.rc').read_text().splitlines()
    assert f"include {tdir / 'config' / 'holidata.rc'}" in (tdir / 'config' / 'included.rc').read_text()


def test_init_twice_changes_only_what_is_given(tdir):
    _init(tdir, 'en-CA', 'ON')
    sp = tdir / 'config' / 'holidata.rc'
    sp.write_text(sp.read_text() + '# my note\n')
    _init(tdir, None, 'QC')
    text = sp.read_text()
    assert 'holidata.locale  = en-CA' in text and 'holidata.regions = QC' in text and '# my note' in text
    assert text.count('include holidays.rc') == 1
    assert (tdir / 'config' / 'included.rc').read_text().count('holidata.rc') == 1


def test_init_reenables_a_commented_include(tdir):
    (tdir / 'config' / 'included.rc').write_text(
        f"#include {tdir / 'config' / 'holidata.rc'}   # removed with tw -r\n")
    holidata.init('en-CA', 'ON', source=FIXTURES, now=NOW)
    lines = (tdir / 'config' / 'included.rc').read_text().splitlines()
    assert lines == [f"include {tdir / 'config' / 'holidata.rc'}"]


def test_init_offline_still_leaves_no_dangling_include(tmp_path, tdir):
    (tdir / 'config' / 'included.rc').touch()
    checks = holidata.init('en-CA', 'ON', source=tmp_path / 'nothing', now=NOW)
    assert holidata._worst(checks) == holidata.FAIL
    assert (tdir / 'config' / 'holidays.rc').is_file()
    assert 'include holidays.rc' in (tdir / 'config' / 'holidata.rc').read_text()


@pytest.mark.skipif(not shutil.which('task'), reason='task not installed')
def test_task_sees_holidays_after_init(tmp_path, tdir, monkeypatch):
    rc = tmp_path / 'taskrc'
    (tmp_path / 'data').mkdir()
    rc.write_text(f"data.location={tmp_path / 'data'}\nhooks=off\nconfirmation=no\n"
                  f"include {tdir / 'config' / 'included.rc'}\n")
    monkeypatch.setenv('TASKRC', str(rc))
    _init(tdir, 'en-CA', 'ON')
    assert 'hd20261012a' in holidata._task_holiday_keys()
    assert holidata._worst(holidata.check(now=NOW)) == holidata.OK
