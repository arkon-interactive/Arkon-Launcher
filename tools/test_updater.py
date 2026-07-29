"""Check the auto-updater against the real GitHub release.

    python tools/test_updater.py            # read-only checks
    python tools/test_updater.py --download # also fetch the installer

Read-only by default: it queries the API, verifies the release is shaped the way
the updater needs, and reports what the app would do. `--download` additionally
pulls the installer and size-checks it, without running anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arkon_launcher import __version__, updater  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the auto-updater.")
    parser.add_argument("--download", action="store_true", help="also fetch the installer")
    arguments = parser.parse_args()

    print(f"Installed version: {__version__}")
    print(f"Repository       : {updater.REPOSITORY}\n")

    print("=== Version comparison")
    passed = True
    for candidate, current, expected in (
        ("0.7.0", "0.6.1", True),
        ("0.6.1", "0.7.0", False),
        ("0.7.0", "0.7.0", False),
        ("v0.8.0", "0.7.0", True),
        ("0.10.0", "0.9.0", True),  # numeric, not lexical
    ):
        result = updater.is_newer(candidate, current)
        passed &= check(f"{candidate} newer than {current} -> {result}", result == expected)

    print("\n=== Asset matching")
    for name, expected in (
        ("ArkonLauncherSetup.exe", True),
        ("ArkonLauncher-0.7.0-Setup.exe", True),
        ("arkon_launcher.zip", False),
        ("SomethingElse.exe", False),
    ):
        matched = bool(updater.INSTALLER_PATTERN.search(name))
        passed &= check(f"{name} -> {matched}", matched == expected)

    print("\n=== Live release")
    release = updater.fetch_latest()
    if release is None:
        print("  No release published yet (or GitHub unreachable).")
        print("\n  Publish one, then run this again. Until then the app correctly")
        print("  does nothing on startup.")
        return 0 if passed else 1

    print(f"  tag        : {release.tag}")
    print(f"  version    : {release.version}")
    print(f"  url        : {release.url}")
    print(f"  asset      : {release.asset_name or '(none attached)'}")
    print(f"  size       : {release.asset_size / 1024**2:.1f} MB" if release.asset_size else "")

    passed &= check(
        "release has an installer attached",
        release.has_installer,
        "without it the app can only link to the releases page",
    )
    passed &= check(
        "version parses to something comparable",
        updater.version_tuple(release.version) != (0, 0, 0),
        f"parsed as {updater.version_tuple(release.version)}",
    )

    print("\n=== What the app would do")
    if updater.is_newer(release.version):
        print(f"  Offer the update: {updater.installed_version()} -> {release.version}")
    else:
        print(
            f"  Nothing - {updater.installed_version()} is already "
            f"{'newer than' if updater.version_tuple(updater.installed_version()) > updater.version_tuple(release.version) else 'the same as'} "
            f"{release.version}"
        )
        print("\n  To see the real update prompt, run the app with an older")
        print("  version simulated:")
        print("\n      set ARKON_FAKE_VERSION=0.0.1")
        print('      "dist\\Arkon Launcher\\Arkon Launcher.exe"')

    if arguments.download and release.has_installer:
        print("\n=== Downloading (not running it)")
        try:
            path = updater.download_installer(release, lambda m: print(f"  {m}", end="\r"))
            print(f"\n  saved to {path}")
            passed &= check(
                "size matches what GitHub advertised",
                path.stat().st_size == release.asset_size,
                f"{path.stat().st_size:,} bytes",
            )
        except updater.UpdateError as exc:
            passed &= check("download", False, str(exc))

    print("\nAll checks passed." if passed else "\nSome checks failed.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
