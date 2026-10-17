- Project: https://github.com/linuxcaffe/tw-holidata
- Issues:  https://github.com/linuxcaffe/tw-holidata/issues

# tw-holidata

Public holidays from [holidata.net](https://holidata.net) as Taskwarrior 2.6.2 `holiday.*` entries, so `task calendar`, [tw-web](https://github.com/linuxcaffe/tw-web) and any other client show the same days.

## TL;DR

- `holidata refresh` fetches this year and next for your locale, keeps the national holidays plus your regions, and writes `~/.task/config/holidays.rc`
- `holidata` (or `task holidays`) lists what's installed; `holidata check` warns before they run out
- `holidata init --locale en-CA --regions ON` sets it up; settings live in `~/.task/config/holidata.rc`
- Runs of consecutive days (a holiday week) become one `.start`/`.end` range, which 2.6.2 understands
- A failed fetch keeps the old file; holiday names can't break out of their rc line
- Respects `TW_TASK_DIR`. Python 3.8+, standard library only

## Why this exists

Taskwarrior ships `holidays.<locale>.rc` files generated from holidata.net, but they stop at 2022, and their format drops each holiday's region: the Canadian file lists Family Day, Louis Riel Day and Islander Day on the same Monday with no way to tell which province keeps which. holidata.net itself is current (it publishes about a year ahead) and says which region each holiday belongs to.

`holidata` fetches it, keeps only what applies to you, and writes the result where Taskwarrior reads its configuration. The CLI stays the one place holidays are defined; clients read them from `task _show`.

## Install

```bash
tw -I holidata
holidata init --locale en-CA --regions ON     # or: task holidays init …
```

`init` writes `~/.task/config/holidata.rc`, fetches, and only then adds the includes (`included.rc` → `holidata.rc` → `holidays.rc`), so Taskwarrior never sees an include whose file is missing. Run it again to change a setting; it keeps everything else in the file. You can also edit `holidata.rc` by hand and run `holidata refresh` (`task config` would write to `~/.taskrc`, where holidata doesn't look).

To see them in `task calendar` too:

```bash
task config calendar.holidays full     # or sparse
```

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

## Tests

```bash
python3 -m pytest -q tests/     # fixtures only; never touches the network or ~/.task
```

## License

MIT
