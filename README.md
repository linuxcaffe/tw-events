- Project: https://github.com/linuxcaffe/tw-holidata
- Issues:  https://github.com/linuxcaffe/tw-holidata/issues

# tw-holidata

Public holidays from [holidata.net](https://holidata.net) as Taskwarrior 2.6.2 `holiday.*` entries, so `task calendar`, [tw-web](https://github.com/linuxcaffe/tw-web) and any other client show the same days.

## TL;DR

- `tw -I holidata`, then `holidata init --locale en-CA --regions ON`: two steps, and the second one is what fetches
- `holidata refresh` fetches this year and next for your locale, keeps the national holidays plus your regions, and writes `~/.task/config/holidays.rc`
- `holidata` (or `task holidays`) lists what's installed; `holidata check` warns before they run out
- Settings live in `~/.task/config/holidata.rc`; `task calendar` and tw-web read the result
- Runs of consecutive days (a holiday week) become one `.start`/`.end` range, which 2.6.2 understands
- A failed fetch keeps the old file; holiday names can't break out of their rc line
- Respects `TW_TASK_DIR`. Python 3.8+, standard library only

## Why this exists

Taskwarrior ships `holidays.<locale>.rc` files generated from holidata.net, but they stop at 2022, and their format drops each holiday's region: the Canadian file lists Family Day, Louis Riel Day and Islander Day on the same Monday with no way to tell which province keeps which. holidata.net itself is current (it publishes about a year ahead) and says which region each holiday belongs to.

`holidata` fetches it, keeps only what applies to you, and writes the result where Taskwarrior reads its configuration. The CLI stays the one place holidays are defined; clients read them from `task _show`.

## Install

Two steps. **`tw -I` installs the command only; nothing is fetched until you run `init`.**

```bash
tw -I holidata                                 # 1. the command (and `task holidays`)
holidata init --locale en-CA --regions ON      # 2. your settings, the holidays, the includes
```

`--locale` is a holidata.net locale (`en-US`, `en-GB`, `de-DE`, `fr-CA`, …; the site lists them all). `--regions` takes the region codes holidata uses, the second half of ISO 3166-2 (`ON`, `QC`, `NY`, `BY`); leave it out for national holidays only. Then:

```console
$ holidata show --year 2026
2026-01-01              New Year's Day  (NF)
2026-02-16              Family Day  (ON V)
2026-04-03              Good Friday  (NRV)
2026-05-18              Victoria Day  (ON V)
2026-07-01              Canada Day  (NF)
2026-09-07              Labour Day  (NV)
2026-10-12              Thanksgiving Day  (ON V)
2026-12-25              Christmas Day  (NRF)
2026-12-26              Boxing Day  (NRF)
```

`init` writes `~/.task/config/holidata.rc`, fetches, and only then adds the includes (`included.rc` → `holidata.rc` → `holidays.rc`), so Taskwarrior never sees an include whose file is missing. Run it again to change a setting; it keeps everything else in the file. You can also edit `holidata.rc` by hand and run `holidata refresh` (`task config` would write to `~/.taskrc`, where holidata doesn't look).

To see them in `task calendar` too:

```bash
task config calendar.holidays full     # or sparse
```

In [tw-web](https://github.com/linuxcaffe/tw-web) they show as amber all-day bars in the Calendar and on the Agenda's date lines (Settings → Calendar → Holidays).

`tw -r holidata` removes the command and the README but leaves `holidata.rc`, `holidays.rc` and the include, so Taskwarrior still starts and the holidays stay. To drop them, delete the `include ~/.task/config/holidata.rc` line from `config/included.rc`.

## Settings

`~/.task/config/holidata.rc`:

| Setting | Default | Meaning |
|---|---|---|
| `holidata.locale` | `en-US` | holidata.net locale; a comma list for several (`en-CA, fr-CA`) |
| `holidata.regions` | (none) | Region codes to keep besides national holidays (`ON`, `NY,NJ`). Applies to every locale |
| `holidata.types` | (all) | Keep only holidays whose type has one of these letters: `N` national, `R` religious, `F` fixed date, `V` variable date |
| `holidata.years` | `2` | This year and the following years (those holidata has published) |

Run `holidata refresh` after changing them; `holidata check` says when you haven't.

## Commands

| Command | Does |
|---|---|
| `holidata init [--locale L] [--regions R] [--types T] [--years N] [--json]` | Write the given settings (keeping the rest), refresh, then add the includes. Offline, it writes an empty data file so the include is still safe |
| `holidata refresh [--source DIR] [--json]` | Fetch and rewrite `config/holidays.rc`. `--source` reads `DIR/<locale>/<year>.json` instead (offline, tests). A year that isn't published yet is skipped; any other failure leaves the file untouched (exit 2) |
| `holidata [show] [--year Y] [--json]` | List the installed holidays |
| `holidata check [--days 60] [--json]` | Read-only. Warns when the holidays end within `--days`, when the settings changed since the last refresh, or when `task` doesn't see the file (no include). [taskutil](https://github.com/linuxcaffe/tw-taskutil) runs it daily |

Exit codes: `0` ok, `1` warnings, `2` failures.

## The data file

```ini
# holidata: locale=en-CA regions=ON types= years=2026,2027
holiday.hd20260101a.name=New Year's Day
holiday.hd20260101a.date=20260101
holiday.hd20260101a.type=NF
holiday.hd20261012a.name=Thanksgiving Day
holiday.hd20261012a.date=20261012
holiday.hd20261012a.type=V
holiday.hd20261012a.region=ON
```

Keys are `hd<date><a,b,…>`. `.name`, `.date` (or `.start`/`.end`) are Taskwarrior's own; `.type`, `.region` and `.locale` are extra keys Taskwarrior ignores and clients may use. A `#` in a name becomes `＃`, because 2.6.2 reads the rest of the line as a comment. Don't edit the file: `refresh` rewrites it whole.

## Changes

- **0.1.1** — `holidata check` and `holidata show` say to run `holidata init` when it hasn't been run (they suggested `tw -I` or `refresh`, which don't set anything up). README: the two install steps, locale and region codes, sample output.
- **0.1.0** — `init`, `refresh`, `show`, `check`.

## Tests

```bash
python3 -m pytest -q tests/     # fixtures only; never touches the network or ~/.task
```

## License

MIT
