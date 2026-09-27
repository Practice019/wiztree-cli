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
| Scan is very slow on a whole drive | Running unelevated | Pass `--admin` (accepts a UAC prompt) |
| `Project has no scan attached` | `query` before any `scan`, or wrong `--project` | Run `scan run <target>` first |
| Duplicates look wrong | Name+size matching cannot see content differences | Verify before deleting — the output says so too |

## What this skill does not do

- Compare file **contents** for duplicates (name + size only, same as WizTree).
- Date/regex file search (WizTree's GUI-only search language).
- macOS (WizTreeMac uses `--paramname`; this harness targets the Windows build).
