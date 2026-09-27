"""WizTree backend — drives ``WizTree64.exe`` and validates what it produced.

WizTree is closed-source GUI software, so there is no library to import.  It
does, however, ship a documented command-line export interface::

    WizTree64.exe "drive/folder" /export="filename" [/filter="filespec"] ...

This module is the only place that knows about that interface.  Three
behaviours were measured on the test machine (WizTree 4.27, non-admin session)
and shape every function below:

**F1 — the exit code is always 0.**  Pointing WizTree at ``Z:\\nope`` returned
``ExitCode=0`` and wrote no file.  Exit status therefore carries no signal:
:func:`run_export` and friends delete any stale output first, then *require* a
non-empty artifact and raise :class:`WizTreeExportError` otherwise.

**F2 — the CSV header is localized.**  Reading the file back is
:mod:`cli_anything.wiztree.core.index`'s job, and it reads positionally.

**F3 — the column set varies.**  Without the advanced switches WizTree appends
``DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE`` to the header and the first
record, so the same scan parses differently on different machines.
:func:`build_export_args` pins every one of them (``=0`` unless asked for) so a
scan's schema is a function of the arguments and nothing else.

Note the real switch for depth limiting is ``/exportmaxdepth``; ``/maxdepth``
(which circulates online) is not a WizTree parameter at all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Iterator, Sequence

__all__ = [
    "WizTreeNotFound",
    "WizTreeExportError",
    "DEFAULT_SEARCH_PATHS",
    "DOWNLOAD_URL",
    "ENV_VAR",
    "HOME_ENV",
    "find_wiztree",
    "discover",
    "iter_candidates",
    "remember_exe",
    "is_launcher_only",
    "state_home",
    "config_path",
    "load_config",
    "save_config",
    "wiztree_version",
    "build_export_args",
    "build_filetypes_args",
    "build_treemap_args",
    "build_mft_args",
    "run_export",
    "export_filetypes",
    "export_treemap",
    "dump_mft",
    "probe",
]

#: Official download page, quoted in every "not installed" error.
DOWNLOAD_URL = "https://diskanalyzer.com/download"

#: Environment override, checked before any discovered location.
ENV_VAR = "WIZTREE_EXE"

#: State directory override (shared with the CLI layer).
HOME_ENV = "CLI_ANYTHING_WIZTREE_HOME"

#: Extra locations probed last.  **Deliberately empty**: a machine-specific
#: literal path in the source would violate the harness rule against hardcoded
#: paths.  Users extend the
#: search via ``search_paths`` in ``<state home>/config.json`` instead; the
#: Windows uninstall registry and the ``%ProgramFiles%``-style environment
#: variables cover the normal installs without baking anything in.
DEFAULT_SEARCH_PATHS: tuple[str, ...] = ()

#: Executable names looked for on ``PATH``.
EXE_NAMES: tuple[str, ...] = ("WizTree64.exe", "WizTree.exe", "wiztree64", "wiztree")

#: Registry location of WizTree's Inno Setup uninstall record.
_REGISTRY_UNINSTALL_SUBKEYS: tuple[str, ...] = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

#: Valid ``/sortby`` values.
SORT_BY_NAME, SORT_BY_SIZE, SORT_BY_ALLOCATED, SORT_BY_MODIFIED = 0, 1, 2, 3

#: A bare drive such as ``C:`` (drive-relative) versus a rooted ``C:\`` path.
_DRIVE_ONLY_RE = re.compile(r"^[A-Za-z]:$")

Runner = Callable[[Sequence[str], int], int]


class WizTreeNotFound(RuntimeError):
    """No ``WizTree64.exe`` could be located."""


class WizTreeExportError(RuntimeError):
    """WizTree ran but the requested artifact was not produced."""


# ── state / configuration ─────────────────────────────────────────────

def state_home() -> str:
    """Directory holding this CLI's state and configuration."""
    return (
        os.environ.get(HOME_ENV)
        or os.path.join(os.path.expanduser("~"), ".cli-anything-wiztree")
    )


def config_path() -> str:
    """Path of the CLI's configuration file."""
    return os.path.join(state_home(), "config.json")


def load_config() -> dict[str, Any]:
    """Load ``<state home>/config.json``; a missing or broken file reads as empty."""
    try:
        with open(config_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    """Persist configuration, creating the state directory when needed."""
    path = config_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False, sort_keys=True)


# ── locating the binary ───────────────────────────────────────────────

def _config_candidates() -> list[str]:
    """Paths remembered in config.json (the discovered exe, then search_paths)."""
    config = load_config()
    out: list[str] = []
    remembered = config.get("exe")
    if remembered:
        out.append(str(remembered))
    for extra in config.get("search_paths") or []:
        if extra:
            out.append(str(extra))
    return out


def _registry_candidates() -> list[str]:
    """Paths taken from WizTree's Windows uninstall record.

    Works for both installed and custom install locations, which is what makes
    a hardcoded default path unnecessary.
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []

    found: list[str] = []
    hives = ((winreg.HKEY_LOCAL_MACHINE,), (winreg.HKEY_CURRENT_USER,))
    for hive in hives:
        for subkey in _REGISTRY_UNINSTALL_SUBKEYS:
            try:
                with winreg.OpenKey(hive[0], subkey) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        try:
                            name = winreg.EnumKey(key, index)
                        except OSError:
                            continue
                        try:
                            with winreg.OpenKey(key, name) as entry:
                                try:
                                    display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                except OSError:
                                    continue
                                if "wiztree" not in display.lower():
                                    continue
                                for value_name in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        raw = str(winreg.QueryValueEx(entry, value_name)[0])
                                    except OSError:
                                        continue
                                    raw = raw.strip().strip('"')
                                    if raw.lower().endswith(".exe"):
                                        found.append(raw)
                                        found.append(
                                            os.path.join(os.path.dirname(raw), "WizTree64.exe")
                                        )
                                    elif raw:
                                        found.append(os.path.join(raw, "WizTree64.exe"))
                                        found.append(os.path.join(raw, "WizTree.exe"))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def _env_candidates() -> list[str]:
    """Standard install locations derived from the environment, never literals.

    A bare drive value such as ``SystemDrive=C:`` must be turned into a root
    (``C:\\``) before joining, otherwise ``os.path.join("C:", "WizTree", …)``
    yields the **drive-relative** path ``C:WizTree\\…``, which resolves against
    whatever directory the process happens to be sitting in on that drive.
    """
    out: list[str] = []
    seen: set[str] = set()
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA", "SystemDrive"):
        base = os.environ.get(variable)
        if not base:
            continue
        if _DRIVE_ONLY_RE.match(base):
            base = base + os.sep
        for candidate in (
            os.path.join(base, "WizTree", "WizTree64.exe"),
            os.path.join(base, "WizTree", "WizTree.exe"),
            os.path.join(base, "Programs", "WizTree", "WizTree64.exe"),
        ):
            if candidate not in seen:  # ProgramFiles == ProgramW6432 on 64-bit Windows
                seen.add(candidate)
                out.append(candidate)
    return out


def _path_candidates() -> list[str]:
    """Executables named WizTree that are already on ``PATH``."""
    found: list[str] = []
    for name in EXE_NAMES:
        located = shutil.which(name)
        if located:
            found.append(located)
    return found


def iter_candidates(explicit: str | None = None) -> Iterator[tuple[str, str]]:
    """Yield ``(path, source)`` discovery candidates in priority order.

    Sources, highest priority first: ``--exe``, ``$WIZTREE_EXE``, remembered
    config, the Windows uninstall registry, ``PATH``, environment-derived
    install directories, then user-configured ``search_paths``.

    The 64-bit binary is always offered before the 32-bit one, because the 32-bit
    build only *launches* the 64-bit build and returns immediately (see
    :func:`is_launcher_only`).
    """
    if explicit:
        yield explicit, "--exe"
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        yield from_env, f"${ENV_VAR}"
    for path in _config_candidates():
        yield path, "config"
    for path in _prefer_64_bit(_registry_candidates()):
        yield path, "registry"
    for path in _prefer_64_bit(_path_candidates()):
        yield path, "PATH"
    for path in _prefer_64_bit(_env_candidates()):
        yield path, "program-files"
    for path in _prefer_64_bit(list(DEFAULT_SEARCH_PATHS)):
        yield path, "search-paths"


def _prefer_64_bit(paths: Sequence[str]) -> list[str]:
    """Reorder candidates so 64-bit binaries precede 32-bit launchers.

    Also inserts the 64-bit sibling of any discovered 32-bit launcher, since the
    two always ship side by side.
    """
    sixty_four: list[str] = []
    launchers: list[str] = []
    for path in paths:
        name = os.path.basename(path).lower()
        if name in ("wiztree64.exe", "wiztree64"):
            sixty_four.append(path)
        else:
            launchers.append(path)
            if is_launcher_only(path):
                sibling = os.path.join(os.path.dirname(path), "WizTree64.exe")
                if os.path.exists(sibling):
                    sixty_four.append(sibling)
    ordered: list[str] = []
    for path in sixty_four + launchers:
        if path not in ordered:
            ordered.append(path)
    return ordered


def is_launcher_only(exe: str) -> bool:
    """True for the 32-bit build, which spawns the 64-bit one and returns.

    The vendor guide: "32 bit wiztree.exe will auto launch wiztree64.exe with the
    same parameters if it detects it's running on 64 bit Windows".  So the exit
    code and even the process lifetime say nothing about whether the export
    finished — the artifact can appear seconds later.
    """
    return os.path.basename(exe).lower() in ("wiztree.exe", "wiztree")


def remember_exe(path: str) -> bool:
    """Cache a discovered executable path so later runs skip the search.

    Deliberately **not** called by :func:`discover`.  A lookup that writes user
    configuration as a side effect is a trap: it makes the function
    non-idempotent and lets one bad lookup poison every later run.  The CLI calls
    this explicitly from ``info``, after showing the user the result.

    Returns:
        ``True`` when the configuration was updated.
    """
    try:
        config = load_config()
        if config.get("exe") == path:
            return False
        config["exe"] = path
        save_config(config)
        return True
    except OSError:
        return False  # an unwritable state dir must not break discovery


def discover(explicit: str | None = None) -> tuple[str, str]:
    """Locate WizTree, returning ``(path, how_it_was_found)``.

    Pure with respect to user configuration: reads ``config.json``, never writes
    it.  A remembered path that no longer exists is simply skipped, so a moved
    installation is rediscovered from the registry on the next run.

    Raises:
        WizTreeNotFound: with install instructions when nothing is found.
    """
    if explicit and not os.path.exists(explicit):
        raise WizTreeNotFound(
            f"WizTree executable not found at the path given: {explicit}\n"
            f"Download WizTree from {DOWNLOAD_URL} or drop --exe."
        )
    for path, source in iter_candidates(explicit):
        if path and os.path.exists(path):
            return path, source
    raise WizTreeNotFound(
        "WizTree not found.\n"
        f"  1. Download and install it from {DOWNLOAD_URL}\n"
        f"  2. Or point this CLI at the portable build: set {ENV_VAR}=<path to WizTree64.exe>\n"
        "  3. Or pass --exe <path> on any command"
    )


def find_wiztree(explicit: str | None = None) -> str:
    """Locate the WizTree executable.

    Resolution order: *explicit* argument → ``$WIZTREE_EXE`` → remembered config
    → Windows uninstall registry → ``PATH`` → environment-derived install
    directories → user-configured ``search_paths``.

    Raises:
        WizTreeNotFound: with install instructions when nothing is found.
    """
    return discover(explicit)[0]


def wiztree_version(exe: str) -> str:
    """Best-effort version string for *exe* (``"unknown"`` when unavailable)."""
    if os.name != "nt":
        return "unknown"
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-Item -LiteralPath '{exe}').VersionInfo.FileVersion",
            ],
            capture_output=True, text=True, timeout=20,
        )
        version = (completed.stdout or "").strip().splitlines()
        return version[0].strip() if version and version[0].strip() else "unknown"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


def probe(exe: str | None = None) -> dict[str, Any]:
    """Report what this machine can actually do with WizTree.

    Never raises — and that promise is enforced, not assumed: a path can be
    discovered and then vanish before it is stat'ed (observed in the wild with a
    stale cached path), so the whole "found" branch is guarded.  ``source`` says
    how the executable was found (``registry``, ``PATH``, ``config``, …), which
    is what a user needs when a stale path is being picked up.
    """
    try:
        resolved, source = discover(exe)
    except WizTreeNotFound as exc:
        return {
            "found": False,
            "exe": None,
            "source": None,
            "version": None,
            "error": str(exc),
            "download_url": DOWNLOAD_URL,
            "is_admin": _is_admin(),
            "config_path": config_path(),
            "candidates": [path for path, _src in iter_candidates(exe)],
        }
    try:
        size = os.path.getsize(resolved)
    except OSError as exc:
        return {
            "found": False,
            "exe": resolved,
            "source": source,
            "version": None,
            "error": f"WizTree was found at {resolved} but cannot be read: {exc}",
            "download_url": DOWNLOAD_URL,
            "is_admin": _is_admin(),
            "config_path": config_path(),
            "candidates": [path for path, _src in iter_candidates(exe)],
        }
    return {
        "found": True,
        "exe": resolved,
        "source": source,
        "version": wiztree_version(resolved),
        "size": size,
        "download_url": DOWNLOAD_URL,
        "is_admin": _is_admin(),
        "config_path": config_path(),
        "candidates": [path for path, _src in iter_candidates(exe)],
    }


def _is_admin() -> bool:
    """True when the current process can use the fast MFT scanner."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - platform dependent
        return False


# ── argument construction ─────────────────────────────────────────────

def _flag(name: str, value: Any, *, on: str = "1", off: str = "0") -> str:
    """Build a WizTree ``/name=value`` switch from a Python bool."""
    if isinstance(value, bool):
        return f"/{name}={on if value else off}"
    return f"/{name}={value}"


def _tri(value: bool | None, default: bool) -> bool:
    return default if value is None else bool(value)


def build_export_args(
    target: str,
    export_path: str,
    *,
    filter: str | None = None,
    filterexclude: str | None = None,
    filterfullpath: bool | None = None,
    admin: bool | None = None,
    exportfolders: bool | None = None,
    exportfiles: bool | None = None,
    sortby: int | None = None,
    exportmftrecno: bool | None = None,
    utc_time: bool | None = None,
    all_dates: bool | None = None,
    all_sizes: bool | None = None,
    split_filename: bool | None = None,
    drive_capacity: bool | None = None,
    percent_of_parent: bool | None = None,
    max_depth: int | None = None,
) -> list[str]:
    """Build the argv for a CSV export, pinning the schema (F3).

    Every advanced switch is emitted explicitly.  Leaving one out lets a
    machine-local WizTree setting change the column layout, which would make
    two runs of the same command produce different files.

    Args:
        target: Drive (``"C:"``) or folder (``"C:\\Users"``) to scan.
        export_path: Destination CSV.
        filter: Include filter, ``|`` separated, e.g. ``"*.mp3|*.wav"``.
        filterexclude: Exclude filter, same syntax.
        filterfullpath: Match filters against the full path (``True``, default)
            or the file name only.
        admin: Request the fast MFT scan (``True``) or stay unelevated.
        exportfolders: Emit folder rows (default ``True``).
        exportfiles: Emit file rows (default ``True``).
        sortby: ``0`` name, ``1`` size, ``2`` allocated, ``3`` modified.
        exportmftrecno: Include NTFS MFT record numbers.
        utc_time: Export UTC instead of local timestamps.
        all_dates: Add created/accessed dates.
        all_sizes: Add folder-exclusive size columns.
        split_filename: Split root/folder/name/extension columns.
        drive_capacity: Add the drive capacity columns.
        percent_of_parent: Add percentage-of-parent column.
        max_depth: Limit exported folder depth (``/exportmaxdepth``).

    Raises:
        ValueError: *sortby* is not 0-3.
    """
    resolved_sortby = 0 if sortby is None else int(sortby)
    if resolved_sortby not in (0, 1, 2, 3):
        raise ValueError(f"sortby must be 0-3 (name/size/allocated/modified), got {sortby!r}")
    if max_depth is not None and int(max_depth) < 0:
        raise ValueError(f"max_depth must be >= 0, got {max_depth!r}")

    args: list[str] = [target, f"/export={export_path}"]
    args.append(_flag("filterfullpath", _tri(filterfullpath, True)))
    args.append(_flag("admin", _tri(admin, False)))
    args.append(_flag("exportfolders", _tri(exportfolders, True)))
    args.append(_flag("exportfiles", _tri(exportfiles, True)))
    args.append(f"/sortby={resolved_sortby}")
    args.append(_flag("exportmftrecno", _tri(exportmftrecno, False)))
    args.append(_flag("exportUTCTime", _tri(utc_time, False)))
    args.append(_flag("exportalldates", _tri(all_dates, False)))
    args.append(_flag("exportallsizes", _tri(all_sizes, False)))
    args.append(_flag("exportsplitfilename", _tri(split_filename, False)))
    args.append(_flag("exportdrivecapacity", _tri(drive_capacity, False)))
    args.append(_flag("exportpercentofparent", _tri(percent_of_parent, False)))

    if filter is not None:
        args.append(f"/filter={filter}")
    if filterexclude is not None:
        args.append(f"/filterexclude={filterexclude}")
    if max_depth:
        args.append(f"/exportmaxdepth={int(max_depth)}")
    return args


def build_filetypes_args(
    target: str,
    export_path: str,
    *,
    admin: bool | None = None,
    filter: str | None = None,
    filterexclude: str | None = None,
    sortby: int | None = None,
) -> list[str]:
    """Build the argv for an ``/exportfiletypes`` report (per-extension totals)."""
    resolved_sortby = 1 if sortby is None else int(sortby)
    if resolved_sortby not in (0, 1, 2):
        raise ValueError(f"filetype sortby must be 0-2, got {sortby!r}")
    args: list[str] = [target, f"/exportfiletypes={export_path}", _flag("admin", _tri(admin, False))]
    args.append(f"/sortbyfiletypes={resolved_sortby}")
    if filter is not None:
        args.append(f"/filter={filter}")
    if filterexclude is not None:
        args.append(f"/filterexclude={filterexclude}")
    return args


def build_treemap_args(
    target: str,
    image_path: str,
    *,
    width: int = 1920,
    height: int = 1080,
    gray: bool | None = None,
    free_space: bool | None = None,
    show_allocated: bool | None = None,
    admin: bool | None = None,
) -> list[str]:
    """Build the argv for a treemap PNG export.

    Defaults mirror the vendor's own (1920x1080, greyscale off, free space on),
    so omitting an option here means the same thing as omitting it in WizTree.

    Note this is the one export path with no field in the vendor docs, so
    WizTree always attempts an on-screen render; it still exits on its own once
    the PNG is written.
    """
    for label, value in (("width", width), ("height", height)):
        if int(value) <= 0:
            raise ValueError(f"treemap {label} must be positive, got {value!r}")
    return [
        target,
        f"/treemapimagefile={image_path}",
        f"/treemapimagewidth={int(width)}",
        f"/treemapimageheight={int(height)}",
        _flag("treemapimagegray", _tri(gray, False)),
        _flag("treemapimagefreespace", _tri(free_space, True)),
        _flag("treemapimageshowallocated", _tri(show_allocated, False)),
        _flag("admin", _tri(admin, False)),
    ]


def build_mft_args(drive: str, dump_path: str) -> list[str]:
    """Build the argv for an NTFS ``$MFT`` dump (requires admin to be useful)."""
    return [drive, f"/dumpmftfile={dump_path}"]


# ── execution ─────────────────────────────────────────────────────────

def _default_runner(argv: Sequence[str], timeout: int) -> int:
    """Run WizTree to completion and return its exit code.

    stdout/stderr go to ``DEVNULL`` on purpose: WizTree is a GUI-subsystem
    binary that writes nothing to the console (measured), and leaving its pipes
    unread would risk a hang if it ever did.  The artifact check downstream is
    the real success signal (F1), so nothing of value is lost.
    """
    completed = subprocess.run(
        list(argv),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    return completed.returncode


def _preflight_output(artifact: str) -> None:
    """Fail fast when the destination cannot be written.

    Measured the hard way: pointing the export at an unwritable directory
    (``C:\\Windows`` as a non-admin) makes WizTree put up a **modal error dialog**
    that nobody can click, so the process never returns.  With stdio on DEVNULL
    and a 30-minute default timeout, an agent would simply hang in silence.  A
    two-line write test turns that into an immediate, actionable error.
    """
    directory = os.path.dirname(os.path.abspath(artifact)) or os.getcwd()
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise WizTreeExportError(
                f"Output directory does not exist and cannot be created: {directory} ({exc})"
            ) from exc
    probe = os.path.join(directory, f".wiztree_write_probe_{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
        os.remove(probe)
    except OSError as exc:
        raise WizTreeExportError(
            f"Output directory is not writable: {directory} ({exc})\n"
            "WizTree would put up a modal error dialog here and never return, so this "
            "is refused up front instead of hanging. Pick a writable -o path."
        ) from exc


def _wait_for_artifact(artifact: str, grace_seconds: float) -> bool:
    """Wait up to *grace_seconds* for the artifact to appear and be non-empty."""
    deadline = time.time() + grace_seconds
    while True:
        if os.path.exists(artifact) and os.path.getsize(artifact) > 0:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _run_and_validate(
    argv: Sequence[str],
    artifact: str,
    *,
    kind: str,
    timeout: int,
    runner: Runner | None,
    subject: str = "the target",
    hint: str = "",
    grace_seconds: float = 15.0,
    exe: str = "",
) -> dict[str, Any]:
    """Run *argv*, then require a non-empty *artifact* at *artifact*.

    The stale file is removed first so a previous run can never be mistaken for
    this one's output.  After the process returns, the artifact is given
    *grace_seconds* to appear: the 32-bit ``wiztree.exe`` build only launches the
    64-bit one and returns immediately, so judging on "process exited, no file"
    would report a false failure while the export is still being written.
    """
    _preflight_output(artifact)
    if os.path.exists(artifact):
        try:
            os.remove(artifact)
        except OSError as exc:
            raise WizTreeExportError(
                f"Cannot replace existing {kind} at {artifact}: {exc}"
            ) from exc

    run = runner or _default_runner
    started = time.time()
    try:
        exit_code = run(list(argv), timeout)
    except subprocess.TimeoutExpired as exc:
        raise WizTreeExportError(
            f"WizTree timed out after {timeout}s producing {kind}: {artifact}\n"
            "If WizTree put up a dialog (unwritable path, unsupported volume), it is "
            "waiting for input that will never come. Check the destination and the "
            "target, or lower --timeout."
        ) from exc
    except OSError as exc:
        raise WizTreeExportError(f"Could not launch WizTree: {exc}") from exc
    process_ms = int((time.time() - started) * 1000)

    waited_ms = 0
    if not (os.path.exists(artifact) and os.path.getsize(artifact) > 0):
        wait_started = time.time()
        # A launcher-only build returns before its child has written anything, so
        # give it longer than the usual asynchronous-write grace period.
        allowance = max(grace_seconds, 30.0) if is_launcher_only(exe or argv[0]) else grace_seconds
        _wait_for_artifact(artifact, allowance)
        waited_ms = int((time.time() - wait_started) * 1000)

    if not os.path.exists(artifact) or os.path.getsize(artifact) == 0:
        raise WizTreeExportError(
            f"WizTree ran (exit code {exit_code}) but produced no output {kind} at {artifact} "
            f"after waiting {waited_ms} ms.\n"
            "WizTree returns exit code 0 even when it fails, so this is reported as an error, "
            "not a success. The usual causes are:\n"
            f"  - the target does not exist or is not readable: {subject}\n"
            "  - the drive is not NTFS and the scan was refused\n"
            "  - the filter matched nothing, which WizTree reports by writing nothing at all\n"
            + (f"  - {hint}\n" if hint else "")
        )

    return {
        "success": True,
        "artifact": artifact,
        "size": os.path.getsize(artifact),
        "elapsed_ms": int((time.time() - started) * 1000),
        "process_ms": process_ms,
        "waited_ms": waited_ms,
        "exit_code": exit_code,
        "exe_is_launcher_only": is_launcher_only(exe or argv[0]),
        "argv": list(argv),
    }


def run_export(
    target: str,
    export_path: str,
    *,
    exe: str | None = None,
    timeout: int = 1800,
    grace_seconds: float = 15.0,
    runner: Runner | None = None,
    **opts: Any,
) -> dict[str, Any]:
    """Scan *target* and write a CSV export to *export_path*.

    Args:
        target: Drive or folder to scan.
        export_path: Destination CSV (overwritten).
        exe: Explicit WizTree path; located automatically when omitted.
        timeout: Seconds before the scan is abandoned.
        grace_seconds: How long to keep waiting for the artifact after the
            process returns (the 32-bit launcher returns before its child writes).
        runner: Injectable ``(argv, timeout) -> exit_code`` for tests.
        **opts: Forwarded to :func:`build_export_args`.

    Returns:
        Result dict with ``success``, ``csv_path``, ``artifact``, ``size``,
        ``elapsed_ms``, ``exit_code`` and ``argv``.

    Raises:
        WizTreeNotFound: WizTree is not installed.
        WizTreeExportError: WizTree produced no usable file (F1).
    """
    resolved_exe = find_wiztree(exe)
    argv = [resolved_exe] + build_export_args(target, export_path, **opts)
    result = _run_and_validate(
        argv, export_path, kind="CSV export", timeout=timeout, runner=runner,
        subject=target, exe=resolved_exe, grace_seconds=grace_seconds,
        hint="if the target needs elevation, re-run with --admin (or from an elevated shell)",
    )
    result["csv_path"] = export_path
    result["target"] = target
    result["exe"] = resolved_exe
    return result


def export_filetypes(
    target: str,
    export_path: str,
    *,
    exe: str | None = None,
    timeout: int = 1800,
    grace_seconds: float = 15.0,
    runner: Runner | None = None,
    **opts: Any,
) -> dict[str, Any]:
    """Export per-extension totals (``/exportfiletypes``) for *target*."""
    resolved_exe = find_wiztree(exe)
    argv = [resolved_exe] + build_filetypes_args(target, export_path, **opts)
    result = _run_and_validate(
        argv, export_path, kind="file-type report", timeout=timeout, runner=runner,
        subject=target, exe=resolved_exe, grace_seconds=grace_seconds,
    )
    result["csv_path"] = export_path
    result["target"] = target
    result["exe"] = resolved_exe
    return result


def export_treemap(
    target: str,
    image_path: str,
    *,
    exe: str | None = None,
    timeout: int = 1800,
    grace_seconds: float = 15.0,
    runner: Runner | None = None,
    **opts: Any,
) -> dict[str, Any]:
    """Export a treemap PNG for *target*."""
    resolved_exe = find_wiztree(exe)
    argv = [resolved_exe] + build_treemap_args(target, image_path, **opts)
    result = _run_and_validate(
        argv, image_path, kind="treemap image", timeout=timeout, runner=runner,
        subject=target, exe=resolved_exe, grace_seconds=grace_seconds,
    )
    result["image_path"] = image_path
    result["target"] = target
    result["exe"] = resolved_exe
    return result


def dump_mft(
    drive: str,
    dump_path: str,
    *,
    exe: str | None = None,
    timeout: int = 1800,
    grace_seconds: float = 15.0,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Dump the raw NTFS ``$MFT`` of *drive*.

    **No elevation required** — verified: a non-admin ``/dumpmftfile`` produced a
    valid dump starting with the ``FILE0`` record signature.  (Elevation *is*
    required for ``/exportmftrecno``, i.e. MFT record numbers inside a CSV.)
    Be aware the dump is the complete raw MFT: it was 5.6 GB on the test machine's
    D: drive.
    """
    resolved_exe = find_wiztree(exe)
    argv = [resolved_exe] + build_mft_args(drive, dump_path)
    result = _run_and_validate(
        argv, dump_path, kind="MFT dump", timeout=timeout, runner=runner,
        subject=drive, exe=resolved_exe, grace_seconds=grace_seconds,
        hint="the volume must be NTFS; the dump is the full raw $MFT and can be many GB",
    )
    result["mft_path"] = dump_path
    result["drive"] = drive
    result["exe"] = resolved_exe
    return result
