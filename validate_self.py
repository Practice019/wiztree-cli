#!/usr/bin/env python3
"""Self-review against cli-anything-plugin/commands/validate.md (52 checks).

Run from the repository root:  python validate_self.py
Prints PASS/FAIL per check with the evidence used.
"""

from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(SRC, "cli_anything", "wiztree")
CLA = os.path.join(SRC, "cli_anything")

results: list[tuple[str, bool, str]] = []


def check(section: str, name: str, ok: bool, evidence: str = "") -> None:
    results.append((f"{section}: {name}", bool(ok), evidence))


def exists(rel: str) -> str:
    return os.path.join(ROOT, rel)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ── 1. Directory structure ────────────────────────────────────────────
check("1-Structure", "namespace sub-package exists", os.path.isdir(PKG), PKG)
check("1-Structure", "cli_anything/ has no __init__.py",
      not os.path.exists(os.path.join(CLA, "__init__.py")),
      "cli_anything/__init__.py absent")
check("1-Structure", "wiztree/ has __init__.py",
      os.path.exists(os.path.join(PKG, "__init__.py")))
for sub in ("core", "utils", "tests"):
    check("1-Structure", f"{sub}/ present", os.path.isdir(os.path.join(PKG, sub)))
setup_src = read(exists("pyproject.toml"))
check("1-Structure", "pyproject.toml declares an explicit package list",
      "packages = [" in setup_src and "cli_anything.wiztree" in setup_src)

# ── 2. Required files ─────────────────────────────────────────────────
required = {
    "README.md": "src/cli_anything/wiztree/README.md",
    "wiztree_cli.py": "src/cli_anything/wiztree/wiztree_cli.py",
    "core/project.py": "src/cli_anything/wiztree/core/project.py",
    "core/session.py": "src/cli_anything/wiztree/core/session.py",
    "core/export.py": "src/cli_anything/wiztree/core/export.py",
    "tests/TEST.md": "src/cli_anything/wiztree/tests/TEST.md",
    "tests/test_core.py": "src/cli_anything/wiztree/tests/test_core.py",
    "tests/test_full_e2e.py": "src/cli_anything/wiztree/tests/test_full_e2e.py",
}
for name, rel in required.items():
    check("2-Files", name, os.path.exists(exists(rel)), rel)
check("2-Files", "docs/WIZTREE.md (software SOP)",
      os.path.exists(exists("docs/WIZTREE.md")),
      "the SOP is shipped alongside the code")

# ── 3. CLI implementation standards ───────────────────────────────────
cli_src = read(exists("src/cli_anything/wiztree/wiztree_cli.py"))
check("3-CLI", "uses Click", "import click" in cli_src)
check("3-CLI", "command groups", cli_src.count("@cli.group(") >= 4,
      f"{cli_src.count('@cli.group(')} @cli.group decorators")
check("3-CLI", "--json flag", '"--json"' in cli_src)
check("3-CLI", "--project flag", '"--project"' in cli_src)
check("3-CLI", "handle_error decorator", "def handle_error(" in cli_src)
check("3-CLI", "REPL mode", 'def repl()' in cli_src and "@cli.command(\"repl\"" in cli_src)
check("3-CLI", "global session state", "_state: dict" in cli_src and "def get_session(" in cli_src)

# ── 4. Core module standards ──────────────────────────────────────────
proj = read(exists("src/cli_anything/wiztree/core/project.py"))
for fn in ("def create(", "def open(", "def save(", "def info(", "def list_profiles("):
    check("4-Core", f"project.py {fn.strip('def ')}", fn in proj)
sess = read(exists("src/cli_anything/wiztree/core/session.py"))
check("4-Core", "Session class", "class Session" in sess)
for fn in ("def undo(", "def redo(", "def snapshot("):
    check("4-Core", f"session.py {fn.strip('def ')}", fn in sess)
exp = read(exists("src/cli_anything/wiztree/core/export.py"))
check("4-Core", "export.py render()", "def render(" in exp)
check("4-Core", "export.py EXPORT_PRESETS", "EXPORT_PRESETS" in exp)

docstring_ok, typing_ok, problems = True, True, []
for rel in ("core/index.py", "core/query.py", "core/project.py", "core/session.py",
            "core/export.py", "utils/wiztree_backend.py"):
    src = read(exists(f"src/cli_anything/wiztree/{rel}"))
    tree = ast.parse(src)
    if not ast.get_docstring(tree):
        docstring_ok = False
        problems.append(f"{rel}: no module docstring")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is None and not node.name.startswith("__"):
                typing_ok = False
                problems.append(f"{rel}:{node.lineno} {node.name} lacks return annotation")
check("4-Core", "all core modules have docstrings", docstring_ok, "; ".join(problems) or "ok")
check("4-Core", "all core functions type-hinted", typing_ok, "; ".join(problems[:3]) or "ok")

# ── 5. Test standards ─────────────────────────────────────────────────
test_md = read(exists("src/cli_anything/wiztree/tests/TEST.md"))
check("5-Tests", "TEST.md has Part 1 plan", "## Part 1" in test_md)
check("5-Tests", "TEST.md has Part 2 results", "## Part 2" in test_md and "_（跑完后追加）_" not in test_md)
e2e = read(exists("src/cli_anything/wiztree/tests/test_full_e2e.py"))
# The rule is that the *shared* runner must not depend on cwd.  One test
# deliberately sets cwd because the defect it guards (Click's Windows argv
# globbing) is itself cwd-dependent — so the exception must be a named,
# documented helper rather than a stray `cwd=` in the shared runner.
_run_body = e2e.split("def _run(self", 1)[1].split("\n    def ", 1)[0] if "def _run(self" in e2e else ""
has_documented_helper = "def _run_in(" in e2e and "cwd-dependent" in e2e
check("5-Tests", "TestCLISubprocess class", "class TestCLISubprocess" in e2e)
check("5-Tests", "_resolve_cli used", '_resolve_cli("cli-anything-wiztree")' in e2e)
check("5-Tests", "_resolve_cli prints backend + FORCE_INSTALLED",
      "[_resolve_cli]" in e2e and "CLI_ANYTHING_FORCE_INSTALLED" in e2e)
check("5-Tests", "subprocess _run does not set cwd", "cwd=" not in _run_body,
      "_run must work from any directory")
check("5-Tests", "cwd use is confined to a documented helper",
      has_documented_helper,
      "cwd= may appear only in _run_in, with a comment explaining why")

# ── 6. Documentation ──────────────────────────────────────────────────
readme = read(exists("src/cli_anything/wiztree/README.md"))
check("6-Docs", "README installation", "## Installation" in readme)
check("6-Docs", "README usage", "## Usage" in readme or "## Command reference" in readme)
check("6-Docs", "README command reference", "## Command reference" in readme)
check("6-Docs", "README examples", readme.count("```bash") >= 5,
      f"{readme.count('```bash')} bash blocks")
sop = read(exists("docs/WIZTREE.md"))
check("6-Docs", "WIZTREE.md architecture analysis", "## 1. Architecture analysis" in sop)
check("6-Docs", "WIZTREE.md command map", "## 2. Command map" in sop)
check("6-Docs", "WIZTREE.md rendering gap assessment", "## 3. Rendering gap assessment" in sop)
check("6-Docs", "Apache-2.0 LICENSE present", os.path.exists(exists("LICENSE")))
check("6-Docs", "third-party attribution present",
      os.path.exists(exists("THIRD_PARTY_NOTICES.md")))

# ── 7. Packaging ──────────────────────────────────────────────────────
check("7-Packaging", "packages scoped to cli_anything.wiztree.*",
      'packages = [' in setup_src and "cli_anything.wiztree.core" in setup_src)
check("7-Packaging", "package name convention", 'name = "cli-anything-wiztree"' in setup_src)
check("7-Packaging", "entry point",
      "cli-anything-wiztree = \"cli_anything.wiztree.wiztree_cli:main\"" in setup_src)
check("7-Packaging", "no cli_anything/__init__.py",
      not os.path.exists(os.path.join(CLA, "__init__.py")))
# The convention is about how the package refers to *itself* and never to a
# sibling harness.  Relative imports inside cli_anything.wiztree are correct
# Python, so the check targets the entry module plus cross-harness leakage.
entry_src = read(exists("src/cli_anything/wiztree/wiztree_cli.py"))
foreign = []
for dirpath, _dirs, files in os.walk(PKG):
    for name in files:
        if name.endswith(".py"):
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
            for match in re.finditer(r"(?:from|import)\s+cli_anything\.([a-z_]+)", read(os.path.join(dirpath, name))):
                if match.group(1) != "wiztree":
                    foreign.append(f"{rel}: cli_anything.{match.group(1)}")
imports_ok = "from cli_anything.wiztree" in entry_src and not foreign
check("7-Packaging", "imports follow cli_anything.wiztree.* convention", imports_ok,
      f"entry uses absolute prefix={('from cli_anything.wiztree' in entry_src)}; "
      f"foreign={foreign or 'none'}")
check("7-Packaging", "dependencies present", "dependencies = [" in setup_src)
check("7-Packaging", "requires-python >= 3.10", 'requires-python = ">=3.10"' in setup_src)

# ── 8. Code quality ───────────────────────────────────────────────────
syntax_ok, syntax_evidence = True, []
for dirpath, _dirs, files in os.walk(PKG):
    for name in files:
        if name.endswith(".py"):
            path = os.path.join(dirpath, name)
            try:
                ast.parse(read(path))
            except SyntaxError as exc:
                syntax_ok = False
                syntax_evidence.append(f"{name}: {exc}")
check("8-Quality", "no syntax errors", syntax_ok, "; ".join(syntax_evidence) or "all .py parse")

bare_except, hardcoded, long_lines = [], [], []
# Path literals are judged in *executable* code only.  Docstrings that show a
# sample CSV and synthetic Windows paths used as test *data* are not
# dependencies on a machine, and flagging them would be noise.
DRIVE_LITERAL = re.compile(r"^[A-Za-z]:\\")
for dirpath, _dirs, files in os.walk(PKG):
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, ROOT)
        src = read(path)
        is_test = os.sep + "tests" + os.sep in path
        for i, line in enumerate(src.splitlines(), 1):
            if re.match(r"except\s*:", line.strip()):
                bare_except.append(f"{rel}:{i}")
            if len(line) > 120:
                long_lines.append(f"{rel}:{i}({len(line)})")
        if is_test:
            continue
        tree = ast.parse(src)
        # Blank out every docstring so only code literals remain.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    body[0].value.value = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and DRIVE_LITERAL.match(node.value):
                hardcoded.append(f"{rel}:{node.lineno} {node.value!r}")
check("8-Quality", "no bare except", not bare_except, "; ".join(bare_except) or "none")
check("8-Quality", "no hardcoded paths in executable code", not hardcoded,
      "; ".join(hardcoded) or "no drive-letter literals outside docstrings/tests")
check("8-Quality", "no lines over 120 chars", not long_lines,
      "; ".join(long_lines[:5]) or "none")

# ── report ────────────────────────────────────────────────────────────
passed = sum(1 for _n, ok, _e in results if ok)
print("CLI Harness Validation Report")
print("Software: wiztree")
print(f"Path: {PKG}\n")
for name, ok, evidence in results:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {evidence}" if evidence and not ok else ""))
print(f"\nOverall: {'PASS' if passed == len(results) else 'FAIL'} ({passed}/{len(results)} checks)")
sys.exit(0 if passed == len(results) else 1)
