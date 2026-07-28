"""Build Arkon Launcher: PyInstaller bundle, then the Inno Setup installer.

    python build.py           # both stages
    python build.py --exe     # PyInstaller only
    python build.py --clean   # remove build artifacts first

Inno Setup is a free, separate install; the PyInstaller stage works without it,
so a build machine without ISCC.exe can still produce a runnable app folder.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "Arkon Launcher"

# Inno Setup lands in different places depending on how it was installed:
# the classic per-machine installer uses Program Files, while winget defaults to
# a per-user install under %LOCALAPPDATA%\Programs.
ISCC_CANDIDATES = tuple(
    Path(base) / "Inno Setup 6" / "ISCC.exe"
    for base in (
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs",
        Path.home() / "AppData/Local/Programs",
    )
)


def app_version() -> str:
    namespace: dict = {}
    exec((ROOT / "arkon_launcher" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return namespace.get("__version__", "0.0.0")


def find_iscc() -> Path | None:
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    return Path(found) if found else None


def clean() -> None:
    for directory in (DIST, BUILD):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            print(f"removed {directory}")
    for spec in ROOT.glob("*.spec"):
        spec.unlink()


def build_exe() -> Path:
    icon = ROOT / "installer" / "app.ico"
    if not icon.is_file():
        print("No icon yet; generating one...")
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        # --onedir, not --onefile: the app is installed rather than run from
        # Downloads, so avoiding a re-extract to %TEMP% on every launch is worth
        # more than a single-file layout.
        "--onedir",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        str(icon),
        "--add-data",
        f"{ROOT / 'arkon_launcher' / 'data'}{os.pathsep}data",
        # PySide6 pulls in a great deal that this app never touches.
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "PySide6.Qt3DCore",
        "--exclude-module",
        "PySide6.QtMultimedia",
        "--exclude-module",
        "PySide6.QtQuick",
        "--exclude-module",
        "PySide6.QtQml",
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "numpy",
        # Point at the wrapper, not the package's __main__: PyInstaller runs its
        # target as a top-level script, where relative imports have no parent
        # package and fail at startup.
        str(ROOT / "arkon.py"),
    ]

    print("Running PyInstaller...")
    subprocess.run(command, check=True, cwd=ROOT)

    produced = DIST / APP_NAME / f"{APP_NAME}.exe"
    if not produced.is_file():
        raise SystemExit(f"PyInstaller did not produce {produced}")

    size = sum(f.stat().st_size for f in (DIST / APP_NAME).rglob("*") if f.is_file())
    print(f"Built {produced} ({size / 1024**2:.0f} MB total)")
    return produced


def build_installer() -> Path | None:
    iscc = find_iscc()
    if iscc is None:
        print(
            "\nInno Setup (ISCC.exe) not found, so the installer was not built.\n"
            "The app folder in dist/ is complete and runnable.\n"
            "Install Inno Setup 6 from https://jrsoftware.org/isdl.php to produce\n"
            "ArkonLauncherSetup.exe."
        )
        return None

    print(f"Running {iscc}...")
    subprocess.run(
        [str(iscc), f"/DAppVersion={app_version()}", str(ROOT / "installer" / "arkon.iss")],
        check=True,
        cwd=ROOT / "installer",
    )

    setup = DIST / "ArkonLauncherSetup.exe"
    if setup.is_file():
        print(f"Built {setup} ({setup.stat().st_size / 1024**2:.0f} MB)")
        return setup
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Arkon Launcher.")
    parser.add_argument("--clean", action="store_true", help="remove build artifacts first")
    parser.add_argument("--exe", action="store_true", help="skip the installer stage")
    arguments = parser.parse_args()

    if arguments.clean:
        clean()

    build_exe()
    if not arguments.exe:
        build_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
