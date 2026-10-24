---
name: "cli-anything-wiztree"
description: "Drive WizTree (Windows NTFS disk-space analyzer) from the command line: scan drives or folders, then query the result as a structured index for largest folders and files, duplicates, file-type breakdowns, and storage reports. Includes an incremental MFT+USN index that answers repeat questions in about a second. Use for disk-space analysis, finding what fills a drive, locating duplicate files, tracking what grew, or producing storage reports on Windows."
---

# cli-anything-wiztree

Agent-native CLI for **WizTree**, the fast Windows disk-space analyzer.

WizTree reads the NTFS Master File Table, so it is fast, but it has no queryable
interface — its output is a GUI treemap. This CLI turns a scan into a **parsed
index you can ask questions of in JSON**.

## When to use this skill

Use it whenever a task involves *what is taking up space on a Windows disk*:

- "Why is my C: drive full?" → `index top` (or `scan run "C:" --folders-only` → `query top`)
- "Find the biggest files under D:\projects" → `scan run "D:\projects"` → `query top --files`
- "Are there duplicate files?" → `query dups --min-size 1m`
- "What file types dominate this folder?" → `query types`
- "What changed since last time?" → `index sync` → `index status`
- "Give me a report of this drive" → `export report -o report.md --format markdown`
- "Is there an image of the layout?" → `scan treemap "C:" -o c.png`
- "Help me clean up this disk" → follow the destructive-operation protocol below

## Prerequisites — check these first

| Requirement | Why it matters |
|---|---|
| **Windows** | WizTree is Windows-only. There is no Linux/macOS build to drive. |
| **WizTree 3.18+** installed — <https://diskanalyzer.com/download> | Closed-source and **not redistributable**. Without it every `scan`, `query`, `export` and `index build` fails. |
| **Python 3.10+** | `pip install .` from the repository root |

Always confirm the environment before promising anything:

```bash
cli-anything-wiztree --json info
```

```json
{"found": true, "exe": "C:\\Program Files\\WizTree\\WizTree64.exe",
 "version": "4.27.0.0", "is_admin": false, "source": "registry"}
```

`is_admin: false` is **normal and usable** — WizTree falls back to walking
directories, which works but is slower on large drives.

> **`--admin` elevates WizTree, not you.** It makes WizTree relaunch itself via
> `ShellExecute(runas)`; your own process stays unelevated, so the USN journal and
> the raw `\\.\C:` handle still fail with `WinError 5`. For `index build`/`sync`,
> run the **whole CLI** in an elevated shell.

## Two paths — pick deliberately

| Path | Command | First cost | Repeat cost | Needs elevation |
|---|---|---:|---:|---|
| Export + parse | `scan run` → `query *` | 25 s – 190 s | 0.5 – 4 s | no |
| **MFT index** | `index build` → `index *` | 22 – 102 s | **0.1 – 1.5 s** | **yes** |

The MFT index is ~4× faster to build and ~3× faster to query. Prefer it whenever
you will ask more than one question.

## Usage

```bash
cli-anything-wiztree --help
cli-anything-wiztree --json info
cli-anything-wiztree --json scan run "C:" --folders-only
cli-anything-wiztree --json query top -n 20 --files
```

### Command groups

| Group | Commands | Purpose |
|---|---|---|
| *(root)* | `info`, `repl` | Environment check, interactive mode |
| `scan` | `run`, `filetypes`, `treemap`, `mft` | Produce exports from WizTree |
| `query` | `summary`, `top`, `find`, `dups`, `types` | Ask questions of the last scan (no re-scan) |
| **`index`** | **`build`, `sync`, `status`, `top`, `find`** | **MFT index: build once, then stay fresh** |
| `export` | `report`, `presets` | Write csv/json/table/markdown/html reports |
| `project` | `new`, `info`, `profiles`, `profile-add`, `profile-remove` | Named scan snapshots |
| `session` | `status`, `history`, `undo`, `redo` | Operation history |

### Global options

| Option | Effect |
|---|---|
| `--json` | Emit one JSON document instead of human text |
| `--project PATH` | Use a specific project file (default `~/.cli-anything-wiztree/default.json`) |
| `--exe PATH` | Explicit `WizTree64.exe` |
| `--admin / --no-admin` | Request WizTree's elevated MFT scan (default `--no-admin`) |

Global options work **before or after** the verb: `--json info` ≡ `info --json`.

### `index` — the fast path

```bash
# one-time build (needs an elevated shell)
cli-anything-wiztree index build "D:" -o index_D.live

# afterwards: apply only what the USN journal recorded
cli-anything-wiztree index sync   "D:" -o index_D.live
cli-anything-wiztree index status --index index_D.live
cli-anything-wiztree index top    --index index_D.live -n 30
cli-anything-wiztree index find   "_cache" --index index_D.live
```

Measured on a 331 GB volume (3.59 M entries) and a 120 GB volume (1.14 M records):

| Operation | 331 GB volume | 120 GB volume |
|---|---:|---:|
| `index build` total | ~102 s | **21.7 s** |
| ├ `$MFT` dump | 16.5 s | 5.7 s |
| ├ parse | 44.0 s | 8.1 s |
| ├ folder totals | 27.6 s | 5.6 s |
| └ save | 6.5 s | 1.6 s |
| `index sync` — journal read | 0.02 – 0.14 s | — |
| `index top` query | 0.5 s | 0.12 s |
| `index find` query | 0.2 – 1.5 s | — |
| index file size | 123 MB | 50 MB |

**A full `index sync` still costs ~31 s wall** on the large volume: the journal
delta is instant, but loading 3.59 M entries costs 18 s and rewriting the index
6 s. There is no daemon. Report it honestly: "the delta is instant, the round trip
is ~31 s".

## Agent guidance

1. **Always pass `--json`.** You then get one parseable document: an object for
   single results, an array for lists, `{"error": ..., "type": ...}` on failure.
2. **Check this CLI's exit code, never WizTree's.** WizTree always exits `0`, even
   for a nonexistent target.
3. **Scan once, query many.** `scan run` and `index build` persist an index;
   `query *` and `index *` read it without touching the disk. Re-scanning a whole
   drive per question is the main way to waste time here.
4. **Ask capacity questions with `--folders-only`.** Measured on 331 GB: folder
   rows were 653 k rows / 103 MB / **24.7 s**, where the file-level export was
   8.14 M rows / 818 MB and took minutes. If the question is "what fills this
   disk", folder rows *are* the answer.
5. **Narrow the target.** `scan run "D:\projects"` is far faster than
   `scan run "D:"`. `--max-depth N` cuts folder rows further.
6. **`query dups` is a candidate list, not proof.** Name + size only, never
   contents. Report it as "candidates to review".
7. **Use `--by allocated` for capacity.** WizTree reports hard-linked files once
   per link (measured 135.8 GB logical vs 119.0 GB allocated). Directory-walk sums
   double-count badly — `Get-ChildItem` measured 9.44 GiB for `WinSxS` where the
   MFT said 3.78 GiB.
8. **`--all-dates`, `--split-name`, `--drive-capacity` change the CSV schema.**
   They are off by default so repeated scans stay comparable.
9. **Repeat questions on one export are cheap.** The parse is memoized in
   `<export>.wiztree-index.pkl`, keyed on size + mtime: 3.8 s hit vs 8.2 s cold for
   653 k rows. The sidecar is ~1.2× the export size.

## Traps that yield wrong answers, not errors

Each was found against a real volume; all have regression tests.

| Trap | Consequence |
|---|---|
| **A folder-target scan emits synthetic record numbers** — WizTree reported the scanned root as record 5 (`$Root`) where `fsutil` and the journal say 3665549 | `index build` always scans the **volume**; do not try to index a single folder |
| **An FRN is not a record number** — low 48 bits = record, high 16 = reuse sequence | Comparing raw FRNs matches nothing; mask with `0xFFFFFFFFFFFF` |
| **Multiple `$FILE_NAME` attributes** — the first may be the DOS 8.3 alias | Taking the first gives `PROGRA~1` instead of `Program Files` |
| **`$FILE_NAME` allocated size is often 0** | Real sizes live in `$DATA`; resident uses a 32-bit content length |
| **Unclamped USN name slices** read neighbouring bytes | Reaches `os.stat` as a bogus path and **silently reports size 0** |
| **The export is streamed and exclusively locked** — grew 3.7 MB → 818 MB while "non-empty" | "Non-empty" is not "finished"; a concurrent read raises `PermissionError` |
| **`--admin` returns in seconds** while the elevated child still scans | An early return is not progress; only `--timeout` bounds the wait |
| **A stale WizTree process vetoes completion** — one left alive 11 h made a finished 64 MB export wait the full 1800 s | The process probe is consulted only while no artifact exists |

## Cleanup protocol

When the task is "free up space", follow this order. Every gate has caught a real
mistake.

```
1. Measure     index build / scan --folders-only   -> sizes, no filesystem walk
2. Identify    read the directory contents; decide WHAT it is
3. Classify    regenerable | user data | system-required
4. Propose     list with sizes + consequences; let the human choose
5. Gate        exclude anything containing a running process's executable
6. Execute     concurrent `cmd /c rd /s /q`, 8 at a time
7. Verify      re-measure; report bytes actually freed
```

**Gate 5 is not optional.** A folder named `..._setup` or `..._installer` may be
the live install. In one audit, six such "installers" were the running
executables of QQ, PostgreSQL, Redis, FinalShell, DeskBox and ARCHPR — deleting
them would have broken all six.

```powershell
$live = (Get-Process | Where-Object Path).Path
$live += (Get-CimInstance Win32_Service |
          Where-Object { $_.State -eq 'Running' -and $_.PathName } |
          ForEach-Object { ($_.PathName -replace '^"?([^"]+\.exe).*$','$1') })
# skip $target if any $p in $live satisfies $p.StartsWith($target)
```

**Never delete without explicit confirmation:** `Documents`, `Desktop`,
`Downloads`, `Pictures`; `Windows\Installer` (breaks repair/uninstall);
`System Volume Information` (restore points); `Recovery\Customizations` (factory
reset); any `node_modules` / `.venv` / `target` in a repo being actively built.

## Examples

```bash
# Fastest full triage on an elevated shell
cli-anything-wiztree index build "C:" -o index_C.live
cli-anything-wiztree --json index top --index index_C.live -n 30
cli-anything-wiztree --json index find "cache" --index index_C.live

# Export route when elevation is impossible
cli-anything-wiztree --json scan run "C:" --folders-only
cli-anything-wiztree --json query summary
cli-anything-wiztree --json query top -n 25 --files

# Large logs anywhere on D:
cli-anything-wiztree scan run "D:" --filter "*.log" --files-only
cli-anything-wiztree --json query find "*.log" --min-size 100m

# Duplicate hunt with a report
cli-anything-wiztree --json query dups --min-size 1m
cli-anything-wiztree export report -o dups.md --format markdown --top 100

# What changed since the last sync
cli-anything-wiztree --json index sync "D:" -o index_D.live
cli-anything-wiztree --json index status --index index_D.live

# A project per drive, so scans do not overwrite each other
cli-anything-wiztree --project c.json scan run "C:"
cli-anything-wiztree --project c.json query top -n 10
```

## Failure modes and what to do

| Symptom | Cause | Fix |
|---|---|---|
| `WizTree not found` with a download URL | Not installed / not on PATH | Install it, set `WIZTREE_EXE`, or pass `--exe` |
| `cannot open \\.\C: ... needs elevation` (`JournalUnavailable`) | `--admin` elevates WizTree only | Run the whole CLI in an elevated shell |
| `CheckpointLost` on `index sync` | Journal was reset or wrapped past the checkpoint | Rebuild with `index build`; do not trust a stale delta |
| `orphans` > 0 in a sync result | Records referenced a parent the index does not hold | There is a hole in the index — report it, consider a rebuild |
| `... but produced no output CSV export` | Target missing/unreadable, output dir unwritable, non-NTFS volume | Check the target; the output dir is probed before WizTree starts |
| Scan very slow on a whole drive | Running unelevated | Pass `--admin` if a UAC prompt is acceptable; otherwise the walk is slow by construction |
| `--admin` returns fast but reports no CSV | Should no longer happen — the CLI waits for the elevated child | Check the UAC prompt was actually accepted |
| Every `query` takes ~10 s | Cold parse of a large export | Second run hits the sidecar (3.8 s); use `--folders-only` / `--max-depth` |
| `Project has no scan attached` | `query` before any `scan`, or wrong `--project` | Run `scan run <target>` first |
| Duplicates look wrong | Name + size cannot see content differences | Verify before deleting — the output says so too |
| Empty export exits non-zero | A filter matched nothing, which looks like a silent failure | Pass `--allow-empty` when empty is expected |

## What this skill does not do

- Compare file **contents** for duplicates (name + size only, same as WizTree).
- Date or regex file search (WizTree's GUI-only search language).
- Run on macOS or Linux — there is no WizTree to drive. WizTreeMac uses
  `--paramname`; a port needs a second arg builder in `utils/wiztree_backend.py`.
- Keep the index resident between calls. Every invocation reloads it (~18 s at
  3.59 M entries). That is the largest remaining fixed cost.
