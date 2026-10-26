# Building native Windows .exe files

`build_windows_exes.ps1` builds three standalone executables with
PyInstaller, each wrapping one of the Windows-native entry points:

| Exe | Entry point | Mode | Needs a console? |
|---|---|---|---|
| `AquariusManager.exe` | `manager.py` | `--onefile` | Yes (CLI/service) |
| `AquariusControlPanel.exe` | `gui_win.py` | `--onedir` | No |
| `AquariusTray.exe` | `tray_win.py` | `--onefile` | No |

Run it from the repo root, with the project's Windows dependencies already
installed in your Python environment (see `install_windows_deps.py`):

```powershell
.\build_windows_exes.ps1
```

Output lands in `dist/` (gitignored — rebuild rather than pull from
history).

## Three real issues this script works around

These were each found by building by hand and watching it fail, not
theorized in advance — see `build_windows_exes.ps1`'s own comments for the
full diagnosis of each.

1. **PySide6 + PyInstaller DLL version conflict.** The control panel's
   bundle ends up with two different versions of `VCRUNTIME140.dll`: the
   correct one (matching Qt6Core.dll) inside `PySide6/`, and a second,
   older one PyInstaller pulls into the bundle's top-level `_internal/`
   directory from elsewhere on the build machine. Windows' DLL search
   order can resolve the wrong one, crashing with an access violation
   (`0xc0000005`) inside `Qt6Core.dll` a few seconds after launch —
   reproduced consistently via Windows Event Viewer's Application Error
   log, same faulting offset every time. Fixed by building `--onedir`
   (required so there's a persistent folder to edit at all) and deleting
   the stale top-level copy after the build, so the loader falls through
   to the correct one next to `Qt6Core.dll`.

2. **The Windows helper-process model needs a real interpreter.**
   `platform_win.py` supervises each bot through a detached helper process
   (`python.exe platform_win.py _helper <name> <config>` — see that file's
   own module docstring for why). A frozen `AquariusManager.exe` has no
   real `python.exe` to spawn (`sys.executable` IS the frozen exe) and no
   standalone `platform_win.py` file to hand it. Fixed by bundling a
   minimal real Python runtime — the official "embeddable" distribution —
   plus a copy of `platform_win.py`, in a `pyrt/` folder the build script
   creates next to `AquariusManager.exe`. `platform_win.py`'s
   `_helper_launch_args()` detects frozen mode
   (`getattr(sys, "frozen", False)`) and points at this bundle instead of
   `sys.executable`/`__file__`.

3. **`--config` is required when running `AquariusManager.exe` directly.**
   `manager.py`'s default config path resolves relative to the script's
   own directory, which for a PyInstaller `--onefile` build is the
   bootloader's temporary extraction folder (`_MEI...`), not the real
   `.exe`'s folder — confirmed by running `AquariusManager.exe sysinfo`
   with no config set and watching it look for `instances.json` inside a
   `_MEI...` path. Always pass `--config <path>` (or set `ABM_CONFIG`)
   when running the exe directly; `install_windows_service.ps1` already
   does this for the NSSM-managed service.

## Verifying a build

A quick end-to-end check after building:

```powershell
# Tray + control panel should both launch and stay running (check with
# Get-Process after a few seconds — no crash, no silent exit).
Start-Process .\dist\AquariusTray.exe
Start-Process .\dist\AquariusControlPanel\AquariusControlPanel.exe

# Manager: full lifecycle against a real config.
.\dist\AquariusManager.exe --config <path to instances.json> start <instance>
.\dist\AquariusManager.exe --config <path to instances.json> status
.\dist\AquariusManager.exe --config <path to instances.json> stop <instance>
```

If `AquariusManager.exe start` reports `error: helper process exited before
starting`, check that `pyrt/python.exe` and `pyrt/platform_win.py` actually
exist next to `AquariusManager.exe` (issue #2 above) — the build script sets
this up automatically, but a hand-copied `dist/` folder can lose it.
