"""End-to-end tests for cli-anything-wiztree — these invoke the real WizTree.

No graceful degradation: if WizTree is not installed or stops working, these
tests fail rather than skip, because the whole harness is worthless without the
backend (HARNESS.md Phase 5).

Every test prints the artifact paths it produced so a human can open them.

The `TestRealBackend` class also re-checks F1/F3 from TEST.md Part 0 against the
real binary rather than a fake runner — those are the two findings that a mock
could never catch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from cli_anything.wiztree.core import export as export_mod  # noqa: E402
from cli_anything.wiztree.core import index, query  # noqa: E402
from cli_anything.wiztree.core.project import Project  # noqa: E402
from cli_anything.wiztree.utils import wiztree_backend as backend  # noqa: E402

STATE_ENV = "CLI_ANYTHING_WIZTREE_HOME"


# ── helpers ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _isolated_state_home(tmp_path_factory):
    """Keep the real ``~/.cli-anything-wiztree`` out of the test run.

    Discovery then runs against a clean state directory, which is also what
    proves the registry route works without a cached path.
    """
    home = tmp_path_factory.mktemp("wiztree_state")
    previous = os.environ.get(STATE_ENV)
    os.environ[STATE_ENV] = str(home)
    yield str(home)
    if previous is None:
        os.environ.pop(STATE_ENV, None)
    else:
        os.environ[STATE_ENV] = previous


@pytest.fixture(scope="module")
def scan_target():
    """The folder WizTree itself lives in.

    Deriving the target from discovery rather than hardcoding a path keeps these
    tests runnable on any machine that has WizTree installed, and that folder is
    small (a few MB) so a real scan stays fast.
    """
    return os.path.dirname(backend.find_wiztree())


def _resolve_cli(name):
    """Resolve the installed CLI command; falls back to ``python -m`` for dev.

    Set ``CLI_ANYTHING_FORCE_INSTALLED=1`` to require the installed command.
    """
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = name.replace("cli-anything-", "cli_anything.") + "." + name.split("-")[-1] + "_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


def _dir_size(path: str) -> int:
    """Total logical bytes under *path*, computed independently of WizTree."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


@pytest.fixture(scope="module")
def scan(scan_target, tmp_path_factory):
    """Run one real WizTree export and share it across the module."""
    out_dir = tmp_path_factory.mktemp("wiztree_e2e")
    csv_path = str(out_dir / "locale_scan.csv")
    result = backend.run_export(scan_target, csv_path, admin=False, timeout=300)
    entries, meta = index.parse_export_csv(result["csv_path"])
    print(f"\n  CSV: {result['csv_path']} ({result['size']:,} bytes, {result['elapsed_ms']} ms)")
    print(f"  parsed: {len(entries)} entries, columns={meta['columns']}, encoding={meta['encoding']}")
    return {"result": result, "entries": entries, "meta": meta, "dir": str(out_dir)}


# ── real backend ──────────────────────────────────────────────────────

class TestRealBackend:
    """Drives WizTree64.exe directly and verifies its artifacts."""

    def test_wiztree_is_installed(self):
        exe = backend.find_wiztree()
        info = backend.probe()
        print(f"\n  exe: {exe}\n  version: {info['version']}")
        assert os.path.exists(exe)
        assert info["found"] is True

    def test_export_produces_verifiable_csv(self, scan):
        verified = export_mod.verify_file(scan["result"]["csv_path"])
        assert verified["valid"] is True
        assert verified["file_size"] > 0
        assert verified["format"] == "CSV"

    def test_parsed_entries_have_folders_and_files(self, scan):
        entries = scan["entries"]
        assert len(entries) > 0
        assert any(e.is_folder for e in entries), "expected at least one folder row"
        assert any(not e.is_folder for e in entries), "expected at least one file row"

    def test_parsed_total_matches_filesystem(self, scan, scan_target):
        """Cross-check WizTree's own numbers against an independent walk."""
        root = next((e for e in scan["entries"] if e.is_folder), None)
        assert root is not None
        wiztree_total = root.size
        filesystem_total = _dir_size(scan_target)
        print(f"\n  WizTree: {wiztree_total:,} bytes   filesystem walk: {filesystem_total:,} bytes")
        assert filesystem_total > 0
        drift = abs(wiztree_total - filesystem_total) / filesystem_total
        assert drift < 0.05, f"totals disagree by {drift:.1%}"

    def test_f1_real_failure_is_reported_not_silently_successful(self, tmp_path):
        """F1: WizTree exits 0 on a bogus target but writes nothing."""
        target_csv = str(tmp_path / "never.csv")
        with pytest.raises(backend.WizTreeExportError) as excinfo:
            backend.run_export(r"Z:\definitely-not-a-drive", target_csv, admin=False, timeout=120)
        message = str(excinfo.value)
        assert "no output" in message.lower()
        assert r"Z:\definitely-not-a-drive" in message
        assert not os.path.exists(target_csv)

    def test_f3_columns_are_identical_between_runs(self, tmp_path, scan_target):
        """F3: pinned switches must make the schema run-independent."""
        first = backend.run_export(scan_target, str(tmp_path / "a.csv"), admin=False, timeout=300)
        second = backend.run_export(scan_target, str(tmp_path / "b.csv"), admin=False, timeout=300)
        cols_a = index.parse_export_csv(first["csv_path"])[1]["columns"]
        cols_b = index.parse_export_csv(second["csv_path"])[1]["columns"]
        print(f"\n  run A columns: {cols_a}\n  run B columns: {cols_b}")
        assert cols_a == cols_b
        assert len(cols_a) == 7, "drive-capacity columns must stay pinned off"

    def test_filter_restricts_export_to_matching_files(self, tmp_path, scan_target):
        csv_path = str(tmp_path / "po_only.csv")
        backend.run_export(
            scan_target, csv_path, admin=False, timeout=300,
            filter="*.po", exportfolders=False,
        )
        entries, _ = index.parse_export_csv(csv_path)
        assert entries, "filter produced no rows"
        assert all(e.ext == "po" for e in entries)
        assert all(not e.is_folder for e in entries)

    def test_filetypes_report_is_parsable(self, tmp_path, scan_target):
        csv_path = str(tmp_path / "types.csv")
        result = backend.export_filetypes(scan_target, csv_path, admin=False, timeout=300)
        rows, meta = index.parse_filetypes_csv(result["csv_path"])
        print(f"\n  file-type report: {result['csv_path']} ({result['size']:,} bytes, {len(rows)} rows)")
        assert rows, "expected at least one file type"
        assert isinstance(rows[0]["percent"], float)
        assert rows[0]["ext"].startswith(".")

    def test_treemap_png_export(self, tmp_path, scan_target):
        png_path = str(tmp_path / "treemap.png")
        result = backend.export_treemap(
            scan_target, png_path, admin=False, width=800, height=600, timeout=300
        )
        verified = export_mod.verify_file(result["image_path"])
        print(f"\n  treemap: {result['image_path']} ({result['size']:,} bytes)")
        assert verified["format"] == "PNG"
        assert result["size"] > 1024


class TestWorkflow:
    """A realistic ops workflow: triage a folder, report on it, re-open later."""

    def test_scan_query_report_reopen(self, tmp_path, scan, scan_target):
        csv_path = scan["result"]["csv_path"]

        # 1. index the scan into a project
        project_path = str(tmp_path / "cleanup.json")
        project = Project.create(project_path, root=scan_target,
                                 source_csv=csv_path, entries=scan["entries"],
                                 meta=scan["meta"])

        # 2. triage: the ten biggest files
        top_first = [e.name for e in query.top_entries(project.entries(), n=10, files_only=True)]
        assert len(top_first) == 10

        # 3. write a markdown report for a human
        report_path = str(tmp_path / "report.md")
        written = export_mod.write_export(
            query.top_entries(project.entries(), n=10, files_only=True),
            report_path, fmt="markdown",
        )
        print(f"\n  report: {report_path} ({written['size']:,} bytes)")
        assert written["size"] > 0
        assert "|" in open(report_path, encoding="utf-8").read()

        # 4. reopen the project in a fresh object and re-query
        reopened = Project.open(project_path)
        top_second = [e.name for e in query.top_entries(reopened.entries(), n=10, files_only=True)]

        # 5. the answer must be stable across the round trip
        assert top_first == top_second
        assert reopened.info()["stats"]["file_count"] == query.summary(scan["entries"])["file_count"]


# ── CLI as a subprocess ───────────────────────────────────────────────

class TestCLISubprocess:
    """Run the CLI the way a user or agent would — installed, from any cwd."""

    CLI_BASE = _resolve_cli("cli-anything-wiztree")

    @pytest.fixture(scope="class")
    def env(self, tmp_path_factory):
        home = tmp_path_factory.mktemp("wiztree_home")
        merged = os.environ.copy()
        merged[STATE_ENV] = str(home)
        merged["PYTHONIOENCODING"] = "utf-8"
        return merged

    def _run(self, args, env, check=True):
        # Deliberately no cwd: the installed command must work from anywhere.
        return subprocess.run(
            self.CLI_BASE + args,
            capture_output=True, text=True, encoding="utf-8",
            env=env, check=check,
        )

    def test_help(self, env):
        result = self._run(["--help"], env)
        assert result.returncode == 0
        assert "scan" in result.stdout and "query" in result.stdout

    def test_info_json(self, env):
        result = self._run(["--json", "info"], env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["found"] is True
        assert payload["exe"].lower().endswith(".exe")

    def test_scan_run_json(self, env, tmp_path, scan_target):
        csv_path = str(tmp_path / "cli_scan.csv")
        result = self._run(
            ["--json", "scan", "run", scan_target, "-o", csv_path], env
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        print(f"\n  CLI csv: {payload['csv_path']} ({payload['artifact_size']:,} bytes)")
        assert payload["entry_count"] > 0
        assert payload["stats"]["file_count"] > 0
        assert os.path.exists(payload["csv_path"])

    def test_query_reads_state_from_a_previous_invocation(self, env, tmp_path, scan_target):
        """The scan above persisted state; a new process must be able to query it."""
        self._run(["--json", "scan", "run", scan_target,
                   "-o", str(tmp_path / "q.csv")], env)
        result = self._run(["--json", "query", "summary"], env)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["file_count"] > 0

        top = self._run(["--json", "query", "top", "-n", "3", "--files"], env)
        assert top.returncode == 0, top.stderr
        assert len(json.loads(top.stdout)) == 3

    def test_failing_scan_exits_nonzero(self, env, tmp_path):
        """F1 at the CLI boundary: a silent WizTree failure must be an error."""
        result = self._run(
            ["--json", "scan", "run", r"Z:\definitely-not-a-drive",
             "-o", str(tmp_path / "never.csv")],
            env, check=False,
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "WizTreeExportError"
        assert "no output" in payload["error"].lower()

    def test_export_report_and_session(self, env, tmp_path, scan_target):
        self._run(["--json", "scan", "run", scan_target,
                   "-o", str(tmp_path / "r.csv")], env)
        report = str(tmp_path / "report.csv")
        result = self._run(
            ["--json", "export", "report", "-o", report, "--top", "5"], env
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["rows"] == 5
        assert payload["verified"]["valid"] is True

        history = self._run(["--json", "session", "history", "-n", "5"], env)
        assert history.returncode == 0
        assert len(json.loads(history.stdout)) >= 2

    def test_help_of_nested_groups(self, env):
        for group in ("scan", "query", "project", "export", "session"):
            result = self._run([group, "--help"], env)
            assert result.returncode == 0, f"{group} --help failed"

    def test_global_flag_works_after_the_subcommand(self, env):
        """Regression: `info --json` used to exit 2 — Click only takes group
        options before the verb, which is the opposite of what everyone types.
        Both orders must now work and agree.
        """
        before = self._run(["--json", "info"], env)
        after = self._run(["info", "--json"], env)
        assert before.returncode == 0, before.stderr
        assert after.returncode == 0, after.stderr
        assert json.loads(before.stdout)["exe"] == json.loads(after.stdout)["exe"]

    def test_global_flag_after_subcommand_on_nested_groups(self, env, tmp_path, scan_target):
        result = self._run(["scan", "run", scan_target, "-o",
                            str(tmp_path / "trailing.csv"), "--json"], env)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["entry_count"] > 0

        summary = self._run(["query", "summary", "--json"], env)
        assert summary.returncode == 0, summary.stderr
        assert json.loads(summary.stdout)["file_count"] > 0

    def test_admin_flag_after_subcommand_is_accepted(self, env, tmp_path):
        """--no-admin after the verb must parse (and not trigger a UAC prompt)."""
        result = self._run(["info", "--no-admin", "--json"], env)
        assert result.returncode == 0, result.stderr

    def _run_in(self, args, env, cwd, check=True):
        """Like _run but with an explicit cwd.

        Used *only* by the wildcard test below, because the defect it guards
        against is inherently cwd-dependent (Click glob-expanded argv against the
        working directory). Every other test keeps the no-cwd guarantee.
        """
        return subprocess.run(
            self.CLI_BASE + args,
            capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=cwd, check=check,
        )

    def test_wildcard_argument_is_not_glob_expanded(self, env, tmp_path, scan_target):
        """Regression: Click's Windows argv expansion rewrote pattern arguments.

        `query find "*.mp4"` used to become the .mp4 files of the current
        directory: rc 2 with two matches, and a silently different pattern with
        one. Patterns are payload here, so expansion is disabled.
        """
        workdir = tmp_path / "cwd_with_matching_files"
        workdir.mkdir()
        (workdir / "decoy_a.mp4").write_text("x")
        (workdir / "decoy_b.mp4").write_text("y")

        self._run(["--json", "scan", "run", scan_target,
                   "-o", str(tmp_path / "for_glob.csv")], env)

        result = self._run_in(["--json", "query", "find", "*.mp4"], env, str(workdir))
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        # The decoys are on disk, not in the index — expansion would have found them.
        assert payload == []

        patterned = self._run_in(["--json", "query", "find", "*.po"], env, str(workdir))
        assert patterned.returncode == 0, patterned.stderr
        assert len(json.loads(patterned.stdout)) > 0

    def test_usage_error_still_emits_json(self, env):
        """Regression: a Click usage error gave empty stdout under --json."""
        result = self._run(["--json", "info", "--definitely-not-an-option"], env, check=False)
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert "definitely-not-an-option" in payload["error"]

    def test_empty_export_is_an_error_unless_allowed(self, env, tmp_path, scan_target):
        """Regression: a filter matching nothing exits 0 with a '✓ Scanned' over
        an export holding no data rows — indistinguishable from a silent no-op."""
        blocked = self._run(
            ["--json", "scan", "run", scan_target, "-o", str(tmp_path / "empty.csv"),
             "--filter", "*.zzzzz"], env, check=False,
        )
        assert blocked.returncode != 0
        assert "no entries" in json.loads(blocked.stdout)["error"]

        allowed = self._run(
            ["--json", "scan", "run", scan_target, "-o", str(tmp_path / "empty2.csv"),
             "--filter", "*.zzzzz", "--allow-empty"], env, check=False,
        )
        assert allowed.returncode == 0, allowed.stderr
        assert json.loads(allowed.stdout)["entry_count"] == 0

    def test_unwritable_destination_fails_fast(self, env, tmp_path, scan_target):
        """Regression: a bad -o path made WizTree hang on a modal dialog for the
        whole 30-minute default timeout. It must now fail immediately."""
        blocker = tmp_path / "blocker.txt"
        blocker.write_text("not a directory")
        started = time.time()
        result = self._run(
            ["--json", "scan", "run", scan_target, "-o", str(blocker / "out.csv")],
            env, check=False,
        )
        elapsed = time.time() - started
        assert result.returncode != 0
        message = json.loads(result.stdout)["error"].lower()
        assert "not writable" in message or "cannot be created" in message
        assert elapsed < 30, f"took {elapsed:.0f}s — the pre-flight check did not run"
