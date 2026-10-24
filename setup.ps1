<#
.SYNOPSIS
    One-shot setup for cli-anything-wiztree: install WizTree, bind it, verify.

.DESCRIPTION
    This script does NOT bundle or redistribute WizTree. It fetches the portable
    build from the vendor's own server, on the user's own machine, which keeps the
    WizTree EULA intact: the free edition is licensed "PERSONAL USE ONLY" and its
    terms forbid copying or distributing the software to third parties. Packaging
    it with an application requires a paid Distribution License (US$1,200/year,
    https://diskanalyzer.com/distribution-license).

    What it does, in order:
      1. look for an existing WizTree installation (registry, PATH, common dirs)
      2. if none, download the portable ZIP from diskanalyzer.com and extract it
      3. bind the executable via `info --exe`, which persists <state>/config.json
      4. verify with `info --json` and a tiny real scan

.PARAMETER InstallDir
    Where to put the portable build. Default: %LOCALAPPDATA%\WizTree.

.PARAMETER SetupExe
    Use the vendor installer instead of the portable build (needs UAC).

.PARAMETER Force
    Re-download even when a working WizTree is already bound.

.PARAMETER SkipScan
    Skip the final smoke-test scan.

.EXAMPLE
    .\setup.ps1

.EXAMPLE
    .\setup.ps1 -InstallDir D:\tools\WizTree

.EXAMPLE
    .\setup.ps1 -SetupExe
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$SetupExe,
    [switch]$Force,
    [switch]$SkipScan
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is far faster without it

$VendorPage   = 'https://diskanalyzer.com/download'
$VendorBase   = 'https://diskanalyzer.com/'
$LicenseUrl   = 'https://diskanalyzer.com/eula'
$DistributionUrl = 'https://diskanalyzer.com/distribution-license'

if (-not $InstallDir) {
    $InstallDir = Join-Path $env:LOCALAPPDATA 'WizTree'
}

# ── output helpers ───────────────────────────────────────────────────────────
function Write-Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Write-Ok($text)       { Write-Host "    OK    $text" -ForegroundColor Green }
function Write-Warn2($text)    { Write-Host "    WARN  $text" -ForegroundColor Yellow }
function Write-Err($text)      { Write-Host "    FAIL  $text" -ForegroundColor Red }
function Write-Info($text)     { Write-Host "          $text" -ForegroundColor DarkGray }

function Exit-WithError($message, $hint) {
    Write-Err $message
    if ($hint) { Write-Info $hint }
    exit 1
}

# ── banner ───────────────────────────────────────────────────────────────────
Write-Host @'

  cli-anything-wiztree — setup
  ----------------------------
  Installs and binds WizTree, the disk-space analyzer this CLI drives.

  WizTree is NOT bundled with this project. It is downloaded here, from the
  vendor's own server, onto your machine. The free edition is licensed for
  PERSONAL USE ONLY; commercial use needs a supporter code, and shipping it
  inside your own application needs a Distribution License (US$1,200/year).
    license:       https://diskanalyzer.com/eula
    distribution:  https://diskanalyzer.com/distribution-license
'@ -ForegroundColor Gray

# ── 0. preconditions ─────────────────────────────────────────────────────────
Write-Step 0 'Checking preconditions'

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Exit-WithError "PowerShell $($PSVersionTable.PSVersion) is too old" 'Use Windows PowerShell 5.1 or PowerShell 7+'
}
if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Exit-WithError 'This script targets Windows' 'WizTree has no Linux or macOS build to drive'
}
Write-Ok "PowerShell $($PSVersionTable.PSVersion)"

$cli = Get-Command 'cli-anything-wiztree' -ErrorAction SilentlyContinue
if (-not $cli) {
    Write-Warn2 'cli-anything-wiztree is not on PATH'
    Write-Info 'Install it first, from the repository root:'
    Write-Info '    pip install .'
    Write-Info 'Then re-run this script.'
    exit 1
}
Write-Ok "CLI found: $($cli.Source)"

# ── 1. is WizTree already usable? ────────────────────────────────────────────
Write-Step 1 'Looking for an existing WizTree'

$existing = $null
try {
    $probe = & cli-anything-wiztree --json info 2>$null | Out-String
    $report = $probe | ConvertFrom-Json
    if ($report.found -and -not $Force) {
        $existing = $report
        Write-Ok "Found WizTree $($report.version)"
        Write-Info "path:   $($report.exe)"
        Write-Info "source: $($report.source)"
    } elseif ($report.found -and $Force) {
        Write-Info "-Force given; ignoring the existing install and re-downloading"
    }
} catch {
    Write-Info 'No usable WizTree reported by `info`'
}

if ($existing) {
    Write-Host ''
    Write-Host '  WizTree is already installed and bound — nothing to download.' -ForegroundColor Green
    Write-Host '  Re-run with -Force to download a fresh portable copy anyway.' -ForegroundColor DarkGray
    $alreadyGood = $true
} else {
    $alreadyGood = $false
}

# ── 2. download ──────────────────────────────────────────────────────────────
$wizExe = $null

if (-not $alreadyGood) {
    Write-Step 2 'Downloading WizTree from the vendor'

    if ($SetupExe) {
        # ---- installer route -------------------------------------------------
        Write-Info 'Resolving the installer URL from the download page…'
        try {
            $page = Invoke-WebRequest -Uri $VendorPage -UseBasicParsing -TimeoutSec 30
        } catch {
            Exit-WithError "Could not reach $VendorPage" $_.Exception.Message
        }
        $m = [regex]::Match($page.Content, 'href="([^"]*_setup\.exe)"')
        if (-not $m.Success) {
            Exit-WithError 'Could not find the installer link on the download page' `
                           'The vendor may have changed the page layout; download it manually from the URL above.'
        }
        $url = $VendorBase + $m.Groups[1].Value.TrimStart('/')
        $tmp = Join-Path $env:TEMP ("wiztree_setup_{0}.exe" -f [guid]::NewGuid().ToString('N'))
        Write-Info "downloading $url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 300
        } catch {
            Exit-WithError 'Download failed' $_.Exception.Message
        }
        Write-Ok ("downloaded {0:N1} MB" -f ((Get-Item $tmp).Length / 1MB))
        Write-Info 'Running the installer silently (a UAC prompt may appear)…'
        $p = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList `
             '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-'
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0) {
            Write-Warn2 "Installer exited with code $($p.ExitCode)"
        }
        # let discovery find whatever the installer wrote
        foreach ($c in @(
            "$env:ProgramFiles\WizTree\WizTree64.exe",
            "${env:ProgramFiles(x86)}\WizTree\WizTree64.exe",
            "$env:LOCALAPPDATA\Programs\WizTree\WizTree64.exe"
        )) {
            if (Test-Path $c) { $wizExe = $c; break }
        }
        if (-not $wizExe) {
            Exit-WithError 'Installer finished but WizTree64.exe was not found where expected' `
                           'Re-run this script and pass -InstallDir, or bind manually with --exe'
        }
    }
    else {
        # ---- portable route (default) ---------------------------------------
        Write-Info 'Resolving the portable URL from the download page…'
        try {
            $page = Invoke-WebRequest -Uri $VendorPage -UseBasicParsing -TimeoutSec 30
        } catch {
            Exit-WithError "Could not reach $VendorPage" $_.Exception.Message
        }
        $m = [regex]::Match($page.Content, 'href="([^"]*_portable\.zip)"')
        if (-not $m.Success) {
            Exit-WithError 'Could not find the portable link on the download page' `
                           'The vendor may have changed the page layout; download it manually from the URL above.'
        }
        $url = $VendorBase + $m.Groups[1].Value.TrimStart('/')
        $zip = Join-Path $env:TEMP ("wiztree_portable_{0}.zip" -f [guid]::NewGuid().ToString('N'))

        Write-Info "downloading $url"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 300
        } catch {
            Exit-WithError 'Download failed' $_.Exception.Message
        }
        $sw.Stop()
        Write-Ok ("downloaded {0:N1} MB in {1:N1}s" -f ((Get-Item $zip).Length / 1MB), $sw.Elapsed.TotalSeconds)

        Write-Info "extracting to $InstallDir"
        if (Test-Path $InstallDir) {
            Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        try {
            Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
        } catch {
            Exit-WithError 'Extraction failed' $_.Exception.Message
        }
        Remove-Item $zip -Force -ErrorAction SilentlyContinue

        $wizExe = Join-Path $InstallDir 'WizTree64.exe'
        if (-not (Test-Path $wizExe)) {
            # some builds ship only the 32-bit launcher
            $alt = Join-Path $InstallDir 'WizTree.exe'
            if (Test-Path $alt) { $wizExe = $alt } else {
                Exit-WithError 'WizTree64.exe not found after extraction' "Inspect $InstallDir"
            }
        }
        Write-Ok "extracted: $wizExe"
    }

    $ver = (Get-Item $wizExe).VersionInfo.FileVersion
    Write-Info "version: $ver"
}

# ── 3. bind ──────────────────────────────────────────────────────────────────
Write-Step 3 'Binding WizTree to the CLI'

if ($alreadyGood) {
    Write-Ok "already bound via source '$($existing.source)' — nothing to do"
} else {
    $bindOut = & cli-anything-wiztree --exe $wizExe --json info 2>&1 | Out-String
    try {
        $b = $bindOut | ConvertFrom-Json
    } catch {
        Exit-WithError 'Could not parse `info` output' $bindOut
    }
    if (-not $b.found) {
        Exit-WithError 'The downloaded binary was not accepted by `info`' $bindOut
    }
    Write-Ok "bound: $($b.exe)"
    Write-Info "version: $($b.version)"
    if ($b.remembered) {
        Write-Info "saved to $($b.config_path) — later commands need no --exe"
    } else {
        Write-Info 'config already pointed here'
    }
}

# ── 4. verify ────────────────────────────────────────────────────────────────
Write-Step 4 'Verifying'

$final = & cli-anything-wiztree --json info 2>&1 | Out-String
try { $f = $final | ConvertFrom-Json } catch { Exit-WithError 'Could not parse `info`' $final }

if (-not $f.found) {
    Exit-WithError 'info reports found: false after binding' $final
}
Write-Ok "found: true   source: $($f.source)   version: $($f.version)"

if ($f.is_admin) {
    Write-Ok 'running elevated — index build/sync available'
} else {
    Write-Info 'not elevated (normal). `index build` and `index sync` need an admin shell:'
    Write-Info '    Start-Process pwsh -Verb RunAs'
}

if (-not $SkipScan) {
    Write-Info 'running a small real scan as a smoke test…'
    $testDir = $env:TEMP
    $csv = Join-Path $env:TEMP ("wiztree_setup_test_{0}.csv" -f [guid]::NewGuid().ToString('N'))
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $scanOut = & cli-anything-wiztree --exe $f.exe --json scan run $testDir --folders-only -o $csv 2>&1 | Out-String
    $sw.Stop()
    try {
        $s = $scanOut | ConvertFrom-Json
        Write-Ok ("scan works: {0:N0} folders in {1:N1}s" -f $s.entry_count, $sw.Elapsed.TotalSeconds)
    } catch {
        Write-Warn2 "smoke-test scan did not return JSON — output follows:"
        Write-Info $scanOut
    }
    Remove-Item $csv, "$csv.wiztree-index.pkl" -Force -ErrorAction SilentlyContinue
}

# ── done ─────────────────────────────────────────────────────────────────────
Write-Host @'

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

  To move or unbind WizTree later, edit:

    <state home>\config.json          (default: %USERPROFILE%\.cli-anything-wiztree)

'@ -ForegroundColor Gray
