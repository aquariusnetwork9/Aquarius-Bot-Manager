<#
Builds the two native Windows .exe deliverables with PyInstaller:
  - AquariusManager.exe       (manager.py -- CLI/service, console app)
  - AquariusControlPanel.exe  (gui_win.py -- the native control panel)

(tray_win.py/AquariusTray.exe was dropped -- it was the one component that
got Defender-quarantined as a false positive, "nice to have" per the user,
not worth the AV friction. tray_win.py itself still exists in the repo for
anyone who wants to run it unpackaged from source; it's just not part of
the installed/distributed product anymore.)

Three real issues this script works around, found and confirmed while
building these by hand (see each section's comment for how each was
diagnosed):

1. PySide6's onedir/onefile bundle collects multiple copies of
   VCRUNTIME140.dll (one from PySide6's own wheel, one from shiboken6's, and
   PyInstaller additionally pulls in a THIRD, OLDER copy from elsewhere on
   this machine into the bundle's top-level _internal/ directory). Windows'
   DLL search order can resolve Qt6Core.dll's dependency to the wrong,
   older copy, which crashes with an 0xc0000005 access violation deep
   inside Qt6Core a few seconds after launch -- reproduced consistently in
   both --onefile and --onedir modes, confirmed via Event Viewer's
   Application Error log pointing at the exact same faulting offset both
   times. Fix: use --onedir (required for the file-removal step below to
   be possible at all -- --onefile re-extracts a fresh copy every launch,
   so there's nothing to durably remove), then delete the stale top-level
   copy so the loader falls through to the correct versioned copies
   sitting next to Qt6Core.dll itself in PySide6/.

2. manager.py's Windows helper-process model (see platform_win.py) spawns
   `python.exe platform_win.py _helper <name> <config>` for each
   supervised bot. A frozen AquariusManager.exe has no real python.exe to
   call (sys.executable IS the frozen exe) and no standalone
   platform_win.py file to hand it. Fix: bundle a minimal real Python
   runtime (the official "embeddable" distribution) plus a copy of
   platform_win.py in a pyrt/ folder next to AquariusManager.exe;
   platform_win.py's _helper_launch_args() detects frozen mode
   (getattr(sys, "frozen", False)) and points at this bundle instead of
   sys.executable/__file__.

3. manager.py's DEFAULT_CONFIG resolves relative to SCRIPT_DIR
   (os.path.dirname(os.path.abspath(__file__))), which for a PyInstaller
   --onefile build resolves to the BOOTLOADER'S TEMP EXTRACTION DIRECTORY
   (_MEI...), not the real exe's folder -- confirmed by running
   AquariusManager.exe sysinfo with no --config/ABM_CONFIG set and seeing
   it look for instances.json inside a _MEI... path. This only matters if
   you run the exe with no explicit config path; always pass --config (or
   set ABM_CONFIG) when running AquariusManager.exe, same as the NSSM
   service install script already does.

Requires: pyinstaller (pip install pyinstaller), and psutil/PySide6-
Essentials/PySide6-Addons/PySide6/pystray already installed in THIS
Python environment (see install_windows_deps.py) -- PyInstaller bundles
whatever's importable in the environment it runs in.

Usage:
    .\build_windows_exes.ps1
#>

param(
    [string]$DistPath = (Join-Path $PSScriptRoot "dist"),
    [string]$PythonEmbedVersion = "3.12.10"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Resolve-RealPython {
    $candidates = Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\WindowsApps\python.exe" }
    if ($candidates) { return $candidates[0].Source }
    throw "No real python.exe found on PATH."
}

$python = Resolve-RealPython
Write-Host "Using Python: $python"

# ---------------------------------------------------------------------------
# 1. Control panel -- onedir (see issue #1 above for why), then strip the
#    stale top-level VCRUNTIME140 copies so the loader resolves the correct
#    PySide6-local ones. --version-file embeds real PE metadata
#    (company/product/version) -- an unsigned PyInstaller exe with NO
#    identifying metadata is exactly the shape antivirus ML heuristics key
#    on; this was the #1 fix in a near-identical real false-positive case
#    (see version_controlpanel.txt's own comment for the citation).
# ---------------------------------------------------------------------------
Write-Host "`n=== Building AquariusControlPanel.exe ==="
& $python -m PyInstaller --onedir --windowed --name AquariusControlPanel --distpath $DistPath `
    --version-file version_controlpanel.txt -y gui_win.py

$internalDir = Join-Path $DistPath "AquariusControlPanel\_internal"
foreach ($dll in @("VCRUNTIME140.dll", "VCRUNTIME140_1.dll")) {
    $stale = Join-Path $internalDir $dll
    if (Test-Path $stale) {
        Remove-Item $stale -Force
        Write-Host "Removed stale $dll from _internal/ (correct copies remain in PySide6/ and shiboken6/)"
    }
}

# ---------------------------------------------------------------------------
# 2. Manager -- onefile (fine here; no PySide6 involved), plus the bundled
#    pyrt/ runtime so its own _helper subprocess launch works when frozen.
#    --version-file, same reasoning as AquariusControlPanel.exe above.
# ---------------------------------------------------------------------------
Write-Host "`n=== Building AquariusManager.exe ==="
& $python -m PyInstaller --onefile --console --name AquariusManager --distpath $DistPath `
    --version-file version_manager.txt -y manager.py

$pyrtDir = Join-Path $DistPath "pyrt"
if (-not (Test-Path (Join-Path $pyrtDir "python.exe"))) {
    Write-Host "`n--- Setting up bundled helper runtime (pyrt/) ---"
    New-Item -ItemType Directory -Force -Path $pyrtDir | Out-Null
    $zipPath = Join-Path $env:TEMP "python-embed-$PythonEmbedVersion.zip"
    if (-not (Test-Path $zipPath)) {
        $url = "https://www.python.org/ftp/python/$PythonEmbedVersion/python-$PythonEmbedVersion-embed-amd64.zip"
        Write-Host "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $zipPath
    }
    Expand-Archive -Path $zipPath -DestinationPath $pyrtDir -Force

    # Enable `import site` so pip (and psutil) can be installed into it --
    # the embeddable distro ships this commented out by default.
    $pthFile = Get-ChildItem $pyrtDir -Filter "python3*._pth" | Select-Object -First 1
    (Get-Content $pthFile.FullName) -replace '^#import site$', 'import site' | Set-Content $pthFile.FullName

    $getPip = Join-Path $pyrtDir "get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
    & (Join-Path $pyrtDir "python.exe") $getPip --no-warn-script-location
    & (Join-Path $pyrtDir "python.exe") -m pip install --no-warn-script-location psutil
    Remove-Item $getPip -Force
} else {
    Write-Host "`npyrt/ runtime already set up, skipping download."
}
Copy-Item (Join-Path $PSScriptRoot "platform_win.py") (Join-Path $pyrtDir "platform_win.py") -Force

Write-Host "`n=== Build complete ==="
Write-Host "AquariusControlPanel.exe:  $(Join-Path $DistPath 'AquariusControlPanel\AquariusControlPanel.exe')"
Write-Host "AquariusManager.exe:       $(Join-Path $DistPath 'AquariusManager.exe')  (needs pyrt/ alongside it)"
Write-Host "`nAlways pass --config <path to instances.json> when running AquariusManager.exe (see issue #3's comment above)."
