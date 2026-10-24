- Project: https://github.com/linuxcaffe/tw-events
- Issues:  https://github.com/linuxcaffe/tw-events/issues

# tw-events

The calendar's non-task entries as Taskwarrior 2.6.2 `holiday.*` entries, so `task calendar`, [tw-web](https://github.com/linuxcaffe/tw-web) and any other client show the same days: public holidays from [holidata.net](https://holidata.net); the moon's phases, the solstices and the equinoxes, computed on your machine; and your own events, including ghosts of tasks you've finished.

## TL;DR

- `tw -I events`, then `events init --locale en-CA --regions ON --moon yes --seasons yes`: two steps, and the second one is what fetches
- `events refresh` fetches this year's and next year's holidays for your locale (national ones plus your regions) into `~/.task/config/holidays.rc`, and computes the moon and seasons into `~/.task/config/sky.rc`
- `events` (or `task events`) lists what's installed; `events check` warns before it runs out
- Your own events (birthdays, appointments, a week away) go in `~/.task/config/my-events.txt`, one per line, timedot style; `--04-12` means every year
- `events add 42` makes a **ghost** of task 42: its description at its due (or scheduled) date and time, kept after you complete or delete the task
- Moon phases and seasons need no network: they're computed with Meeus's *Astronomical Algorithms*, within a minute of PyEphem
- Runs of consecutive days (a holiday week) become one `.start`/`.end` range, which 2.6.2 understands
- A failed fetch keeps the old holidays; names can't break out of their rc line
- Respects `TW_TASK_DIR`. Python 3.8+, standard library only

## Why this exists

Taskwarrior ships `holidays.<locale>.rc` files generated from holidata.net, but they stop at 2022, and their format drops each holiday's region: the Canadian file lists Family Day, Louis Riel Day and Islander Day on the same Monday with no way to tell which province keeps which. holidata.net itself is current (it publishes about a year ahead) and says which region each holiday belongs to.

`events` fetches it, keeps only what applies to you, and writes the result where Taskwarrior reads its configuration. The CLI stays the one place these days are defined; clients read them from `task _show`. A holiday entry is just a named date, so the full moon and the solstice fit in the same place, with two extra keys for what they are and the exact moment.

## Install

Two steps. **`tw -I` installs the command only; nothing is fetched until you run `init`.**

```bash
tw -I events                                   # 1. the command (and `task events`)
events init --locale en-CA --regions ON \
            --moon yes --seasons yes           # 2. your settings, the entries, the includes
```

`--locale` is a holidata.net locale (`en-US`, `en-GB`, `de-DE`, `fr-CA`, …; the site lists them all). `--regions` takes the region codes holidata uses, the second half of ISO 3166-2 (`ON`, `QC`, `NY`, `BY`); leave it out for national holidays only. `--moon` and `--seasons` are off unless you say yes. Then:

```console
$ events show --year 2026 | tail -8
2026-12-01 01:08        Last Quarter  (moon)
2026-12-08 19:51        New Moon  (moon)
2026-12-17 00:42        First Quarter  (moon)
2026-12-21 15:50        December Solstice  (season)
2026-12-23 20:28        Full Moon  (moon)
2026-12-25              Christmas Day  (NRF)
2026-12-26              Boxing Day  (NRF)
2026-12-30 13:59        Last Quarter  (moon)
```

Moments are shown in your local time; each entry's date is the local day it falls on.

`init` writes `~/.task/config/events.rc`, refreshes, and only then adds the includes (`included.rc` → `events.rc` → `holidays.rc` and `sky.rc`), so Taskwarrior never sees an include whose file is missing. Run it again to change a setting; it keeps everything else in the file. You can also edit `events.rc` by hand and run `events refresh` (`task config` would write to `~/.taskrc`, where events doesn't look).

To see them in `task calendar` too:

```bash
task config calendar.holidays full     # or sparse
```

`task calendar` marks every entry's day in the holiday colour and, with `full`, lists the names, full moons and solstices included.

In [tw-web](https://github.com/linuxcaffe/tw-web) holidays show as amber all-day bars in the Calendar and on the Agenda's date lines; the moon's phases and the seasons as a mark at their moment in Week and Day views and an icon in Month (Settings → Calendar).

`tw -r events` removes the command and the README but leaves `events.rc`, its data files and the include, so Taskwarrior still starts and the entries stay. To drop them, delete the `include ~/.task/config/events.rc` line from `config/included.rc`.

**Coming from tw-holidata 0.1:** `tw -r holidata`, `tw -I events`, then `events init` (add `--moon yes --seasons yes` if you like). It moves your `holidata.*` settings into `events.rc`, swaps the include in `included.rc`, and removes `holidata.rc`.

## Settings

`~/.task/config/events.rc`:

| Setting | Default | Meaning |
|---|---|---|
| `events.locale` | `en-US` | holidata.net locale; a comma list for several (`en-CA, fr-CA`) |
| `events.regions` | (none) | Region codes to keep besides national holidays (`ON`, `NY,NJ`). Applies to every locale |
| `events.types` | (all) | Keep only holidays whose type has one of these letters: `N` national, `R` religious, `F` fixed date, `V` variable date |
| `events.years` | `2` | This year and the following years (holidays: those holidata has published) |
| `events.moon` | `no` | The moon's four principal phases: new, first quarter, full, last quarter (about 50 a year) |
| `events.seasons` | `no` | The March and September equinoxes, the June and December solstices |

Run `events refresh` after changing them; `events check` says when you haven't.

## Your own events

`~/.task/config/my-events.txt` is yours: `init` writes it with examples, you edit it, and `events add` only ever appends to it. Each line is a date, an optional time and a name, or a date on its own line with that day's events indented under it (as in an hledger timedot file):

```text
# comments start with #, ; or *
2026-10-29 14:30  Dentist
--04-12           Anne's birthday
2026-12-24..2026-12-27  Cottage
2026-11-05
  09:00  Board meeting
  Library books due
```

- `YYYY-MM-DD` is one day; `--MM-DD` repeats every year over `events.years` (29 February falls on the 28th in common years); `A..B` is a run of days, which takes no time.
- A time is local (`HH:MM`); the entry carries the moment in UTC as `.at`, like the moon's phases.
- `events refresh` (and `events add`) turn the file into `config/mine.rc`, which Taskwarrior includes. A line it can't read is skipped and named (`my-events.txt:12 — no such date`), by `refresh` and by `check`.

### Ghosts: `events add`

```console
$ events add 42
  ok   task 42 — 2026-10-29 14:30  Dentist
$ task 42 done
```

`events add <id|uuid>…` reads each task (read-only, hooks off) and appends its ghost: the description at its due date, else its scheduled date, with the time unless it's midnight or 23:59:59. The task's UUID and annotations go above it as comments:

```text
# ghost of 0a90a7f8-0e6f-4013-bd29-bfbf56ff4dd8  (events add, 2026-10-23)
# annotation 2026-10-23: moved to Thursday
2026-10-29 14:30  Dentist
```

Then complete or delete the task as usual; the ghost stays on the calendar. A task already ghosted, a recurring template and a task with neither date are refused. `events add` takes task IDs and UUIDs only, never a filter. It's an ordinary line afterwards: edit or delete it like any other.

## Commands

| Command | Does |
|---|---|
| `events init [--locale L] [--regions R] [--types T] [--years N] [--moon yes\|no] [--seasons yes\|no] [--json]` | Write the given settings (keeping the rest), refresh, then add the includes. Offline, it writes an empty holidays file so the include is still safe. Moves tw-holidata 0.1's settings over |
| `events refresh [--source DIR] [--json]` | Fetch and rewrite `config/holidays.rc`, compute `config/sky.rc`, and rebuild `config/mine.rc` from `my-events.txt`. `--source` reads `DIR/<locale>/<year>.json` instead (offline, tests). A year that isn't published yet is skipped; any other failure leaves the holidays untouched (exit 2), but the sky is still written |
| `events [show] [--year Y] [--json]` | List what's installed |
| `events add <id\|uuid>… [--json]` | A ghost of each task in `my-events.txt`, then rebuild `mine.rc` (no network) |
| `events check [--days 60] [--json]` | Read-only. Warns when the entries end within `--days`, when the settings or `my-events.txt` changed since the last refresh, on lines in `my-events.txt` it can't read, or when `task` doesn't see them (no include). [taskutil](https://github.com/linuxcaffe/tw-taskutil) runs it daily |

Exit codes: `0` ok, `1` warnings, `2` failures.

## The data files

`holidays.rc`:

```ini
# events: locale=en-CA regions=ON types= years=2026,2027
holiday.hd20260101a.name=New Year's Day
holiday.hd20260101a.date=20260101
holiday.hd20260101a.type=NF
holiday.hd20261012a.name=Thanksgiving Day
holiday.hd20261012a.date=20261012
holiday.hd20261012a.type=V
holiday.hd20261012a.region=ON
```

`sky.rc`:

```ini
# events-sky: moon=yes seasons=yes years=2026,2027
holiday.mn20261026a.name=Full Moon
holiday.mn20261026a.date=20261026
holiday.mn20261026a.kind=moon
holiday.mn20261026a.at=20261026T041142Z
holiday.ss20261221a.name=December Solstice
holiday.ss20261221a.date=20261221
holiday.ss20261221a.kind=season
holiday.ss20261221a.at=20261221T205006Z
```

`mine.rc` is the same shape, `.kind=mine`, with `.at` for a timed event and `.start`/`.end` for a run of days.

Keys are `hd` (holidays), `mn` (moon), `ss` (seasons) or `me` (yours), then the date and `a, b, …`. `.name`, `.date` (or `.start`/`.end`) are Taskwarrior's own; `.type`, `.region`, `.locale`, `.kind` and `.at` (the moment, UTC) are extra keys Taskwarrior ignores and clients may use. A `#` in a name becomes `＃`, because 2.6.2 reads the rest of the line as a comment. Don't edit the files: `refresh` rewrites them whole.

## Changes

- **0.3.0** — Your own events in `config/my-events.txt` (timedot style; `--MM-DD` every year; runs of days), built into `mine.rc`. `events add <id>`: a ghost of a task that stays after the task is done. `refresh` adds the `mine.rc` include to an existing 0.2 install once the file is written.
- **0.2.0** — Renamed from tw-holidata: the command is `events` (`task events`), settings `events.*` in `events.rc`; `init` moves 0.1's settings over. New: the moon's phases (`events.moon`) and the solstices and equinoxes (`events.seasons`), computed (Meeus), in `sky.rc`.
- **0.1.1** — `check` and `show` say to run `init` when it hasn't been run. README: the two install steps, locale and region codes, sample output.
- **0.1.0** — `init`, `refresh`, `show`, `check`.

## Tests

```bash
python3 -m pytest -q tests/     # fixtures only; never touches the network or ~/.task
```

The moon and season times are checked against a PyEphem reference (`tests/fixtures/sky-ephem-2026-2027.json`), within two minutes.

## License

MIT
