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


#: The genuine runner, captured so the completion logic can tell a real scan
#: from an injected test double.  A double never spawns WizTree, so probing for
#: a live process would be both pointless and flaky under it.
_REAL_DEFAULT_RUNNER = _default_runner

#: Image names WizTree can be running under (the 64-bit binary and the 32-bit
#: launcher that spawns it).
_WIZTREE_IMAGES = ("WizTree64.exe", "WizTree.exe")


def _wiztree_process_probe() -> bool:
    """True while any WizTree process is alive.

    Needed because ``/admin=1`` makes WizTree **self-elevate**: it launches an
    elevated copy through ``ShellExecute(runas)`` and the original process
    exits immediately.  The elevated child is the one that scans and writes the
    export, so "the process I started has returned" is not the completion
    signal -- its disappearance is.

    The image name is compared **exactly**, never as a substring of the whole
    listing.  Measured the hard way: this CLI installs an entry point called
    ``cli-anything-wiztree.exe``, which contains ``wiztree.exe``, so a
    substring test matched the CLI's *own* process and every scan then waited
    out the full ``--timeout`` believing a writer was still running.

    Returns:
        ``False`` on any failure or on a non-Windows host, so a broken probe can
        never turn into an infinite wait on a platform this cannot apply to.
    """
    if os.name != "nt":
        return False

    wanted = {image.lower() for image in _WIZTREE_IMAGES}
    for image in _WIZTREE_IMAGES:
        try:
            # A filtered listing is far cheaper than dumping every process, and
            # this runs once per poll for the whole length of a scan.
            listed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        for line in listed.splitlines():
            if not line.startswith('"'):
                continue
            # tasklist CSV: "Image Name","PID","Session Name","Session#","Mem Usage"
            found = line[1:].split('","', 1)[0].strip().lower()
            if found in wanted:
                return True
    return False


def _artifact_is_readable(artifact: str) -> bool:
    """True when the artifact can be opened for reading.

    Measured: while the elevated child writes the export it holds the file with
    an exclusive lock, so a concurrent read raises ``PermissionError``.  That is
    a *not finished yet* signal, not a failure to report to the caller.
    """
    try:
        with open(artifact, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _wait_for_complete_artifact(
    artifact: str,
    allowance: float,
    *,
    probe: Callable[[], bool] | None = None,
    settle_seconds: float = 1.0,
    poll_seconds: float = 0.5,
) -> tuple[bool, float, str]:
    """Wait until the export is *finished*, not merely started.

    Three measured behaviours make "file exists and is non-empty" the wrong
    completion test:

    1. **The export is streamed.**  On a 331 GB volume the CSV was non-empty
       within seconds and then grew 3.7 MB -> 818 MB.  Returning early hands the
       parser a truncated file that still looks valid.
    2. **The writer may not be the process that returned** (``/admin=1``
       self-elevation), so a live process is the authoritative signal.
    3. **The file is exclusively locked while being written**, so a read raises
       ``PermissionError``.

    Completion is therefore: no writer process, a size that has stopped moving
    for *settle_seconds*, and a file that opens.

    Args:
        artifact: Path the export is being written to.
        allowance: Maximum seconds to wait.
        probe: ``() -> bool`` reporting a live writer; ``None`` disables it.
        settle_seconds: How long the size must stay unchanged.
        poll_seconds: Poll interval.

    Returns:
        ``(complete, waited_seconds, reason)`` -- *reason* is a short diagnostic
        string suitable for an error message.
    """
    started = time.time()
    deadline = started + max(0.0, allowance)
    last_size = -1
    stable_since: float | None = None
    reason = "no artifact appeared"

    while True:
        try:
            size = os.path.getsize(artifact)
        except OSError:
            size = -1

        # Cheap signals first, and once the artifact exists the decision is made
        # on the artifact itself.  The process probe is consulted ONLY while
        # nothing has been written yet, because "is the elevated child alive?" is
        # the only question it can actually answer.
        #
        # Measured the hard way: probing for *any* WizTree process on every poll
        # let an unrelated instance left over from an earlier run -- still alive
        # 11 hours later, PID 34096 -- veto completion forever.  A scan that had
        # already written its 63.67 MB file sat there until the full --timeout.
        # A stale process cannot keep growing the file, so the artifact's own
        # size and lock state are the authoritative signals.
        writer_alive = bool(probe()) if (size <= 0 and probe is not None) else False

        if size > 0 and _artifact_is_readable(artifact) and not writer_alive:
            if size == last_size:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= settle_seconds:
                    return True, time.time() - started, "artifact complete"
            else:
                stable_since = None
        else:
            stable_since = None
            if writer_alive:
                reason = "no artifact yet and a WizTree process is running"
            elif size > 0 and not _artifact_is_readable(artifact):
                reason = "the artifact is still exclusively locked by its writer"
            elif size == 0:
                reason = "the artifact is still empty"

        last_size = size
        if time.time() >= deadline:
            return False, time.time() - started, reason
        time.sleep(poll_seconds)


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
    process_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run *argv*, then require a *finished*, non-empty *artifact* at *artifact*.

    The stale file is removed first so a previous run can never be mistaken for
    this one's output.

    Two measured traps make the naive "process exited + file is non-empty" test
    wrong, and both are handled here:

    * **Self-elevation.**  ``/admin=1`` makes WizTree relaunch itself elevated
      via ``ShellExecute(runas)``; the process this function started returns
      immediately while the elevated child is still scanning.  A fixed
      *grace_seconds* therefore expires mid-scan and the caller is told "no
      output" for a scan that is running perfectly.  When a live WizTree process
      is detected the wait is extended to *timeout*, which is the only bound the
      caller actually promised.
    * **Streamed output.**  The CSV becomes non-empty within seconds and then
      keeps growing, so a non-empty file is not a finished file.  Completion is
      delegated to :func:`_wait_for_complete_artifact`, which also treats the
      writer's exclusive lock as "still going" instead of surfacing a raw
      ``PermissionError`` to the caller.
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
    # Probing for a live process only makes sense when WizTree was really
    # launched; under an injected runner the probe would be both meaningless and
    # a source of flakiness (it would see any user-started WizTree GUI).
    if process_probe is not None:
        probe: Callable[[], bool] | None = process_probe
    else:
        probe = _wiztree_process_probe if run is _REAL_DEFAULT_RUNNER else None

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

    def _looks_complete() -> bool:
        """Done = a non-empty artifact that can be opened right now.

        Deliberately does **not** ask whether a WizTree process exists: an
        unrelated instance running for any reason is not evidence about *this*
        artifact, and treating it as one makes the whole command wait out
        ``--timeout`` for a scan that finished long ago.  A live writer still
        holds the file locked, which :func:`_artifact_is_readable` already
        reports, and a growing file is caught by the stability check.
        """
        try:
            if os.path.getsize(artifact) <= 0:
                return False
        except OSError:
            return False
        return _artifact_is_readable(artifact)

    waited_ms = 0
    wait_reason = ""
    if not _looks_complete():
        wait_started = time.time()
        # A launcher-only build returns before its child has written anything, so
        # give it longer than the usual asynchronous-write grace period.
        allowance = max(grace_seconds, 30.0) if is_launcher_only(exe or argv[0]) else grace_seconds
        # Only while nothing exists yet does a live process mean "still working":
        # that is the self-elevation case, where the elevated child has not
        # produced a file at all.  Widen the bound to the caller's own --timeout
        # rather than a 15 s guess made before the scan even started.
        try:
            nothing_yet = os.path.getsize(artifact) <= 0
        except OSError:
            nothing_yet = True
        if nothing_yet and probe is not None and probe():
            allowance = max(allowance, float(timeout))
        _wait_for_complete_artifact(artifact, allowance, probe=probe)
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

    if not _artifact_is_readable(artifact):
        raise WizTreeExportError(
            f"WizTree produced {kind} at {artifact} but the file is still exclusively "
            f"locked by its writer after {waited_ms} ms, so it cannot be read yet.\n"
            "A streamed export is only safe to parse once the writing process has "
            "exited; wait for WizTree to close the file and retry."
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
    process_probe: Callable[[], bool] | None = None,
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
        process_probe=process_probe,
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
