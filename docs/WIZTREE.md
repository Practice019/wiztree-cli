# WizTree — Software SOP for CLI Harness Construction

Software: **WizTree 4.27** (`WizTree64.exe`, 10,868,848 bytes, Antibody Software Limited)
Vendor: <https://diskanalyzer.com> · Guide: <https://diskanalyzer.com/guide>
License: proprietary; **no source code available**

This document is Phase 1 of the CLI-Anything methodology applied to WizTree: what
the software is, how its interface was analysed, what the CLI maps onto, and what
gaps remain.

---

## 1. Architecture analysis

### 1.1 No source, no library — but a real CLI surface

WizTree is a closed-source Delphi/VCL Windows GUI application. There is no library
to import and no plugin API. Unlike a pure GUI program, however, it **does** expose a
documented command-line interface, which makes this a *driver + data layer* harness
rather than a GUI-automation harness.

```
WizTree64.exe "drive/folder" /export="filename"
    [/filter="filespec"] [/filterexclude="filespec"] [/filterfullpath=0|1]
    [/admin=0|1] [/exportfolders=0|1] [/exportfiles=0|1] [/sortby=0..3]
    [/exportmftrecno=0|1] [/exportUTCTime=0|1]
    [/exportalldates=0|1] [/exportallsizes=0|1] [/exportsplitfilename=0|1]
    [/exportdrivecapacity=0|1] [/exportpercentofparent=0|1] [/exportmaxdepth=n]
```

Additional modes:

| Mode | Switch | Access in this harness |
|---|---|---|
| Per-extension totals | `/exportfiletypes="f.csv"` (+ `/sortbyfiletypes=0..2`) | `scan filetypes` |
| Treemap image | `/treemapimagefile=`, `/treemapimagewidth=`, `/treemapimageheight=`, `/treemapimagegray=`, `/treemapimagefreespace=`, `/treemapimageshowallocated=` | `scan treemap` |
| Raw NTFS `$MFT` dump | `/dumpmftfile="f"` | `scan mft` |

### 1.2 Backend engine

The scanning engine is the NTFS Master File Table reader, which has two modes:

- **Elevated** (`/admin=1`): reads the MFT directly — the famous near-instant scan.
- **Unelevated** (`/admin=0`, the default here): falls back to a directory walk.

The harness defaults to unelevated so it never surprises a user with a UAC prompt;
`--admin` opts in.

### 1.3 Data model

The CSV export is the data model. Verified layout (banner line, then a header row,
then data rows; **only the path field is quoted**, the numeric dates/values are
not). Real output, with the path generalized:

```
生成由 WizTree 4.27 2026/9/27 14:05:24 (您可以通过捐赠隐藏此信息)
文件名称,大小,分配,修改时间,属性,文件,文件夹
"D:\sample\",20702725,20844544,2026/09/27 11:53:56,0,65,57
```

| Column | Meaning |
|---|---|
| name | Full path; **folders end with a separator** (`C:\Users\`) |
| size | Logical bytes (folders: subtree total) |
| allocated | On-disk bytes; a **leading `0` marks a hard link** |
| modified | `yyyy/mm/dd HH:mm:ss` |
| attributes | Bitmask: 1 R, 2 H, 4 S, **16 = directory**, 32 A, 2048 C |
| files / folders | Child counts (folders only) |
| (extras) | `DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE` when capacity columns are on |

Two notes on the attributes column, both from inspecting real exports rather than
the vendor docs: the vendor list omits `16` (`FILE_ATTRIBUTE_DIRECTORY`), which
every folder row carries, and the leading-`0` hard-link marker could not be
reproduced here because the test volume had no hard links.

### 1.4 Three measured behaviours that dictate the design

Reconnaissance ran the real binary; these are measurements, not documentation readings.

| # | Finding | Evidence | Design consequence |
|---|---|---|---|
| **F1** | Exit code is always `0`, including failure | `Z:\nope` → `ExitCode=0`, no file written | `_run_and_validate` deletes the stale artifact, then requires a non-empty file; nothing trusts the exit status |
| **F2** | CSV header is **localized** | Chinese system emitted `文件名称,大小,分配,…` | `core/index.py` reads positionally; `csv.DictReader` is unusable |
| **F3** | Column set is **not stable** | Without `/exportdrivecapacity=0` the header had 11 columns and the first record 11 fields while the rest had 7 | `build_export_args` emits every advanced switch explicitly, so the schema is a pure function of the arguments |
| **F4** | The 32-bit build is a **launcher that returns immediately** | `WizTree.exe … /export=…` exits 0 in 0.36 s with no file yet; the CSV appears ~2 s later. The vendor guide documents the auto-launch into `wiztree64.exe` | Discovery offers `WizTree64.exe` first and inserts the 64-bit sibling; the driver additionally waits (grace period) for the artifact after the process returns |
| **F5** | An **unwritable destination produces a modal dialog** and the process never returns | `-o C:\Windows\…` as a non-admin: WizTree still alive after 45 s, no exit code. With stdio on DEVNULL that is a silent 30-minute hang | The output directory is probed for writability **before** launching; the failure is immediate and names the path |

F4 and F5 were both found by an independent adversarial review rather than by
reading the documentation, and both had been shipping as silent misbehaviour: F4
reported a *false failure* while the export was still in flight, F5 reported
*nothing at all* for half an hour.

A fourth, smaller finding: **`/maxdepth` does not exist** — the real switch is
`/exportmaxdepth=n`. Online parameter tables that show `/drive:C:` / `/maxdepth:` are
wrong (one such table is indexed at `m.guofenmi.net/wz/484082.html`; its page is
firewalled, so the quote comes from a search-index snapshot) and were discarded in
favour of the vendor guide plus binary inspection.

### 1.5 Vendor defaults this harness deliberately mirrors

Where WizTree has a documented default, the harness passes the same value rather
than inventing its own, so "omit the option here" means the same thing as "omit it
in WizTree":

| Switch | Vendor default | Harness default |
|---|---|---|
| `/treemapimagewidth` | 1920 | 1920 |
| `/treemapimageheight` | 1080 | 1080 |
| `/treemapimagegray` | 0 | 0 |
| `/treemapimagefreespace` | 1 | 1 |
| `/treemapimageshowallocated` | 0 | 0 |
| `/sortbyfiletypes` | 1 (size desc) | 1 |
| `/filterfullpath` | 1 (full path) | 1 |

An earlier revision defaulted the treemap to 1024x768; that was an undocumented
divergence from the vendor and was corrected, because silently resizing an export
is the kind of difference nobody notices until the image looks wrong.

---

## 2. Command map

| GUI action | Backing mechanism | CLI command |
|---|---|---|
| Select drive → Scan | positional target + `/export` | `scan run <target>` |
| Ctrl+Alt+E (Export to CSV) | `/export="file"` | `scan run -o file.csv` |
| Include / exclude filter box | `/filter`, `/filterexclude`, `/filterfullpath` | `--filter`, `--exclude`, `--name-only` |
| Sort by size / allocated / modified | `/sortby=0..3` | `--sort-by` |
| File View ↔ Tree View | `/exportfiles`, `/exportfolders` | `--files-only`, `--folders-only` |
| "Run as admin" setting | `/admin` | `--admin` |
| File-type summary view | `/exportfiletypes` | `scan filetypes` |
| Treemap pane | `/treemapimage*` | `scan treemap` |
| MFT dump | `/dumpmftfile` | `scan mft` (no elevation needed) |
| File search filter (name/size/date) | *(no CLI switch)* | `query find` — re-implemented over the exported index |
| Duplicate detection | *(no CLI switch)* | `query dups` — same name+size rule, recomputed locally |
| "Max files to display" limit | *(GUI-only)* | `query find --limit` |
| Scheduled scans | Windows Task Scheduler | out of scope — see gaps |

---

## 3. Rendering gap assessment

WizTree's rendering pipeline is the CSV writer plus the treemap renderer. Both are
reachable, so **nothing required GUI automation**. What is genuinely missing:

1. **Duplicate export.** The GUI detects duplicates but offers no export switch for
   them. The harness recomputes groups from the index using WizTree's own rule
   (base name + size) and states loudly in every output that contents are not
   compared. This is an approximation of the GUI feature, not a reproduction.
2. **Search filters.** The GUI's rich search language (wildcards, `=folder`,
   `namelen>200`, `>=today-7`, regex) is applied *inside* WizTree and has no export
   equivalent. `query find` implements glob + extension + size range over the index
   only. Date and regex filters are **not** implemented — a documented gap, not a
   silent one.
3. **`percent-of-parent` / `split filename` columns** are exposed as switches but not
   surfaced as first-class CLI options beyond `--split-name`; they land in
   `entry.extra` when enabled.
4. **GUI-only niceties** (colour schemes, saved scan history, clipboard copy,
   supporter-code entry) are intentionally out of scope.
5. **macOS.** WizTreeMac exists and uses `--paramname` instead of `/paramname`. This
   harness targets the Windows build; a macOS port would need a second arg builder.

---

## 4. Backend module contract

`utils/wiztree_backend.py` is the only module that knows the vendor's interface:

- `discover()` / `find_wiztree()` — resolution order: `--exe` → `$WIZTREE_EXE` →
  remembered config → **Windows uninstall registry** → `PATH` →
  environment-derived install directories → user `search_paths`. No
  machine-specific literal appears in the source; on the test machine the registry
  supplied the location (`WizTree_is1` → `InstallLocation = D:\software\WizTree\`).
- `remember_exe()` — caches a discovered path and is called **only** by the CLI's
  `info` command. `discover()` never writes configuration, because a lookup that
  mutates state is non-idempotent and lets one bad result poison every later run
  (that failure was observed and is now a regression test).
- `build_export_args()` / `build_filetypes_args()` / `build_treemap_args()` / `build_mft_args()` — pure, fully unit-tested argument builders that pin the schema
- `run_export()` / `export_filetypes()` / `export_treemap()` / `dump_mft()` — execute and **validate the artifact**
- `probe()` — installation, version, admin capability, and which discovery route won

The rest of the harness (`core/index.py`, `core/query.py`, `core/project.py`,
`core/export.py`) never mentions WizTree's switches, so a future macOS or
command-line-only variant replaces one module, not the CLI.
