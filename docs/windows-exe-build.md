# Building native Windows .exe files

`build_windows_exes.ps1` builds two standalone executables with
PyInstaller, each wrapping one of the Windows-native entry points:

| Exe | Entry point | Mode | Needs a console? |
|---|---|---|---|
| `AquariusManager.exe` | `manager.py` | `--onefile` | Yes (CLI/service) |
| `AquariusControlPanel.exe` | `gui_win.py` | `--onedir` | No |

(`tray_win.py` is still in the repo and can be run from source, but it's
not packaged/distributed as `AquariusTray.exe` anymore — see issue #4
below for why.)

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
   does this for the NSSM-managed service. (`manager.py`'s `load_config()`
   also self-initializes an empty `instances.json` on Windows if the path
   doesn't exist at all, so a fresh install never needs this run manually —
   see its own IS_WINDOWS branch.)

4. **Unsigned PyInstaller exes get flagged as malware by Windows Defender.**
   `AquariusTray.exe` was quarantined as `Trojan:Win32/Bearfoos.A!ml` — a
   documented ML-heuristic false positive (confirmed via research: PyInstaller's
   own docs name "antivirus false positives from the wide-spread use of
   pre-compiled bootloaders" as an explicit, official reason to compile a
   custom bootloader; a near-identical real case,
   [microsoft/apm#487](https://github.com/microsoft/apm/issues/487), hit the
   same detection family one letter off — `Bearfoos.B!ml`). Real, documented,
   zero-cost mitigations applied here:
   - **`--version-file`** on both remaining exes (`version_manager.txt`,
     `version_controlpanel.txt`) embeds real company/product/version PE
     metadata — an ML classifier has nothing to weigh against the generic
     packed-exe shape when this is totally absent; this was the #1 fix in
     the `microsoft/apm` case above.
   - **UPX disabled** (confirmed not in use on this build machine — UPX
     compression is independently documented, via a controlled SANS ISC
     test, to trigger AV false positives on its own; if a future build
     environment has UPX on PATH, pass `--noupx` explicitly rather than
     letting PyInstaller auto-enable it).
   - **`AquariusTray.exe` was dropped from the distributed product
     entirely** rather than chasing the false positive further — it was a
     "nice to have," not core functionality, and removing one of three exes
     also shrinks the overall AV-scrutiny surface of the installer.
   - **Not yet done, real fix if this resurfaces**: Authenticode code
     signing (e.g. SSL.com's ~$129/yr Individual Validated tier, or Azure
     Artifact Signing's ~$10/mo cloud-signing option) is the one mitigation
     every source converges on as actually reliable — the free measures
     above reduce risk, they don't eliminate it.

## Verifying a build

A quick end-to-end check after building:

```powershell
# Control panel should launch and stay running (check with Get-Process
# after a few seconds — no crash, no silent exit).
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
