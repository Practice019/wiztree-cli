#!/usr/bin/env python3
"""WizTree CLI — drive the closed-source WizTree disk analyzer from a terminal.

WizTree is a Windows GUI application, but it ships a documented command-line
export interface.  This CLI wraps that interface and adds the layer the GUI
never had: a parsed, queryable index of a scan.

Usage:
    # What is on this machine?
    cli-anything-wiztree info --json

    # Scan and index a drive or folder
    cli-anything-wiztree scan run "C:" --json
    cli-anything-wiztree scan run "D:/projects" --filter "*.log" -o scan.csv

    # Query the most recent scan without touching the disk again
    cli-anything-wiztree query summary --json
    cli-anything-wiztree query top -n 20 --files
    cli-anything-wiztree query find "*.mp4" --min-size 100m
    cli-anything-wiztree query dups --min-size 1m
    cli-anything-wiztree query types

    # Reports and other WizTree export modes
    cli-anything-wiztree export report -o report.md --format markdown
    cli-anything-wiztree scan filetypes "C:" -o types.csv
    cli-anything-wiztree scan treemap "C:" -o c.png --width 1920
    cli-anything-wiztree scan mft "D:" -o d.mft     # needs an elevated shell

    # Interactive
    cli-anything-wiztree

Environment:
    WIZTREE_EXE                   Path to WizTree64.exe (overrides discovery)
    CLI_ANYTHING_WIZTREE_HOME     State directory (default ~/.cli-anything-wiztree)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Optional

import click

# Make `python cli_anything/wiztree/wiztree_cli.py` work from a bare checkout by
# putting the harness root (the parent of `cli_anything/`) on the path.  For the
# installed console script this is redundant — PEP 660 handles resolution — but a
# wrong depth here would be worse than none at all.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cli_anything.wiztree import __version__  # noqa: E402
from cli_anything.wiztree.core import export as export_mod  # noqa: E402
from cli_anything.wiztree.core import index as index_mod, query as query_mod  # noqa: E402
from cli_anything.wiztree.core.project import Project  # noqa: E402
from cli_anything.wiztree.core.session import Session  # noqa: E402
from cli_anything.wiztree.utils import wiztree_backend as backend  # noqa: E402

HOME_ENV = backend.HOME_ENV


# ── global state ──────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "json": False,
    "exe": None,
    "admin": False,
    "project_path": None,
    "repl": False,
    "session": None,
}


def state_home() -> str:
    """Directory holding this CLI's state (overridable for tests).

    Defined once in the backend so the CLI and the executable-discovery cache
    can never disagree about where configuration lives.
    """
    return backend.state_home()


def scans_dir() -> str:
    """Directory holding generated CSV exports."""
    return os.path.join(state_home(), "scans")


def default_project_path() -> str:
    """Path of the project file that tracks the most recent scan."""
    return os.path.join(state_home(), "default.json")


def get_session() -> Session:
    """The process-wide session (history, undo/redo)."""
    if _state["session"] is None:
        _state["session"] = Session(
            session_file=os.path.join(state_home(), "session.json")
        )
    return _state["session"]


def output(data: Any, message: str = "") -> None:
    """Emit a result as JSON or as human-readable text."""
    if _state["json"]:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        if message:
            click.echo(message)
        _print_human(data)


def _print_human(data: Any, indent: int = 0) -> None:
    prefix = "  " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                click.echo(f"{prefix}{key}:")
                _print_human(value, indent + 1)
            elif isinstance(value, bool):
                click.echo(f"{prefix}{key}: {'true' if value else 'false'}")
            elif isinstance(value, float):
                click.echo(f"{prefix}{key}: {value:,.2f}")
            elif isinstance(value, int):
                click.echo(f"{prefix}{key}: {value:,}")
            else:
                click.echo(f"{prefix}{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                click.echo(prefix + "- " + ", ".join(f"{k}={v}" for k, v in item.items()))
            else:
                click.echo(f"{prefix}- {item}")
    elif data not in (None, ""):
        click.echo(f"{prefix}{data}")


def handle_error(func):
    """Turn known failures into clean exit-1 errors (never a traceback)."""

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (backend.WizTreeNotFound, backend.WizTreeExportError) as exc:
            _fail(str(exc), type(exc).__name__)
        except (FileNotFoundError, ValueError, KeyError, RuntimeError, OSError,
                OverflowError, AttributeError, TypeError) as exc:
            _fail(str(exc), type(exc).__name__)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _apply_globals(use_json, project_path, exe_path, admin) -> None:
    """Fold subcommand-level global flags into the shared state.

    Only flags that were actually supplied are applied, so a flag written after
    the verb overrides the group-level value instead of resetting it.
    """
    if use_json:
        _state["json"] = True
    if project_path:
        _state["project_path"] = project_path
    if exe_path:
        _state["exe"] = exe_path
    if admin is not None:
        _state["admin"] = bool(admin)


def with_globals(func):
    """Let the global flags be written *after* the subcommand as well.

    ``cli-anything-wiztree info --json`` is what humans and agents type, but
    Click only accepts group options before the verb, so that form used to fail
    with ``exit 2: No such option: --json``.  This decorator registers the same
    flags on the subcommand and folds them into the shared state, so both
    ``--json info`` and ``info --json`` work and mean the same thing.
    """
    func = click.option("--json", "use_json", is_flag=True, default=None,
                        help="Machine-readable JSON output.")(func)
    func = click.option("--project", "project_path", type=click.Path(), default=None,
                        help="Project file to read/write.")(func)
    func = click.option("--exe", "exe_path", type=click.Path(), default=None,
                        help="Path to WizTree64.exe.")(func)
    func = click.option("--admin/--no-admin", "admin", default=None,
                        help="Request WizTree's elevated MFT scan.")(func)

    def wrapper(*args, **kwargs):
        _apply_globals(
            kwargs.pop("use_json", None),
            kwargs.pop("project_path", None),
            kwargs.pop("exe_path", None),
            kwargs.pop("admin", None),
        )
        return func(*args, **kwargs)

    wrapper.__name__ = getattr(func, "__name__", "command")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    # Fresh list so the outer @click.option/@click.argument decorators append to
    # this wrapper's params rather than mutating a shared one.
    wrapper.__click_params__ = list(getattr(func, "__click_params__", []))
    return wrapper


def _fail(message: str, kind: str) -> None:
    if _state["json"]:
        click.echo(json.dumps({"error": message, "type": kind}, ensure_ascii=False))
    else:
        click.echo(f"Error: {message}", err=True)
    if not _state["repl"]:
        sys.exit(1)


# ── scan helpers ──────────────────────────────────────────────────────

def _slug(text: str) -> str:
    cleaned = "".join(c if c.isalnum() else "_" for c in text)
    return cleaned.strip("_")[:40] or "scan"


def _resolve_project(create: bool = False) -> Project:
    """The project backing query commands."""
    path = _state["project_path"] or default_project_path()
    if os.path.exists(path):
        return Project.open(path)
    if create:
        return Project.create(path, root="")
    raise FileNotFoundError(
        f"No project at {path} — run `scan run <target>` first, or pass --project."
    )


def _load_last_entries() -> list[index_mod.WizTreeEntry]:
    """Entries from the active project's most recent scan."""
    project = _resolve_project()
    return project.entries()


def _do_scan(
    target: str,
    destination: str | None,
    *,
    allow_empty: bool = False,
    **opts: Any,
) -> dict[str, Any]:
    """Run a WizTree export, index it, and record it in the active project.

    Args:
        target: Drive or folder to scan.
        destination: CSV path, or ``None`` for a timestamped file under the state dir.
        allow_empty: Accept an export that yielded zero entries.  Without this, an
            empty result is an error: WizTree signals "nothing matched" by writing
            a banner and header with no data rows, which is byte-for-byte what a
            silent no-op looks like.  Defaulting to failure means an agent never
            reports "✓ Scanned" over a scan that did not happen.
        **opts: Forwarded to the backend.

    Returns:
        A result dict ready for ``--json``.
    """
    if not destination:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = os.path.join(scans_dir(), f"{_slug(target)}_{stamp}.csv")
    # Directory creation and writability are the backend's pre-flight job, so a
    # bad destination produces one clear message on one code path instead of a
    # raw, localized OSError raised from here.

    started = time.time()
    result = backend.run_export(target, destination, exe=_state["exe"], **opts)
    entries, meta = index_mod.parse_export_csv(result["csv_path"])
    stats = query_mod.summary(entries)

    warnings: list[str] = []
    if meta.get("skipped_rows"):
        warnings.append(
            f"{len(meta['skipped_rows'])} row(s) were dropped: the byte-size field was "
            f"empty or non-numeric (body rows: {meta.get('body_rows')}). Totals are "
            "understated."
        )
    if meta.get("encoding_fallback"):
        warnings.append(
            f"the export decoded by fallback ({meta.get('encoding')}); text may be wrong"
        )

    if not entries and not allow_empty:
        raise FileNotFoundError(
            f"Scan of {target} produced no entries — WizTree wrote a banner and header but "
            "no data rows.\n"
            "That is what a silent WizTree failure looks like, and also what a filter that "
            "matched nothing looks like, and the two are indistinguishable here.\n"
            f"  CSV: {result['csv_path']} ({result['size']:,} bytes, kept for inspection)\n"
            "If an empty result is genuinely expected, re-run with --allow-empty."
        )

    project = _resolve_project(create=True)
    project.attach_scan(result["csv_path"], entries=entries, meta=meta, root=target)

    payload: dict[str, Any] = {
        "target": target,
        "csv_path": result["csv_path"],
        "artifact_size": result["size"],
        "elapsed_ms": result["elapsed_ms"],
        "entry_count": len(entries),
        "stats": stats,
        "columns": meta.get("columns"),
        "encoding": meta.get("encoding"),
        "banner": meta.get("banner"),
        "skipped_rows": meta.get("skipped_rows") or [],
        "exe_is_launcher_only": result.get("exe_is_launcher_only", False),
        "wait_for_artifact_ms": result.get("waited_ms", 0),
        "warnings": warnings,
        "project": project.path,
        "total_ms": int((time.time() - started) * 1000),
    }
    get_session().record("scan run", {"target": target, "csv": result["csv_path"]}, payload)
    return payload


# ── main group ────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Machine-readable JSON output.")
@click.option("--project", "project_path", type=click.Path(), default=None,
              help="Project file to read/write instead of the default one.")
@click.option("--exe", "exe_path", type=click.Path(), default=None,
              help="Path to WizTree64.exe (overrides auto-discovery).")
@click.option("--admin/--no-admin", default=False,
              help="Request WizTree's elevated MFT scan (triggers UAC).")
@click.option("--version", "show_version", is_flag=True, help="Show version and exit.")
@click.pass_context
def cli(ctx, use_json, project_path, exe_path, admin, show_version):
    """WizTree CLI — scan disks and query the results like a database."""
    _state["json"] = use_json
    _state["exe"] = exe_path
    _state["admin"] = admin
    _state["project_path"] = project_path
    ctx.ensure_object(dict)
    if show_version:
        click.echo(f"cli-anything-wiztree {__version__}")
        return
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


# ── system ────────────────────────────────────────────────────────────

@cli.command("info")
@with_globals
@handle_error
def info_cmd():
    """Report whether WizTree is installed and what it can do here."""
    report = backend.probe(_state["exe"])
    report["cli_version"] = __version__
    report["state_home"] = state_home()
    report["scans_dir"] = scans_dir()
    if report.get("found"):
        # Persist discovery only here, after the user has seen the result.
        report["remembered"] = backend.remember_exe(report["exe"])
        output(report, f"✓ WizTree {report.get('version')} at {report['exe']} "
                       f"(found via {report.get('source')})")
    else:
        output(report, f"✗ WizTree not found.\n{report.get('error', '')}")
        if not _state["json"]:
            sys.exit(1)


@cli.command("doctor", hidden=True)
@with_globals
@handle_error
def doctor_cmd():
    """Alias of `info` — kept for parity with other harnesses."""
    info_cmd.callback()


# ── scan ──────────────────────────────────────────────────────────────

@cli.group()
def scan():
    """Run WizTree scans and exports."""


@scan.command("run")
@click.argument("target")
@click.option("--output", "-o", "destination", type=click.Path(), default=None,
              help="CSV destination (default: <home>/scans/<target>_<timestamp>.csv).")
@click.option("--filter", "filter_spec", default=None,
              help='Include filter, e.g. "*.mp3|*.wav" (| separates alternatives).')
@click.option("--exclude", "exclude_spec", default=None, help="Exclude filter.")
@click.option("--name-only", is_flag=True,
              help="Match filters against the file name only, not the full path.")
@click.option("--files-only", is_flag=True, help="Export files, not folders.")
@click.option("--folders-only", is_flag=True, help="Export folders, not files.")
@click.option("--sort-by", type=click.Choice(["name", "size", "allocated", "modified"]),
              default="name", show_default=True, help="WizTree export ordering.")
@click.option("--max-depth", type=int, default=0, show_default=True,
              help="Limit exported folder depth (0 = unlimited).")
@click.option("--mft-recno", is_flag=True, help="Include NTFS MFT record numbers.")
@click.option("--utc", is_flag=True, help="Export UTC timestamps.")
@click.option("--all-dates", is_flag=True, help="Include created/accessed dates.")
@click.option("--split-name", is_flag=True,
              help="Split root/folder/name/extension into separate columns.")
@click.option("--drive-capacity", is_flag=True,
              help="Add drive capacity columns (off by default for a stable schema).")
@click.option("--timeout", type=int, default=1800, show_default=True,
              help="Seconds before the scan is abandoned.")
@click.option("--allow-empty", is_flag=True,
              help="Accept an export with zero entries (a filter that matched nothing).")
@with_globals
@handle_error
def scan_run(target, destination, filter_spec, exclude_spec, name_only, files_only,
             folders_only, sort_by, max_depth, mft_recno, utc, all_dates, split_name,
             drive_capacity, timeout, allow_empty):
    """Scan TARGET (a drive like "C:" or a folder) and index the result."""
    sort_map = {"name": 0, "size": 1, "allocated": 2, "modified": 3}
    payload = _do_scan(
        target, destination,
        allow_empty=allow_empty,
        filter=filter_spec,
        filterexclude=exclude_spec,
        filterfullpath=not name_only,
        admin=_state["admin"],
        exportfolders=not files_only,
        exportfiles=not folders_only,
        sortby=sort_map[sort_by],
        max_depth=max_depth,
        exportmftrecno=mft_recno,
        utc_time=utc,
        all_dates=all_dates,
        split_filename=split_name,
        drive_capacity=drive_capacity,
        timeout=timeout,
    )
    stats = payload["stats"]
    note = ""
    if payload["warnings"]:
        note = "\n  ⚠ " + "\n  ⚠ ".join(payload["warnings"])
    output(
        payload,
        f"✓ Scanned {target}: {stats['file_count']:,} files, "
        f"{stats['folder_count']:,} folders, {stats['total_file_size_human']} total "
        f"in {payload['elapsed_ms']:,} ms\n  CSV: {payload['csv_path']}{note}",
    )


@scan.command("filetypes")
@click.argument("target")
@click.option("--output", "-o", "destination", type=click.Path(), required=True,
              help="Destination CSV for the per-extension report.")
@click.option("--filter", "filter_spec", default=None, help="Include filter.")
@click.option("--exclude", "exclude_spec", default=None, help="Exclude filter.")
@click.option("--top", "-n", type=int, default=20, show_default=True,
              help="How many extensions to display (0 = all).")
@click.option("--timeout", type=int, default=1800, show_default=True)
@with_globals
@handle_error
def scan_filetypes(target, destination, filter_spec, exclude_spec, top, timeout):
    """Export per-extension totals (WizTree's /exportfiletypes)."""
    result = backend.export_filetypes(
        target, destination, exe=_state["exe"], admin=_state["admin"],
        filter=filter_spec, filterexclude=exclude_spec, timeout=timeout,
    )
    rows, meta = index_mod.parse_filetypes_csv(result["csv_path"])
    payload = {
        "target": target,
        "csv_path": result["csv_path"],
        "elapsed_ms": result["elapsed_ms"],
        "row_count": len(rows),
        "banner": meta.get("banner"),
        "types": rows[: top or len(rows)],
    }
    get_session().record("scan filetypes", {"target": target}, payload)
    output(payload, f"✓ {len(rows):,} file types → {result['csv_path']}")


@scan.command("treemap")
@click.argument("target")
@click.option("--output", "-o", "destination", type=click.Path(), required=True,
              help="Destination PNG.")
@click.option("--width", type=int, default=1920, show_default=True)
@click.option("--height", type=int, default=1080, show_default=True)
@click.option("--gray", is_flag=True, help="Render in greyscale.")
@click.option("--no-free-space", is_flag=True, help="Hide free space.")
@click.option("--show-allocated", is_flag=True, help="Size by allocated space.")
@click.option("--timeout", type=int, default=1800, show_default=True)
@with_globals
@handle_error
def scan_treemap(target, destination, width, height, gray, no_free_space,
                 show_allocated, timeout):
    """Export a treemap PNG image of TARGET."""
    result = backend.export_treemap(
        target, destination, exe=_state["exe"], admin=_state["admin"],
        width=width, height=height, gray=gray,
        free_space=not no_free_space, show_allocated=show_allocated, timeout=timeout,
    )
    verified = export_mod.verify_file(result["image_path"])
    payload = {**result, "verified": verified}
    get_session().record("scan treemap", {"target": target}, payload)
    output(payload, f"✓ Treemap → {result['image_path']} ({result['size']:,} bytes)")


@scan.command("mft")
@click.argument("drive")
@click.option("--output", "-o", "destination", type=click.Path(), required=True,
              help="Destination file for the raw $MFT dump.")
@click.option("--timeout", type=int, default=1800, show_default=True)
@with_globals
@handle_error
def scan_mft(drive, destination, timeout):
    """Dump the raw NTFS $MFT of DRIVE (no elevation needed; the dump is the whole MFT)."""
    result = backend.dump_mft(drive, destination, exe=_state["exe"], timeout=timeout)
    payload = {**result, "verified": export_mod.verify_file(result["mft_path"])}
    get_session().record("scan mft", {"drive": drive}, payload)
    output(payload, f"✓ MFT → {result['mft_path']} ({result['size']:,} bytes)")


# ── query ─────────────────────────────────────────────────────────────

@cli.group()
def query():
    """Query the indexed results of the most recent scan."""


@query.command("summary")
@with_globals
@handle_error
def query_summary():
    """Aggregate totals for the active scan."""
    entries = _load_last_entries()
    project = _resolve_project()
    stats = query_mod.summary(entries)
    payload = {
        "project": project.path,
        "root": project.data.get("root", ""),
        "source_csv": project.data.get("source_csv", ""),
        **stats,
    }
    output(payload, f"{stats['entry_count']:,} entries from {project.data.get('root', '?')}")


@query.command("top")
@click.option("--count", "-n", type=int, default=20, show_default=True)
@click.option("--files", "files_only", is_flag=True, help="Files only.")
@click.option("--folders", "folders_only", is_flag=True, help="Folders only.")
@click.option("--by", type=click.Choice(["size", "allocated"]), default="size",
              show_default=True)
@with_globals
@handle_error
def query_top(count, files_only, folders_only, by):
    """Largest entries in the active scan."""
    entries = query_mod.top_entries(
        _load_last_entries(), n=count, by=by,
        files_only=files_only, folders_only=folders_only,
    )
    payload = [
        {**e.to_dict(), "size_human": query_mod.human_size(e.size)} for e in entries
    ]
    output(payload, f"Top {len(payload)} by {by}:")


@query.command("find")
@click.argument("pattern", default="*")
@click.option("--ext", default=None, help="Restrict to one extension (e.g. mp4).")
@click.option("--min-size", default=None, help="Minimum size (10, 500k, 1.5g …).")
@click.option("--max-size", default=None, help="Maximum size.")
@click.option("--files-only", is_flag=True, help="Files only.")
@click.option("--folders-only", is_flag=True, help="Folders only.")
@click.option("--limit", "-n", type=int, default=100, show_default=True)
@with_globals
@handle_error
def query_find(pattern, ext, min_size, max_size, files_only, folders_only, limit):
    """Find entries by glob PATTERN (matches the full path)."""
    min_bytes = query_mod.parse_size(min_size)
    max_bytes = query_mod.parse_size(max_size)
    hits = query_mod.find_entries(
        _load_last_entries(), pattern, ext=ext,
        min_size=min_bytes, max_size=max_bytes,
        files_only=files_only, folders_only=folders_only, limit=limit,
    )
    payload = [{**e.to_dict(), "size_human": query_mod.human_size(e.size)} for e in hits]
    output(payload, f"{len(payload)} match(es) for {pattern!r}")


@query.command("dups")
@click.option("--min-size", default="1m", show_default=True,
              help="Ignore files smaller than this.")
@click.option("--limit", "-n", type=int, default=20, show_default=True,
              help="How many duplicate groups to show.")
@with_globals
@handle_error
def query_dups(min_size, limit):
    """Find duplicate files (same name and size — WizTree's own rule)."""
    groups = query_mod.duplicates(_load_last_entries(), min_size=query_mod.parse_size(min_size) or 1)
    wasted = query_mod.duplicates_wasted(groups)
    payload = {
        "group_count": len(groups),
        "wasted_bytes": wasted,
        "wasted_human": query_mod.human_size(wasted),
        "note": "Matched on name + size, not contents — verify before deleting.",
        "groups": [
            [
                {**e.to_dict(), "size_human": query_mod.human_size(e.size)} for e in group
            ]
            for group in groups[:limit]
        ],
    }
    output(
        payload,
        f"{len(groups):,} duplicate group(s), {query_mod.human_size(wasted)} reclaimable "
        f"(name+size match only)",
    )


@query.command("types")
@click.option("--count", "-n", type=int, default=20, show_default=True)
@with_globals
@handle_error
def query_types(count):
    """File-type breakdown of the active scan."""
    rows = query_mod.ext_breakdown(_load_last_entries(), n=count)
    output(rows, f"Top {len(rows)} extensions:")


# ── project ───────────────────────────────────────────────────────────

@cli.group()
def project():
    """Manage scan projects (saved snapshots with export profiles)."""


@project.command("new")
@click.argument("root")
@click.option("--output", "-o", "path", type=click.Path(), default=None,
              help="Project file path (default: <home>/default.json).")
@with_globals
@handle_error
def project_new(root, path):
    """Create an empty project for ROOT."""
    target = path or default_project_path()
    created = Project.create(target, root=root)
    output(created.info(), f"✓ Project created: {created.path}")


@project.command("info")
@with_globals
@handle_error
def project_info():
    """Show the active project."""
    active = _resolve_project()
    payload = active.info()
    payload["scan_meta"] = active.scan_meta()
    output(payload, f"Project {active.path}")


@project.command("profiles")
@with_globals
@handle_error
def project_profiles():
    """List export profiles in the active project."""
    active = _resolve_project()
    payload = {name: active.get_profile(name) for name in active.list_profiles()}
    output(payload, f"{len(payload)} profile(s)")


@project.command("profile-add")
@click.argument("name")
@click.option("--filter", "filter_spec", default=None)
@click.option("--sort-by", type=click.Choice(["name", "size", "allocated", "modified"]),
              default="size")
@click.option("--files-only", is_flag=True)
@with_globals
@handle_error
def project_profile_add(name, filter_spec, sort_by, files_only):
    """Add or replace an export profile."""
    sort_map = {"name": 0, "size": 1, "allocated": 2, "modified": 3}
    active = _resolve_project(create=True)
    active.add_profile(name, {
        "filter": filter_spec,
        "sortby": sort_map[sort_by],
        "exportfolders": not files_only,
    })
    output({"name": name, **active.get_profile(name)}, f"✓ Profile saved: {name}")


@project.command("profile-remove")
@click.argument("name")
@with_globals
@handle_error
def project_profile_remove(name):
    """Delete an export profile."""
    active = _resolve_project()
    active.remove_profile(name)
    output({"removed": name}, f"✓ Profile removed: {name}")


# ── export ────────────────────────────────────────────────────────────

@cli.group(name="export")
def export_group():
    """Turn the active scan into reports."""


@export_group.command("report")
@click.option("--output", "-o", "destination", type=click.Path(), required=True,
              help="Destination file (.csv .json .md .html).")
@click.option("--format", "fmt", default=None,
              help="csv / json / table / markdown / html (default: from extension).")
@click.option("--top", "-n", type=int, default=None, help="Only the first N entries.")
@click.option("--files-only", is_flag=True)
@click.option("--preset", type=click.Choice(sorted(export_mod.EXPORT_PRESETS)),
              default=None, help="Use a named preset's columns.")
@with_globals
@handle_error
def export_report(destination, fmt, top, files_only, preset):
    """Write a report over the active scan."""
    entries = _load_last_entries()
    if files_only:
        entries = [e for e in entries if not e.is_folder]
    if preset:
        entries = query_mod.top_entries(entries, n=top or len(entries), by="size")
    columns = export_mod.EXPORT_PRESETS[preset]["columns"] if preset else None
    result = export_mod.write_export(entries, destination, fmt=fmt, columns=columns, top=top)
    result["verified"] = export_mod.verify_file(result["path"])
    get_session().record("export report", {"path": destination}, result)
    output(result, f"✓ {result['rows']:,} rows → {result['path']} ({result['size']:,} bytes)")


@export_group.command("presets")
@with_globals
@handle_error
def export_presets():
    """List the built-in export presets."""
    output(export_mod.EXPORT_PRESETS, f"{len(export_mod.EXPORT_PRESETS)} preset(s)")


# ── session ───────────────────────────────────────────────────────────

@cli.group()
def session():
    """Inspect command history and undo/redo."""


@session.command("status")
@with_globals
@handle_error
def session_status():
    """Session counters."""
    output(get_session().status())


@session.command("history")
@click.option("--limit", "-n", type=int, default=20, show_default=True)
@with_globals
@handle_error
def session_history(limit):
    """Recently recorded operations."""
    entries = get_session().history(limit=limit)
    output(entries, f"{len(entries)} entr(ies)")


@session.command("undo")
@with_globals
@handle_error
def session_undo():
    """Undo the most recent recorded operation."""
    entry = get_session().undo()
    if entry:
        output(entry.to_dict(), f"✓ Undone: {entry.command}")
    else:
        output({"error": "Nothing to undo"}, "Nothing to undo")


@session.command("redo")
@with_globals
@handle_error
def session_redo():
    """Redo the most recently undone operation."""
    entry = get_session().redo()
    if entry:
        output(entry.to_dict(), f"✓ Redone: {entry.command}")
    else:
        output({"error": "Nothing to redo"}, "Nothing to redo")


# ── REPL ──────────────────────────────────────────────────────────────

@cli.command("repl", hidden=True)
def repl():
    """Enter the interactive REPL."""
    _state["repl"] = True
    from cli_anything.wiztree.utils.repl_skin import ReplSkin

    skin = ReplSkin("wiztree", version=__version__)
    skin.print_banner()

    commands = {
        "info": "Is WizTree installed and usable?",
        "scan run <target>": 'Scan a drive or folder, e.g. scan run "C:"',
        "scan filetypes <target> -o f.csv": "Per-extension totals",
        "scan treemap <target> -o f.png": "Treemap image",
        "scan mft <drive> -o f.mft": "Raw NTFS $MFT dump (needs elevation)",
        "query summary": "Totals for the active scan",
        "query top -n 20 [--files]": "Largest entries",
        'query find "*.mp4" --min-size 100m': "Glob + size search",
        "query dups --min-size 1m": "Duplicate candidates",
        "query types": "Extension breakdown",
        "export report -o report.md": "Write a report",
        "export presets": "List export presets",
        "project info": "Active project",
        "session history|undo|redo": "History controls",
        "help": "Show this help",
        "quit / exit": "Leave the REPL",
    }

    try:
        prompt_session = skin.create_prompt_session()
    except Exception:
        prompt_session = None

    while True:
        try:
            line = skin.get_input(prompt_session, project_name="wiztree")
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            break
        if not line:
            continue
        if line.strip() in ("quit", "exit", "q"):
            skin.print_goodbye()
            break
        if line.strip() == "help":
            skin.help(commands)
            continue
        parts = line.split()
        try:
            cli.main(parts, standalone_mode=False)
        except SystemExit:
            pass
        except click.exceptions.UsageError as exc:
            skin.error(str(exc))
        except click.ClickException as exc:
            skin.error(exc.format_message())
        except Exception as exc:  # keep the REPL alive on any failure
            skin.error(f"{type(exc).__name__}: {exc}")


def _wants_json() -> bool:
    """True when JSON was requested, including as a trailing subcommand flag."""
    return bool(_state["json"]) or "--json" in sys.argv[1:]


def main() -> None:
    """Console-script entry point.

    ``windows_expand_args=False`` is essential on Windows: Click otherwise
    glob-expands ``sys.argv`` before dispatch, which silently rewrites arguments
    that are *data*, not paths.  ``query find "*.mp4"`` used to be expanded into
    the .mp4 files of the current directory — rc 2 with two matches, and a
    silently different pattern with one.  Discovery and filters pass wildcards
    through constantly, so expansion is off for the whole command tree.

    ``standalone_mode=False`` lets usage errors (unknown option, missing
    argument, bad choice) be reported through the same channel as every other
    failure, so ``--json`` never returns empty stdout with a human message on
    stderr.
    """
    try:
        cli.main(windows_expand_args=False, standalone_mode=False)
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        sys.exit(1)
    except click.ClickException as exc:
        if _wants_json():
            click.echo(json.dumps(
                {"error": exc.format_message(), "type": type(exc).__name__},
                ensure_ascii=False,
            ))
        else:
            exc.show()
        sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
