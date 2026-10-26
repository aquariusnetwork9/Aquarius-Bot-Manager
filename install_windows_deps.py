"""
Installs this project's Windows-only dependencies (see requirements.txt).

Plain `pip install -r requirements.txt` is not enough for PySide6: pip has
no way to say "skip this one line's own dependencies" inside a requirements
file, and PySide6-Essentials alone leaves a PySide6 namespace directory with
no __init__.py (DLL path setup happens there) -- QtWidgets fails to import
with a DLL load error until the real PySide6 metapackage is also installed.
Installing the metapackage normally pulls PySide6_WebEngine (an embedded
Chromium browser, ~130MB) as a hard, non-optional dependency, which this
project deliberately does not use (see gui_win.py's module docstring) -- so
the metapackage is installed with --no-deps, relying on Essentials/Addons
(installed first, normally) to provide everything it would otherwise pull in.

Usage:
    python install_windows_deps.py
"""

import subprocess
import sys


def pip_install(*args):
    cmd = [sys.executable, "-m", "pip", "install", *args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    if sys.platform != "win32":
        print("This installer is Windows-only; nothing to do here.")
        return
    pip_install("psutil>=5.9", "pystray>=0.19",
                "PySide6-Essentials==6.12.0", "PySide6-Addons==6.12.0")
    pip_install("--no-deps", "PySide6==6.12.0")
    print("\nDone. Verify with: python -c \"from PySide6 import QtWidgets; print('ok')\"")


if __name__ == "__main__":
    main()
