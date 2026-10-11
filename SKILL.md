---
name: "cli-anything-wiztree"
description: "Drive WizTree (Windows NTFS disk-space analyzer) from the command line: scan drives or folders, then query the resulting index for largest files, duplicates, and file-type breakdowns. Use for disk-space analysis, finding what fills a drive, locating duplicate files, or producing storage reports on Windows."
---

# cli-anything-wiztree

Agent-native CLI for **WizTree**, the fast Windows disk-space analyzer.

## When to use this skill

Use it whenever a task involves *what is taking up space on a Windows disk*:

- "Why is my C: drive full?" → `scan run "C:"` then `query summary` / `query top`
- "Find the biggest files under D:\projects" → `scan run "D:\projects"` then `query top --files`
- "Are there duplicate files?" → `query dups --min-size 1m`
- "What file types dominate this folder?" → `query types`
- "Give me a report of this drive" → `export report -o report.md --format markdown`
- "Is there an image of the layout?" → `scan treemap "C:" -o c.png`

## Prerequisites

- Windows, with **WizTree 3.18+** installed (<https://diskanalyzer.com/download>)
- The CLI on PATH: `pip install .` from the repository root
- If WizTree is portable, set `WIZTREE_EXE` to the full path of `WizTree64.exe`

Always confirm the environment first:

```bash
cli-anything-wiztree --json info
```

```json
{"found": true, "exe": "D:\\software\\WizTree\\WizTree64.exe", "version": "4.27.0.0", "is_admin": false}
```

`is_admin: false` is normal and still works — it just means WizTree walks
directories instead of reading the MFT, so large drives take longer. Only pass
`--admin` if a UAC prompt is acceptable in the environment.

## Usage

```bash
cli-anything-wiztree --help
cli-anything-wiztree scan run "C:" --json
cli-anything-wiztree query top -n 20 --json
```

### Command groups

| Group | Commands | Purpose |
|---|---|---|
| *(root)* | `info`, `repl` | Environment check, interactive mode |
| `scan` | `run`, `filetypes`, `treemap`, `mft` | Produce exports from WizTree |
| `query` | `summary`, `top`, `find`, `dups`, `types` | Ask questions of the last scan (no re-scan) |
| `export` | `report`, `presets` | Write csv/json/table/markdown/html reports |
| `project` | `new`, `info`, `profiles`, `profile-add`, `profile-remove` | Named scan snapshots |
| `session` | `status`, `history`, `undo`, `redo` | Operation history |

### Global options

| Option | Effect |
|---|---|
| `--json` | Emit one JSON document instead of human text |
| `--project PATH` | Use a specific project file (default `~/.cli-anything-wiztree/default.json`) |
| `--exe PATH` | Explicit `WizTree64.exe` |
| `--admin / --no-admin` | Request the elevated MFT scan (default `--no-admin`) |

## Agent guidance

1. **Always pass `--json`** — output is then a single parseable document: an object
   for single results, an array for lists, and `{"error": ..., "type": ...}` on failure.
2. **Check the exit code, not WizTree's.** WizTree itself always exits `0`, even for
   a non-existent target. This CLI returns non-zero when no artifact was produced,
   so `scan` failing loudly means the scan really did not happen.
3. **Scan once, query many times.** `scan run` persists an index; `query *` reads it
   without touching the disk. Re-scanning an entire drive for every question is the
   main way to waste time here.
4. **`query dups` is a candidate list, not proof.** It matches base name + size,
   exactly like WizTree's own detector, and never compares contents. Report it as
   candidates to review.
5. **Quote targets with spaces** and prefer a folder over an entire drive when the
   question is scoped: `scan run "D:\projects"` is far faster than `scan run "D:"`.
6. **`--all-dates`, `--split-name`, `--drive-capacity` change the CSV schema.** They
   are off by default precisely so repeated scans stay comparable.
7. **Ask a capacity question with `--folders-only`.** Measured on a 331 GB volume:
   folder rows were 653k rows / 103 MB / 24.7 s, where the default file-level
   export was 8.14 M rows / 818 MB and took minutes. If the question is "what is
   filling this disk", the folder rows *are* the answer — the file rows cost ~8×
   the time and ~8× the disk to say the same thing. Add `--max-depth N` to cut it
   further.
8. **`--admin` self-elevates, so budget for a UAC prompt and a slower return.**
   WizTree relaunches itself elevated; the launcher returns in seconds while the
   scan continues. The CLI waits for the real end (no writer process, size
   settled, file readable), so a bare `--timeout` is the honest bound — do not
   read an early launcher return as progress.
9. **Repeat questions about one scan are cheap.** The parsed index is memoized in
   `<export>.wiztree-index.pkl`, keyed on the export's size and mtime; a hit costs
   3.8 s against 8.2 s to re-parse a 653k-row export. The sidecar is ~1.2× the
   export size on disk — delete it to reclaim the space, and set
   `CLI_ANYTHING_WIZTREE_NO_CACHE=1` to stop it being written.

## Examples

```bash
# Full triage: scan, summarise, list the biggest files
cli-anything-wiztree --json scan run "C:"
cli-anything-wiztree --json query summary
cli-anything-wiztree --json query top -n 25 --files

# Find large logs anywhere on D:
cli-anything-wiztree scan run "D:" --filter "*.log" --files-only
cli-anything-wiztree --json query find "*.log" --min-size 100m

# Duplicate hunt with a report
cli-anything-wiztree --json query dups --min-size 1m
cli-anything-wiztree export report -o dups.md --format markdown --top 100

# File types, straight from WizTree's own report mode
cli-anything-wiztree scan filetypes "C:" -o types.csv -n 25

# A project per drive, so scans do not overwrite each other
cli-anything-wiztree --project c.json scan run "C:"
cli-anything-wiztree --project c.json query top -n 10
```

## Failure modes and what to do

| Symptom | Cause | Fix |
|---|---|---|
| `WizTree not found` with a download URL | Not installed / not on PATH | Install it, or set `WIZTREE_EXE`, or pass `--exe` |
| `... but produced no output CSV export` | Target missing or unreadable, output dir not writable, or non-NTFS volume | Check the target path; use `--admin` if elevation is needed |
| Error names `PermissionError` on the CSV | A streamed export still held by its writer | No longer reachable: completion waits for the writer to exit. If you see it, the export was read by a second process mid-scan |
| Scan is very slow on a whole drive | Running unelevated | Pass `--admin` (accepts a UAC prompt). If a UAC prompt cannot be accepted, the unelevated walk is the only option and it is slow by construction |
| `--admin` returns fast but reports no CSV | It no longer should — the CLI waits for the elevated child | Update; and check the UAC prompt was actually accepted |
| Every `query` on a big export takes ~10 s | Parse cost, on the first run only | Second run hits the sidecar index (3.8 s). Use `--folders-only` / `--max-depth` for a smaller export |
| `Project has no scan attached` | `query` before any `scan`, or wrong `--project` | Run `scan run <target>` first |
| Duplicates look wrong | Name+size matching cannot see content differences | Verify before deleting — the output says so too |

## What this skill does not do

- Compare file **contents** for duplicates (name + size only, same as WizTree).
- Date/regex file search (WizTree's GUI-only search language).
- macOS (WizTreeMac uses `--paramname`; this harness targets the Windows build).
