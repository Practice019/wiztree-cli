# AGENTS.md

Operating manual for AI agents driving `cli-anything-wiztree`.

This file is written for a model, not a human. It states what the tool is, which
command answers which question, the exact numbers measured on real hardware, and
the failure modes that produce **silently wrong answers** rather than errors. Read
the traps section before your first destructive recommendation.

---

## 1. What this is

`cli-anything-wiztree` is a CLI wrapper around **WizTree**, a closed-source
Windows disk-space analyzer. WizTree reads the NTFS Master File Table and is fast;
it has no queryable interface and its output is a GUI treemap. This project turns
a scan into a **parsed, queryable index** so an agent can ask questions in JSON.

```
scan "C:"  ->  CSV export  ->  parsed index  ->  summary / top / find / dups / types / report
```

Two execution paths exist. Know which one you are on:

| Path | Command | First cost | Repeat cost | Requires |
|---|---|---:|---:|---|
| **Export + parse** | `scan run` then `query *` | 25 s – 190 s | 0.5 – 4 s (sidecar cache) | WizTree |
| **MFT index** | `index build` then `index *` | ~29 s – 102 s | 0.1 – 1.5 s | WizTree + **elevation** |

The MFT index is roughly **4× faster to build and 3× faster to query** than the
CSV route on a 331 GB volume. Prefer it whenever you will ask more than one
question.

---

## 2. Non-negotiable prerequisites

Verify these before promising the user anything.

| Requirement | Why it is fatal | How to check |
|---|---|---|
| **Windows** | WizTree is Windows-only. There is no Linux/macOS build to drive. | `os.name == 'nt'` |
| **WizTree 3.18+ installed** | Closed-source and **not redistributable**. Without it, `scan`, `query`, `export` and `index build` all fail — `index build` uses WizTree's `/dumpmftfile=` to obtain the raw `$MFT`. | `cli-anything-wiztree --json info` |
| **Python 3.10+** | Uses `X \| Y` unions and `slots=True` dataclasses | `python --version` |
| **Elevation** (MFT index only) | The raw volume handle `\\.\C:` and the USN journal both require admin | `info` reports `is_admin` |

**`--admin` elevates WizTree, not you.** It makes WizTree relaunch itself through
`ShellExecute(runas)`. Your own Python process stays unelevated, so the USN journal
still fails:

```json
{"error": "cannot open \\\\.\\C: for reading (WinError 5); reading the USN journal needs elevation",
 "type": "JournalUnavailable"}
```

Fix: run the **whole CLI** in an elevated shell. `--admin` alone is not enough.

The first command on any new machine:

```bash
cli-anything-wiztree --json info
```

```json
{"found": true, "exe": "C:\\Program Files\\WizTree\\WizTree64.exe",
 "version": "4.27.0.0", "is_admin": false, "source": "registry"}
```

`is_admin: false` is **normal and usable** — WizTree falls back to walking
directories, which works but is slower on large drives.

---

## 3. Choosing a command

### By question

| The user asks | Command | Notes |
|---|---|---|
| "What is filling this disk?" | `index top --index F -n 30` | Prefer; see §4 |
| "What is filling this disk?" (no index yet) | `scan run "C:" --folders-only` then `query top -n 30` | Folder rows *are* the answer |
| "Biggest files?" | `query top -n 25 --files` / `index top --files` | |
| "Find files matching X" | `query find "*.log" --min-size 100m` / `index find X` | Glob + extension + size only |
| "Duplicate files?" | `query dups --min-size 1m` | **Candidates, not proof** — see §6 |
| "What file types dominate?" | `query types -n 15` | |
| "Give me a report" | `export report -o r.md --format markdown` | csv/json/table/markdown/html |
| "Show me a picture" | `scan treemap "C:" -o c.png` | 1920×1080 default |
| "What changed since last time?" | `index sync` then `index status` | Journal delta, 0.02–0.14 s |

### By scope

Narrow the target whenever the question allows it. Measured on a 331 GB volume:

| Target | Rows | Export size | Time |
|---|---:|---:|---:|
| Whole volume, file-level | 8.14 M | 818 MB | minutes |
| Whole volume, `--folders-only` | 653 k | 103 MB | **24.7 s** |
| One folder | proportionally less | | |

`scan run "D:\projects"` beats `scan run "D:"` by a wide margin. `--max-depth N`
cuts folder rows further.

---

## 4. Measured numbers (do not re-derive, cite these)

Hardware: Intel i5-1135G7 (4C/8T, 15 W), 15.8 GiB RAM, single NVMe, Windows 11.
Volume: 331 GB, 3.59 M entries (476,820 folders + 3,116,937 files).

### `index build` on `D:` — 102 s, once

| Stage | Time |
|---|---:|
| `$MFT` dump (5.30 GiB raw, written by WizTree) | 16.5 s |
| parse, 8 processes, sharded by byte range | 44.0 s |
| recursive folder totals | 27.6 s |
| save (123 MB index) | 6.5 s |

On a smaller volume (`C:`, 1.14 M records) the same build takes **21.7 s**:
dump 5.7 s, parse 8.1 s, totals 5.6 s, save 1.6 s, index 50 MB.

### Queries and sync

| Operation | Time |
|---|---:|
| `index sync` — journal read | **0.02 – 0.14 s** |
| `index top` — query | 0.5 s (0.12 s warm) |
| `index find` — query | 0.2 – 1.5 s |
| `index status` — fast path | 2.4 – 4.6 s |
| `query *` — sidecar cache hit | 3.8 s |
| `query *` — cold parse of 653 k rows | 8.2 s |

### Where the time actually goes

A full `index sync` reads the journal in 0.02 s but still costs **~31 s wall**:
18 s loading 3.59 M entries, 6 s rewriting the index, the rest in the child map.
The mechanism is millisecond-scale; the process lifecycle is not. **There is no
daemon** — every invocation pays the load. Do not tell the user "sync is instant";
say "the journal delta is instant, the round trip is ~31 s".

---

## 5. Traps that produce *wrong* answers, not errors

These were each found by running against a real volume. All have regression tests.

### 5.1 A folder-target scan emits synthetic record numbers

WizTree reported the scanned root as MFT record **5** (i.e. `$Root`), where both
`fsutil file queryfileid` and the USN journal say **3665549**.

**Consequence:** an index built from a folder scan has garbage record numbers and
will never match the journal. `index build` therefore always scans the **volume**,
whatever you pass. Do not try to index `D:\projects` alone.

### 5.2 An FRN is not a record number

A File Reference Number is 64-bit: low 48 bits = record number, high 16 = reuse
sequence. Comparing an FRN raw against WizTree's `MFTRECNO` matches **nothing**.

**Consequence:** if you ever compute FRNs yourself, mask with `0xFFFFFFFFFFFF`.

### 5.3 `$FILE_NAME` namespaces and DOS 8.3 aliases

A file has multiple `$FILE_NAME` attributes. Taking the first gives `PROGRA~1`
instead of `Program Files`.

**Consequence:** name preference must be Win32 > Win32+DOS > POSIX > DOS. The
parser only stops at a namespace ≥ Win32+DOS.

### 5.4 `$FILE_NAME` allocated size is unusable

It is often 0. Real sizes live in `$DATA`; resident data uses a 32-bit content
length, non-resident a 64-bit allocated/real pair at offsets 40/48 in a 64-byte
header.

### 5.5 USN record names must be clamped

An unclamped name slice reads neighbouring bytes and reaches `os.stat` as a bogus
path — which **silently reports size 0** instead of raising. Clamp to
`min(name_offset + name_len*2, record_end)` and verify `MajorVersion == 2`.

### 5.6 WizTree's exit code is always 0

Even for a target that does not exist. Never trust it. This CLI returns non-zero
when no artifact was produced, which is the signal you should use.

### 5.7 CSV headers are localized

`文件名称,大小,分配,…` on a Chinese system. Columns must be read **positionally**;
`csv.DictReader` is unusable.

### 5.8 A non-empty CSV is not a finished CSV

WizTree streams the export and holds it exclusively while writing: the file was
non-empty within seconds and grew 3.7 MB → 818 MB. Completion means *no writer +
size stopped moving + file opens*. A concurrent read raises a bare
`PermissionError`.

### 5.9 `--admin` returns early

WizTree self-elevates, so the launcher exits in seconds while the child is still
scanning. An early return is **not** progress. Only `--timeout` bounds the wait.

### 5.10 A stale WizTree process vetoes completion

An unrelated `WizTree64.exe` left running from a previous session (observed: alive
11 h, 15,687 CPU s) made a scan that had already written its 64 MB export wait out
the full 1800 s timeout. The process probe is therefore consulted **only while no
artifact exists**; an existing artifact is judged on its own state.

---

## 6. Reporting rules

1. **`query dups` is a candidate list.** Name + size only, never contents. Say
   "candidates to review", never "these are duplicates".
2. **Logical size ≠ on-disk usage.** WizTree reports hard-linked files once per
   link (measured: 135.8 GB logical vs 119.0 GB allocated). For capacity questions
   use `--by allocated`, and prefer `index top` which stores allocated totals.
3. **`WinSxS` and similar are hard-link farms.** `Get-ChildItem` measured 9.44 GiB
   where WizTree reported 3.78 GiB for the same tree. Directory-walk sums
   double-count. Trust the MFT-based figure.
4. **Never recommend deletion on size alone.** Identify what a directory *is*
   first. A folder named `..._setup` or `..._installer` may be the live install:
   in one audit, six such "installers" were the running executables of QQ,
   PostgreSQL, Redis, FinalShell, DeskBox and ARCHPR.
5. **A non-zero `orphans` count from `index sync` means there is a hole** in the
   index — records referenced a parent it does not hold. Report it; do not paper
   over it.

---

## 7. Destructive-operation protocol

When the task is cleanup, follow this order. Each gate has caught a real mistake.

```
1. Measure      index build / scan --folders-only  -> sizes, no filesystem walk
2. Identify     read the directory's contents; decide WHAT it is
3. Classify     regenerable | user data | system-required
4. Propose      list with sizes + consequence, let the human choose
5. Gate         exclude anything containing a running process's executable
6. Execute      concurrent `cmd /c rd /s /q`, 8 at a time
7. Verify       re-measure; report actual bytes freed, not predicted
```

**Gate 5 in detail.** Before deleting a directory, resolve every running
process's `Path` and every running service's executable, and skip any target that
contains one:

```powershell
$live = (Get-Process | Where-Object Path).Path
$live += (Get-CimInstance Win32_Service |
          Where-Object { $_.State -eq 'Running' -and $_.PathName } |
          ForEach-Object { ($_.PathName -replace '^"?([^"]+\.exe).*$','$1') })
# skip $target if any $p in $live satisfies $p.StartsWith($target)
```

**Gate 7 matters.** Predicted and actual diverge when files are locked. Report the
measured delta from `Get-PSDrive`, and list anything that came back
`部分残留`/partial.

**Never delete without confirmation**: anything under `Documents`, `Desktop`,
`Downloads`, `Pictures`; anything in `Windows\Installer` (breaks repair/uninstall);
`System Volume Information` (restore points); `Recovery\Customizations` (factory
reset); any `node_modules`, `.venv`, `target` belonging to a repo the user is
actively building.

---

## 8. Output contract

Always pass `--json`. You get exactly one document on stdout:

| Situation | Shape |
|---|---|
| Single result | object |
| List result | array, or object with `results` |
| Failure | `{"error": "...", "type": "..."}` + non-zero exit |

| Exit code | Meaning |
|---|---|
| `0` | Success. For `scan`, the artifact was verified to contain a data row |
| `1` | Handled failure (WizTree missing, no output, empty export, bad project, bad size string) |
| `2` | Usage error (unknown option, missing argument) |

`--json` never leaves stdout empty with a human message on stderr.

---

## 9. Environment variables

| Variable | Purpose |
|---|---|
| `WIZTREE_EXE` | Explicit `WizTree64.exe`; overrides discovery |
| `CLI_ANYTHING_WIZTREE_HOME` | State directory (default `~/.cli-anything-wiztree`) |
| `CLI_ANYTHING_WIZTREE_NO_CACHE` | Set to `1` to stop writing `.wiztree-index.pkl` sidecars |

---

## 10. Discovery order

`info` reports which route won in `source`.

1. `--exe <path>`
2. `$WIZTREE_EXE`
3. remembered path in `<state home>/config.json`
4. Windows uninstall registry (`InstallLocation` / `DisplayIcon`)
5. `PATH` (64-bit preferred; the 32-bit launcher's sibling is added)
6. `%ProgramFiles%`-derived locations
7. user `search_paths` from `config.json`

No machine-specific path is baked into the source; `DEFAULT_SEARCH_PATHS` is
deliberately empty. Discovery never writes config — only `info` persists what it
found, after showing it.

---

## 11. Verification checklist

Before reporting success on a new machine:

```bash
cli-anything-wiztree --json info                    # found: true, note is_admin
cli-anything-wiztree --json scan run "C:" --folders-only   # or index build C:
cli-anything-wiztree --json query summary           # entry_count > 0
python -m pytest src/cli_anything/wiztree/tests/test_core.py -q   # 173 pass, no WizTree needed
```

`test_core.py` (173) and `test_liveindex.py` (27) run anywhere. `test_full_e2e.py`
(24) drives the real `WizTree64.exe` and **fails rather than skips** when the
backend is unusable — a harness whose backend cannot be tested is worth nothing.

---

## 12. Known limitations — state these plainly

- **Windows only.** WizTreeMac takes `--paramname`, not `/paramname`; a port needs
  a second arg builder.
- **No date or regex search.** WizTree's GUI search language has no export
  equivalent. `query find` does glob, extension and size range only.
- **Duplicates are name + size**, never contents. Hard links are not excluded from
  the reclaimable estimate, so that number can be optimistic.
- **The sidecar cache costs disk** — ~1.2× the export size (121 MB for 99 MB).
  `scan mft` and `scan treemap` are not cached.
- **No daemon.** Every invocation reloads the index (~18 s at 3.59 M entries).
  This is the largest remaining fixed cost; the journal mechanism itself is
  already millisecond-scale.
