# cli-anything-wiztree

Agent-native CLI for **WizTree** — the Windows NTFS disk-space analyzer by Antibody Software.

WizTree is a GUI program, but it ships a documented command-line export interface.
This harness drives that interface and adds the layer the GUI never had: a parsed,
queryable index you can ask questions of without re-scanning.

```
scan "C:"  ->  CSV export  ->  parsed index  ->  summary / top / find / dups / types / reports
```

## Why this exists

An agent cannot read a treemap. It *can* read JSON, so the job is to turn
"where did my disk space go" into a deterministic command that returns structured
data — and to do it while surviving three behaviours of the underlying binary that
will silently break a naive wrapper (all three measured, all three pinned by tests):

| Behaviour | Consequence |
|---|---|
| **Exit code is always `0`** — even for a target that does not exist | Never trust the exit status; a non-empty artifact is the only success signal |
| **CSV headers are localized** (`文件名称,大小,分配,…` on a Chinese system) | The parser reads columns positionally, never by name |
| **The column set varies** unless advanced switches are pinned | Every export switch is emitted explicitly so a scan's schema is reproducible |

## Requirements

- Windows (WizTree is Windows-only; the macOS build needs its own harness)
- Python 3.10+
- WizTree 3.18 or newer installed — <https://diskanalyzer.com/download>

## Installation

```bash
pip install .
cli-anything-wiztree --help
```

Installing to PATH is what makes the harness discoverable by agents. Verify:

```bash
cli-anything-wiztree info --json
```

## Configuration

Nothing is required for a normal install — WizTree is found automatically.
Discovery tries these routes in order, and `info` reports which one won (`source`):

1. `--exe <path>`
2. `$WIZTREE_EXE`
3. the remembered path in `<state home>/config.json`
4. **the Windows uninstall registry** (`InstallLocation` / `DisplayIcon` of the
   WizTree entry) — which is why no machine-specific path is baked into the source
5. `PATH`
6. `%ProgramFiles%`-style locations derived from the environment
7. user-configured `search_paths`

```bash
# portable build
set WIZTREE_EXE=D:\software\WizTree\WizTree64.exe      # cmd
$env:WIZTREE_EXE = "D:\software\WizTree\WizTree64.exe" # PowerShell

# or per invocation
cli-anything-wiztree --exe "D:\software\WizTree\WizTree64.exe" info
```

To teach it extra locations, edit `<state home>/config.json`:

```json
{ "search_paths": ["E:\\tools\\WizTree\\WizTree64.exe"] }
```

| Variable | Purpose |
|---|---|
| `WIZTREE_EXE` | Path to `WizTree64.exe`; overrides discovery |
| `CLI_ANYTHING_WIZTREE_HOME` | State directory (default `~/.cli-anything-wiztree`) |

Discovery never writes configuration — only `info` persists the path it found,
after showing it to you. A remembered path that has gone missing is skipped, so
moving WizTree does not break the CLI.

## Command reference

### `info` — what can this machine do?

```bash
cli-anything-wiztree info --json
# { "found": true, "exe": "...WizTree64.exe", "version": "4.27.0.0", "is_admin": false, ... }
```

`is_admin: false` means WizTree will fall back to a slower directory walk instead
of reading the MFT directly. Add `--admin` (which triggers a UAC prompt) for the
fast path.

### `scan run` — scan and index

```bash
cli-anything-wiztree scan run "C:"                          # whole drive
cli-anything-wiztree scan run "D:/projects" --json          # one folder
cli-anything-wiztree scan run "C:" --files-only -o files.csv
cli-anything-wiztree scan run "C:/Users" --filter "*.mp3|*.wav" --exclude "*\temp\*"
cli-anything-wiztree scan run "C:" --sort-by size --max-depth 3
```

| Option | Meaning |
|---|---|
| `--filter` / `--exclude` | Include / exclude filters, `\|` separates alternatives |
| `--name-only` | Match filters against the file name, not the whole path |
| `--files-only` / `--folders-only` | Drop folder or file rows |
| `--sort-by name\|size\|allocated\|modified` | Export ordering |
| `--max-depth N` | Limit exported folder depth (0 = unlimited) |
| `--mft-recno`, `--utc`, `--all-dates`, `--split-name` | WizTree's advanced columns |
| `--drive-capacity` | Add drive capacity columns (off by default to keep the schema stable) |
| `--allow-empty` | Accept an export with zero entries (see below) |
| `--timeout N` | Seconds before the scan is abandoned (default 1800) |

The result is written to `<home>/scans/<target>_<timestamp>.csv` unless `-o` is
given, and recorded in the active project so `query` can read it back.

**An empty result is an error, not a success.** When a filter matches nothing,
WizTree writes a banner and a header with no data rows — byte-for-byte what a
silent WizTree failure looks like. `scan run` therefore exits non-zero and keeps
the CSV for inspection instead of printing `✓ Scanned` over a scan that produced
nothing. Pass `--allow-empty` when an empty result is genuinely expected.

**Wildcards are data, never paths.** Pattern arguments are passed through
untouched: `query find "*.mp4"` searches the index, it does not expand into the
`.mp4` files of your working directory.

**The output directory is checked before WizTree starts.** An unwritable `-o`
path makes WizTree open a modal error dialog that nobody can click, and the
process then never returns; the harness refuses the destination up front rather
than hanging until `--timeout`.

**Rows can be dropped, and you are told.** A row whose byte-size field is empty
or non-numeric cannot be indexed; the count is reported in `skipped_rows` and as
a warning, because dropping rows silently would understate every total.

### `query` — ask the index questions

```bash
cli-anything-wiztree query summary --json
cli-anything-wiztree query top -n 20 --files
cli-anything-wiztree query top -n 10 --folders --by allocated
cli-anything-wiztree query find "*.log" --min-size 100m
cli-anything-wiztree query find "*" --ext mp4 --limit 50
cli-anything-wiztree query dups --min-size 1m
cli-anything-wiztree query types -n 15
```

`query dups` matches on **name + size**, which is exactly WizTree's own duplicate
rule — it never compares contents. The output says so, and so does this README:
verify before deleting.

### `export` — reports for humans and agents

```bash
cli-anything-wiztree export report -o report.md --format markdown --top 50
cli-anything-wiztree export report -o scan.json --format json
cli-anything-wiztree export report -o big.csv --preset large-files
cli-anything-wiztree export presets
```

Formats: `csv`, `json`, `table`, `markdown`, `html`. The format defaults to the
file extension. Presets: `default`, `large-files`, `media`, `cleanup`, `folders-only`.

### Other WizTree export modes

```bash
cli-anything-wiztree scan filetypes "C:" -o types.csv -n 20   # per-extension totals
cli-anything-wiztree scan treemap "C:" -o c.png                 # defaults to 1920x1080
cli-anything-wiztree scan treemap "C:" -o small.png --width 1024 --height 768
cli-anything-wiztree scan mft "D:" -o d.mft                   # raw NTFS $MFT, no elevation needed
```

### `project` and `session`

```bash
cli-anything-wiztree project new "C:" -o cleanup.json
cli-anything-wiztree --project cleanup.json scan run "C:"
cli-anything-wiztree --project cleanup.json query top -n 10
cli-anything-wiztree --project cleanup.json project profile-add media --filter "*.mp4" --files-only
cli-anything-wiztree --project cleanup.json project info

cli-anything-wiztree session history -n 10
cli-anything-wiztree session undo
```

### Interactive REPL

Run with no arguments:

```bash
cli-anything-wiztree
```

## JSON output

Every command accepts `--json` and then prints one JSON document — an object for
single results, an array for lists, and `{"error": ..., "type": ...}` plus a
non-zero exit code on failure.

The global options (`--json`, `--project`, `--exe`, `--admin/--no-admin`) work
**before or after the verb**, so all three of these are equivalent:

```powershell
cli-anything-wiztree --json scan run "C:"     # global before the verb
cli-anything-wiztree scan run "C:" --json     # global after the verb
cli-anything-wiztree --json scan run "C:" | ConvertFrom-Json |
  Select-Object -ExpandProperty stats
```

When both positions are used, the one closer to the command wins.

Usage errors (unknown option, missing argument, bad choice) go through the same
JSON channel as every other failure, so `--json` never yields empty stdout with a
human message on stderr.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — and for `scan`, the artifact was verified to contain at least one data row |
| `1` | A handled failure (WizTree missing, no output produced, empty export, bad project, bad size string) |
| `2` | A usage error (unknown option, missing argument) — reported as JSON too when `--json` is set |

WizTree's own exit status is meaningless (always `0`); this CLI's exit code is not.

## Testing

```bash
python -m pytest cli_anything/wiztree/tests/ -v
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/wiztree/tests/ -v
```

Unit tests use synthetic data. E2E tests invoke the real `WizTree64.exe` and
cross-check its numbers against an independent filesystem walk. See
[`tests/TEST.md`](tests/TEST.md) for the plan, the reconnaissance findings, and
the recorded results.

## License

Apache-2.0. WizTree itself is proprietary
software by Antibody Software Limited and is not distributed here.
