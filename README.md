# cli-anything-wiztree

**Agent-native CLI for [WizTree](https://diskanalyzer.com) — scan Windows disks and query the results like a database.**

WizTree is a fast (MFT-reading) Windows disk-space analyzer. It is a GUI program,
but it ships a documented command-line export interface. This project drives that
interface and adds the layer the GUI never had: a parsed, queryable index of a scan.

```
scan "C:"  ->  CSV export  ->  parsed index  ->  summary / top / find / dups / types / reports
```

An agent cannot read a treemap. It *can* read JSON — so the job is to turn
"where did my disk space go" into a deterministic command that returns structured
data, and to survive three measured behaviours of the binary that silently break a
naive wrapper.

## Why this is not a thin wrapper

All three of these were measured against WizTree 4.27 and each has a regression test:

| Behaviour | Consequence |
|---|---|
| **The exit code is always `0`** — even for a target that does not exist | Never trust the exit status; a non-empty artifact is the only success signal |
| **CSV headers are localized** (`文件名称,大小,分配,…` on a Chinese system) | The parser reads columns *positionally*, never by name — `csv.DictReader` is unusable |
| **The column set varies** unless advanced switches are pinned | Every export switch is emitted explicitly, so a scan's schema is reproducible |

Two more were found later by adversarial review and fixed:

| Behaviour | Consequence |
|---|---|
| **The 32-bit build is a launcher** that spawns the 64-bit one and returns immediately | Judging on "process exited, no file" reports a false failure; discovery prefers 64-bit and the driver waits for the artifact |
| **An unwritable destination opens a modal dialog** that never closes | 30 minutes of silence on a default timeout; the output directory is now probed *before* WizTree starts |

And two more were found by driving the tool against a real 331 GB volume:

| Behaviour | Consequence |
|---|---|
| **`/admin=1` self-elevates.** WizTree relaunches itself through `ShellExecute(runas)`; the process the CLI started returns in seconds while the elevated child is still scanning | A fixed post-return grace expired mid-scan and reported "produced no output" for a scan that was running perfectly. A live WizTree process now extends the wait to `--timeout` |
| **The export is streamed, and locked while it is written.** The CSV was non-empty within seconds and then grew 3.7 MB → 818 MB, held with an exclusive lock throughout | "File exists and is non-empty" is not "finished": it would hand the parser a truncated file, and a concurrent read raised a bare `PermissionError`. Completion now means *no writer process + a size that stopped moving + a file that opens* |

The same run also produced the throughput numbers that shaped the parser:
**folders-only (`--folders-only`) exported 653k rows / 103 MB in 24.7 s, where the
full file-level export was 8.14 M rows / 818 MB and took minutes.** If the
question is "what is filling this disk", folder rows are the whole answer — file
rows cost 8× the time and 8× the space to say the same thing.

## Requirements

- **Windows** (WizTree is Windows-only; the macOS build needs its own arg builder)
- **Python 3.10+**
- **WizTree 3.18 or newer** installed — <https://diskanalyzer.com/download>

## Install

```bash
git clone https://github.com/Practice019/wiztree-cli
cd wiztree-cli
pip install .

# verify
cli-anything-wiztree info --json
```

For development (edits take effect immediately):

```bash
pip install -e .
```

> **Note on self-containment.** `pip install .` copies the package into
> site-packages, so the CLI keeps working even if you delete this checkout.
> `pip install -e .` records a path back to the source tree instead — convenient
> while developing, but the CLI breaks if the tree moves.

## Quick start

```bash
cli-anything-wiztree info --json                    # where is WizTree, what can it do
cli-anything-wiztree scan run "C:" --json           # scan and index
cli-anything-wiztree query summary                  # ask the index (no re-scan)
cli-anything-wiztree query top -n 20 --files
cli-anything-wiztree query find "*.log" --min-size 100m
cli-anything-wiztree query dups --min-size 1m
cli-anything-wiztree export report -o report.md --format markdown
cli-anything-wiztree scan treemap "C:" -o c.png     # 1920x1080 by default
cli-anything-wiztree                                # interactive REPL
```

## Command reference

| Group | Commands | Purpose |
|---|---|---|
| *(root)* | `info`, `repl` | Environment check, interactive mode |
| `scan` | `run`, `filetypes`, `treemap`, `mft` | Produce exports from WizTree |
| `query` | `summary`, `top`, `find`, `dups`, `types` | Query the last scan without re-scanning |
| `export` | `report`, `presets` | Write csv / json / table / markdown / html |
| `project` | `new`, `info`, `profiles`, `profile-add`, `profile-remove` | Named scan snapshots |
| `session` | `status`, `history`, `undo`, `redo` | Operation history |

Global options (`--json`, `--project`, `--exe`, `--admin/--no-admin`) work **before
or after the verb**: `--json info` and `info --json` are equivalent.

### `scan run`

```bash
cli-anything-wiztree scan run "C:"                              # whole drive
cli-anything-wiztree scan run "D:/projects" --json              # one folder
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
| `--mft-recno`, `--utc`, `--all-dates`, `--split-name`, `--drive-capacity` | WizTree's advanced columns |
| `--allow-empty` | Accept an export with zero entries |
| `--timeout N` | Seconds before a scan is abandoned (default 1800) |

**An empty result is an error, not a success.** When a filter matches nothing
WizTree writes a banner and a header with no data rows — byte-for-byte what a
silent failure looks like. `scan run` therefore exits non-zero and keeps the CSV
for inspection. Pass `--allow-empty` when an empty result is expected.

**Wildcards are data, never paths.** Template arguments pass through untouched:
`query find "*.mp4"` searches the index, it does not expand into the `.mp4` files
of your working directory. (Click's `windows_expand_args` is disabled for exactly
this reason.)

### `query`

```bash
cli-anything-wiztree query summary --json
cli-anything-wiztree query top -n 20 --files
cli-anything-wiztree query top -n 10 --folders --by allocated
cli-anything-wiztree query find "*.log" --min-size 100m
cli-anything-wiztree query find "*" --ext mp4 --limit 50
cli-anything-wiztree query dups --min-size 1m
cli-anything-wiztree query types -n 15
```

`query dups` matches on **name + size**, which is WizTree's own duplicate rule — it
never compares contents. Verify before deleting.

### `index` — build once, then stay fresh

Every `scan run` re-reads the whole volume. Measured on a 331 GB volume with
3.3 M files, that is ~25 s for the scan plus the export and parse on top. The
`index` commands pay it **once** and then apply only what changed, using the
NTFS USN change journal — the same mechanism Everything is built on.

```bash
cli-anything-wiztree index build "D:" -o index_D.live   # once
cli-anything-wiztree index sync  "D:" -o index_D.live   # after that
cli-anything-wiztree index status --index index_D.live
cli-anything-wiztree index top   --index index_D.live -n 20
cli-anything-wiztree index find  "_cache" --index index_D.live
```

Measured end to end on `D:` (3,593,757 entries: 476,820 folders, 3,116,937 files):

| Stage | Time | Notes |
|---|---:|---|
| `$MFT` dump | **16.5 s** | 5.30 GiB raw, written by WizTree |
| parse (8 processes) | **44.0 s** | fixed-size records, sharded by byte range |
| recursive folder totals | **27.6 s** | paid here so queries never re-roll the volume |
| save | **6.5 s** | 123 MB index |
| **`index build` total** | **~102 s** | once |
| **`index sync`** — journal read | **0.02–0.14 s** | thousands of records, not millions |
| `index top` — query | **0.5 s** | reads the stored totals |
| `index status` | **3.1 s** | metadata only, no node materialization |

Why the dump-and-parse route instead of WizTree's own file-level CSV export: the
CSV is 818 MB / 8.14 M rows and takes **~190 s** end to end, because it is text
that has to be re-parsed. The raw `$MFT` is a flat array of fixed-size records
keyed by the same file reference number the USN journal uses, so it parses faster
*and* needs no join.

Two correctness notes from building it, both of which produce a *silently wrong*
index rather than an error:

* **A folder-target scan emits synthetic record numbers.** WizTree reported the
  scanned root as record 5 — the volume root — where `fsutil file queryfileid`
  and the USN journal both say 3665549. Only a **volume** scan yields real ones,
  so `index build` always scans the volume.
* **An FRN is not a record number.** The low 48 bits are the record number; the
  high 16 are a reuse sequence number. Comparing them raw matches nothing.

`index sync` refuses rather than guesses when the delta cannot be trusted: if the
journal was reset, or wrapped past the checkpoint, it raises and tells you to
rebuild. A non-zero `orphans` count in the result means records referenced a
parent the index does not hold, i.e. there is a hole.

Still single-process: everything above the journal read (loading 3.59 M entries
costs ~18 s, and a sync rewrites the 123 MB index). A daemon holding the index
resident — which is exactly why Everything runs as a service — is the next step;
the mechanism itself is already millisecond-scale.

### The sidecar index (why the second question is fast)

`scan run` parses the export once and memoizes the result in
`<export>.wiztree-index.pkl` beside it, because *every* `query` subcommand loads
the whole export. Without it, asking a second question about the same scan
re-parsed the file from scratch.

Measured on a 653k-row / 99 MB folders-only export:

| | Before | After |
|---|---:|---:|
| First parse (cold) | 18.2 s | 8.2 s |
| Repeat query (cache hit) | 18.2 s | **3.8 s** |

The cache is keyed on `(size, mtime_ns, version)`, so a re-export invalidates it
automatically, and it is only written for exports over 4 MB — below that the
parse is already faster than the bookkeeping. A corrupt or foreign cache is
ignored and the file is re-parsed; unpickling is restricted to the entry class,
so a planted cache can produce wrong numbers but never run code. The sidecar
costs ~1.2× the export's size on disk (121 MB for a 99 MB export). To opt out:

```bash
export CLI_ANYTHING_WIZTREE_NO_CACHE=1     # PowerShell: $env:CLI_ANYTHING_WIZTREE_NO_CACHE=1
```

### Other WizTree export modes

```bash
cli-anything-wiztree scan filetypes "C:" -o types.csv -n 20   # per-extension totals
cli-anything-wiztree scan treemap "C:" -o c.png               # defaults to 1920x1080
cli-anything-wiztree scan mft "D:" -o d.mft                   # raw NTFS $MFT
```

`scan mft` does **not** require elevation (verified: a non-admin dump produced a
valid `FILE0` stream). Be aware the dump is the complete raw MFT and can be
several GB. Elevation is required for `/exportmftrecno` (MFT record numbers
inside a CSV), which is a different feature.

## JSON output and exit codes

Every command accepts `--json` and prints one JSON document: an object for single
results, an array for lists, and `{"error": ..., "type": ...}` plus a non-zero exit
code on failure. Usage errors go through the same channel, so `--json` never
returns empty stdout with a human message on stderr.

| Code | Meaning |
|---|---|
| `0` | Success — and for `scan`, the artifact was verified to contain a data row |
| `1` | A handled failure (WizTree missing, no output produced, empty export, bad project, bad size string) |
| `2` | A usage error (unknown option, missing argument) |

WizTree's own exit status is meaningless (always `0`); this CLI's is not.

## How WizTree is discovered

`cli-anything-wiztree info` reports which route won (`source`). Order:

1. `--exe <path>`
2. `$WIZTREE_EXE`
3. the remembered path in `<state home>/config.json`
4. **the Windows uninstall registry** (`InstallLocation` / `DisplayIcon`) — no
   machine-specific path is baked into the source
5. `PATH` (64-bit binaries preferred; the 32-bit launcher's sibling is added)
6. `%ProgramFiles%`-style locations derived from the environment
7. user-configured `search_paths`

```json
{ "search_paths": ["E:\\tools\\WizTree\\WizTree64.exe"] }
```

Discovery never writes configuration — only `info` persists the path it found,
after showing it to you. A remembered path that has gone missing is skipped.

| Variable | Purpose |
|---|---|
| `WIZTREE_EXE` | Path to `WizTree64.exe`; overrides discovery |
| `CLI_ANYTHING_WIZTREE_HOME` | State directory (default `~/.cli-anything-wiztree`) |

## Testing

```bash
python -m pytest                          # all 154 (the E2E half needs WizTree installed)
python -m pytest src/cli_anything/wiztree/tests/test_core.py      # 130 unit tests, ~2s, no WizTree needed
python -m pytest src/cli_anything/wiztree/tests/test_full_e2e.py  # 24 E2E tests, real WizTree + real scans
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest                   # require the installed command, not the fallback
python validate_self.py                   # 63 structural / convention checks
```

The split is deliberate:

- **`test_core.py` — 130 tests, ~2 seconds, runs anywhere.** Parser, query, project,
  session, argument builders and artifact validation, all on synthetic data. Every
  discovery test monkeypatches the registry / config / `PATH` providers shut, so
  these never touch a real WizTree installation. This is the suite CI runs.
- **`test_full_e2e.py` — 24 tests, real backend, no graceful degradation.** They
  invoke the actual `WizTree64.exe`, cross-check its totals against an independent
  `os.walk` (5% tolerance), and drive the CLI as a subprocess via `_resolve_cli`.
  If WizTree is missing they **fail, they do not skip** — a harness whose backend
  cannot be tested is worth nothing.

See [`src/cli_anything/wiztree/tests/TEST.md`](src/cli_anything/wiztree/tests/TEST.md)
for the test plan, the reconnaissance findings, the recorded results, and the
24 defects found and fixed during development and review.

## Known limitations

- **`query dups` is a candidate list, not proof.** Base name + size only; file
  contents are never compared. Hard links are not excluded from the reclaimable
  estimate, so that figure can be optimistic.
- **The sidecar index costs disk.** A cache hit on a 99 MB export costs 3.8 s
  instead of 8.2 s, but the `.wiztree-index.pkl` beside it is ~121 MB. Delete the
  sidecar to reclaim it; it is rebuilt on the next parse. `scan mft` and
  `scan treemap` are not cached at all.
- **Logical size ≠ on-disk usage.** WizTree reports hard-linked files once per link
  (measured: 135.8 GB logical vs 119.0 GB allocated on a test volume). Use
  `--by allocated` for capacity questions.
- **No date or regex search.** WizTree's GUI search language for dates, `namelen`
  and regex has no export equivalent. `query find` supports glob, extension and
  size range only.
- **Windows only.** WizTreeMac takes `--paramname` instead of `/paramname`; a port
  needs a second arg builder in `utils/wiztree_backend.py`.
- **macOS/Linux cannot be supported at all** — there is no WizTree to drive.

## Documentation

- [`docs/WIZTREE.md`](docs/WIZTREE.md) — the software SOP: architecture analysis,
  GUI→CLI command map, measured behaviours, rendering gap assessment
- [`src/cli_anything/wiztree/README.md`](src/cli_anything/wiztree/README.md) — the
  package-level README that ships in the wheel
- [`SKILL.md`](SKILL.md) — the agent-facing skill definition
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — attribution

## License

Apache-2.0 — see [`LICENSE`](LICENSE).

WizTree itself is proprietary software by Antibody Software Limited. It is **not**
distributed here; you must install it separately. This project only drives its
documented command-line interface.
