# Third-party notices

This project is licensed under Apache-2.0 (see [`LICENSE`](LICENSE)). It includes
or builds on the following third-party material.

## HKUDS/CLI-Anything

- **Source:** <https://github.com/HKUDS/CLI-Anything>
- **License:** Apache-2.0
- **What is used:**

  1. **`src/cli_anything/wiztree/utils/repl_skin.py`** — a **verbatim copy** of
     `cli-anything-plugin/repl_skin.py` from that repository. The file's own
     docstring instructs harness authors to copy it into their package, which is
     what happened here. It has not been modified
     (SHA-256 `EFBBAE140830BACE95D11B3C4861D1F8A001477C18B7BC68F519066EB9C01B11`).
     It provides the terminal skin used by the interactive REPL and depends on
     `prompt-toolkit`.

  2. **Methodology and project layout** — the harness structure
     (`cli_anything/<software>/`, `core/`, `utils/`, `tests/`, `tests/TEST.md`)
     follows the CLI-Anything conventional layout described in
     `cli-anything-plugin/HARNESS.md`.

  3. `repl_skin.py` references the upstream repository by name when suggesting
     how to install the companion skill. Override it with the
     `CLI_ANYTHING_SKILL_REPO` environment variable if you install from a fork.

If you redistribute this project, keep this notice and the Apache-2.0 `LICENSE`
together, as that license requires.

## WizTree (Antibody Software Limited)

- **Source:** <https://diskanalyzer.com>
- **License:** proprietary, commercial
- **Relationship:** WizTree is **not** bundled, redistributed, or linked against.
  This project is an independent command-line front end that executes a
  separately installed `WizTree64.exe` and reads the CSV files it exports through
  its documented command-line interface. Users must obtain WizTree themselves and
  comply with its licence.

Documentation quotations from the WizTree guide at <https://diskanalyzer.com/guide>
are reproduced for interoperability purposes.

## Runtime dependencies

Installed automatically by pip:

| Package | License | Purpose |
|---|---|---|
| [`click`](https://github.com/pallets/click) | BSD-3-Clause | CLI framework |
| [`prompt-toolkit`](https://github.com/prompt-toolkit/python-prompt-toolkit) | BSD-3-Clause | REPL input (pulled in by `repl_skin.py`) |
| `wcwidth` | MIT | transitive dependency of prompt-toolkit |

Development only:

| Package | License |
|---|---|
| [`pytest`](https://github.com/pytest-dev/pytest) | MIT |
| `pytest-cov` | MIT |

The Python standard library modules used (`csv`, `json`, `subprocess`, `winreg`,
`msvcrt`, `ctypes`, …) are covered by the Python Software Foundation License.
