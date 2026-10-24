#!/usr/bin/env bash
# One-shot setup for cli-anything-wiztree on Git Bash / WSL.
#
# This script does NOT bundle or redistribute WizTree. It fetches the portable
# build from the vendor's own server, on the user's own machine, which keeps the
# WizTree EULA intact: the free edition is licensed "PERSONAL USE ONLY" and its
# terms forbid copying or distributing the software to third parties. Packaging
# it with an application requires a paid Distribution License (US$1,200/year,
# https://diskanalyzer.com/distribution-license).
#
# The PowerShell script (setup.ps1) is the primary path and supports the vendor
# installer too. This one covers the portable route only.
#
# Usage:
#   ./setup.sh                      # -> ~/.local/share/wiztree (or %LOCALAPPDATA%)
#   ./setup.sh -d /d/tools/wiztree  # custom location
#   ./setup.sh -f                   # re-download even if already bound
#   ./setup.sh -s                   # skip the smoke-test scan

set -euo pipefail

VENDOR_PAGE='https://diskanalyzer.com/download'
VENDOR_BASE='https://diskanalyzer.com/'

INSTALL_DIR=''
FORCE=0
SKIP_SCAN=0

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while getopts ':d:fs-:h' opt; do
    case "$opt" in
        d) INSTALL_DIR="$OPTARG" ;;
        f) FORCE=1 ;;
        s) SKIP_SCAN=1 ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done

step()  { printf '\n[%s] %s\n' "$1" "$2"; }
ok()    { printf '    OK    %s\n' "$1"; }
warn()  { printf '    WARN  %s\n' "$1"; }
fail()  { printf '    FAIL  %s\n' "$1"; exit 1; }
info()  { printf '          %s\n' "$1"; }

cat <<'BANNER'

  cli-anything-wiztree — setup (bash)
  -----------------------------------
  Installs and binds WizTree, the disk-space analyzer this CLI drives.

  WizTree is NOT bundled with this project. It is downloaded here, from the
  vendor's own server, onto your machine. The free edition is licensed for
  PERSONAL USE ONLY; commercial use needs a supporter code, and shipping it
  inside your own application needs a Distribution License (US$1,200/year).
    license:       https://diskanalyzer.com/eula
    distribution:  https://diskanalyzer.com/distribution-license

BANNER

# ── 0. preconditions ─────────────────────────────────────────────────────────
step 0 'Checking preconditions'

command -v curl >/dev/null 2>&1 || fail 'curl is required but not on PATH'
ok 'curl found'

command -v unzip >/dev/null 2>&1 || fail 'unzip is required but not on PATH'
ok 'unzip found'

command -v cli-anything-wiztree >/dev/null 2>&1 || {
    warn 'cli-anything-wiztree is not on PATH'
    info 'Install it first, from the repository root:  pip install .'
    exit 1
}
ok "CLI found: $(command -v cli-anything-wiztree)"

# ── 1. is WizTree already usable? ────────────────────────────────────────────
step 1 'Looking for an existing WizTree'

ALREADY=0
EXISTING_SOURCE=''
if probe=$(cli-anything-wiztree --json info 2>/dev/null); then
    if printf '%s' "$probe" | grep -q '"found": *true'; then
        if [ "$FORCE" -eq 1 ]; then
            info '-f given; ignoring the existing install and re-downloading'
        else
            ALREADY=1
            EXISTING_SOURCE=$(printf '%s' "$probe" \
                | grep -o '"source": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
            EXISTING_EXE=$(printf '%s' "$probe" \
                | grep -o '"exe": *"[^"]*"' | head -1 | sed 's/.*"\(.*\)"$/\1/')
            ok "Found WizTree at $EXISTING_EXE"
            info "source: $EXISTING_SOURCE"
        fi
    fi
fi

if [ "$ALREADY" -eq 1 ]; then
    printf '\n  WizTree is already installed and bound — nothing to download.\n'
    printf '  Re-run with -f to download a fresh portable copy anyway.\n'
fi

# ── 2. download ──────────────────────────────────────────────────────────────
WIZ_EXE=''
if [ "$ALREADY" -eq 0 ]; then
    step 2 'Downloading WizTree from the vendor'

    info 'Resolving the portable URL from the download page…'
    page=$(curl -fsSL --max-time 30 "$VENDOR_PAGE") || fail "Could not reach $VENDOR_PAGE"
    rel=$(printf '%s' "$page" | grep -o 'href="[^"]*_portable\.zip"' | head -1 | sed 's/href="//;s/"$//')
    [ -n "$rel" ] || fail 'Could not find the portable link on the download page (vendor may have changed the layout)'
    url="${VENDOR_BASE}${rel#/}"

    # default location
    if [ -z "$INSTALL_DIR" ]; then
        if [ -n "${LOCALAPPDATA:-}" ]; then
            INSTALL_DIR="$(cygpath -u "$LOCALAPPDATA" 2>/dev/null || printf '%s' "$LOCALAPPDATA")/WizTree"
        else
            INSTALL_DIR="$HOME/.local/share/wiztree"
        fi
    fi

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT

    info "downloading $url"
    curl -fsSL --max-time 300 -o "$tmp/wz.zip" "$url" || fail 'Download failed'
    size=$(wc -c < "$tmp/wz.zip")
    ok "downloaded $(( size / 1024 / 1024 )) MB"

    info "extracting to $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    unzip -q "$tmp/wz.zip" -d "$INSTALL_DIR" || fail 'Extraction failed'

    if [ -f "$INSTALL_DIR/WizTree64.exe" ]; then
        WIZ_EXE="$INSTALL_DIR/WizTree64.exe"
    elif [ -f "$INSTALL_DIR/WizTree.exe" ]; then
        WIZ_EXE="$INSTALL_DIR/WizTree.exe"
    else
        fail "WizTree64.exe not found after extraction — inspect $INSTALL_DIR"
    fi
    ok "extracted: $WIZ_EXE"

    # hand the Windows path to the CLI when we are inside WSL
    if command -v wslpath >/dev/null 2>&1; then
        WIZ_EXE_WIN=$(wslpath -w "$WIZ_EXE")
    else
        WIZ_EXE_WIN="$WIZ_EXE"
    fi
fi

# ── 3. bind ──────────────────────────────────────────────────────────────────
step 3 'Binding WizTree to the CLI'

if [ "$ALREADY" -eq 1 ]; then
    ok "already bound via source '$EXISTING_SOURCE' — nothing to do"
else
    bound=$(cli-anything-wiztree --exe "$WIZ_EXE_WIN" --json info 2>&1) || {
        fail "The downloaded binary was not accepted by info: $bound"
    }
    printf '%s' "$bound" | grep -q '"found": *true' || fail "info rejected the binary: $bound"
    ok 'bound and saved to the state directory'
    info 'later commands need no --exe'
fi

# ── 4. verify ────────────────────────────────────────────────────────────────
step 4 'Verifying'

final=$(cli-anything-wiztree --json info 2>&1) || fail "info failed: $final"
printf '%s' "$final" | grep -q '"found": *true' || fail "info reports found: false after binding: $final"

ver=$(printf '%s' "$final" | grep -o '"version": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
src=$(printf '%s' "$final" | grep -o '"source": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
ok "found: true   source: $src   version: $ver"

if printf '%s' "$final" | grep -q '"is_admin": *true'; then
    ok 'running elevated — index build/sync available'
else
    info 'not elevated (normal). `index build` and `index sync` need an admin shell:'
    info '    Start-Process pwsh -Verb RunAs'
fi

if [ "$SKIP_SCAN" -eq 0 ]; then
    info 'running a small real scan as a smoke test…'
    csv=$(mktemp -u).csv
    if scan=$(cli-anything-wiztree --json scan run "${TMPDIR:-/tmp}" --folders-only -o "$csv" 2>&1); then
        count=$(printf '%s' "$scan" | grep -o '"entry_count": *[0-9]*' | head -1 | grep -o '[0-9]*$')
        ok "scan works: ${count:-?} folders"
    else
        warn 'smoke-test scan failed — output follows:'
        info "$scan"
    fi
    rm -f "$csv" "$csv.wiztree-index.pkl"
fi

cat <<'DONE'

  Setup complete.
  ---------------
  Try it:

    cli-anything-wiztree info --json
    cli-anything-wiztree scan run "C:" --folders-only
    cli-anything-wiztree --json query top -n 25

  For the fast path, build the MFT index once (needs an admin shell):

    Start-Process pwsh -Verb RunAs
    cli-anything-wiztree index build "C:" -o index_C.live
    cli-anything-wiztree --json index top --index index_C.live -n 30

DONE
