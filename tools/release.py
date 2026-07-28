"""Cut a release: check, build, tag, publish.

    python tools/release.py            # full release of the current version
    python tools/release.py --check    # validate only, change nothing
    python tools/release.py --no-publish  # build and tag, don't touch GitHub

The order is deliberate. Everything that can fail cheaply is checked *before*
anything irreversible happens, because an aborted release that already pushed a
tag is far more annoying to clean up than one that refused to start:

    validate -> build -> smoke-test the exe -> commit -> tag -> push -> publish

Publishing needs the GitHub CLI (`gh`) authenticated once with `gh auth login`.
Without it the script still does everything local and prints exactly what to do
by hand.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INSTALLER = ROOT / "dist" / "ArkonLauncherSetup.exe"
APP_EXE = ROOT / "dist" / "Arkon Launcher" / "Arkon Launcher.exe"
CHANGELOG = ROOT / "CHANGELOG.md"
REPOSITORY = "arkon-interactive/Arkon-Launcher"


class ReleaseError(Exception):
    pass


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def step(message: str) -> None:
    print(f"\n=== {message}", flush=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True, **kwargs)


def current_version() -> str:
    namespace: dict = {}
    exec((ROOT / "arkon_launcher" / "__init__.py").read_text(encoding="utf-8"), namespace)
    version = namespace.get("__version__")
    if not version:
        raise ReleaseError("No __version__ in arkon_launcher/__init__.py")
    return str(version)


def find_gh() -> Path | None:
    found = shutil.which("gh")
    if found:
        return Path(found)
    for candidate in (
        Path(r"C:\Program Files\GitHub CLI\gh.exe"),
        Path(r"C:\Program Files (x86)\GitHub CLI\gh.exe"),
        Path.home() / "AppData/Local/GitHubCLI/gh.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def gh_authenticated(gh: Path) -> bool:
    return subprocess.run(
        [str(gh), "auth", "status"], capture_output=True, text=True
    ).returncode == 0


# --- Validation ---------------------------------------------------------------


def changelog_section(version: str) -> str:
    """The notes for this version, which become the release body."""
    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(version)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    if not match:
        raise ReleaseError(
            f"CHANGELOG.md has no '## {version}' section. Add one before releasing."
        )
    body = match.group(1).strip()
    if not body:
        raise ReleaseError(f"The '## {version}' changelog section is empty.")
    return body


def validate(version: str) -> str:
    step(f"Validating {version}")

    notes = changelog_section(version)
    say(f"changelog section found ({len(notes.splitlines())} lines)")

    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        raise ReleaseError(
            "Working tree is dirty. Commit or stash first:\n"
            + "\n".join(f"    {line}" for line in status.splitlines()[:10])
        )
    say("working tree clean")

    existing = run(["git", "tag", "-l", f"v{version}"]).stdout.strip()
    if existing:
        raise ReleaseError(
            f"Tag v{version} already exists. Bump __version__ before releasing again."
        )
    say(f"tag v{version} is free")

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    say(f"on branch {branch}")

    return notes


# --- Build --------------------------------------------------------------------


def build() -> None:
    step("Building")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "build.py"), "--clean"], cwd=str(ROOT)
    )
    if completed.returncode != 0:
        raise ReleaseError("build.py failed.")
    if not INSTALLER.is_file():
        raise ReleaseError(f"No installer produced at {INSTALLER}")
    say(f"{INSTALLER.name} ({INSTALLER.stat().st_size / 1024**2:.0f} MB)")


def smoke_test() -> None:
    """Start the packaged app and make sure it stays up.

    A release that ships an exe which dies on launch is the one mistake worth
    spending twenty seconds to rule out - and it has happened here before, when
    PyInstaller's entry point broke every relative import.
    """
    step("Smoke-testing the packaged app")
    if not APP_EXE.is_file():
        raise ReleaseError(f"No packaged exe at {APP_EXE}")

    process = subprocess.Popen(
        [str(APP_EXE)],
        cwd=str(APP_EXE.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(20)
        if process.poll() is not None:
            raise ReleaseError(
                f"The packaged app exited on its own (code {process.returncode}). "
                f"Run it directly to see why."
            )
        say("still running after 20s")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


# --- Publish ------------------------------------------------------------------


def tag_and_push(version: str) -> None:
    step(f"Tagging v{version}")
    tag = run(["git", "tag", "-a", f"v{version}", "-m", f"Arkon Launcher {version}"])
    if tag.returncode != 0:
        raise ReleaseError(f"Could not create tag: {tag.stderr.strip()}")
    say(f"created v{version}")

    step("Pushing")
    for arguments in (["git", "push", "origin", "HEAD"], ["git", "push", "origin", f"v{version}"]):
        pushed = run(arguments)
        if pushed.returncode != 0:
            raise ReleaseError(
                f"{' '.join(arguments)} failed:\n{pushed.stderr.strip()}\n\n"
                f"The tag exists locally; delete it with "
                f"`git tag -d v{version}` if you want to start over."
            )
        say(" ".join(arguments[1:]))


def publish(version: str, notes: str, gh: Path) -> None:
    step(f"Publishing release v{version}")
    notes_file = ROOT / "dist" / "release-notes.md"
    notes_file.write_text(notes, encoding="utf-8")

    completed = subprocess.run(
        [
            str(gh), "release", "create", f"v{version}",
            str(INSTALLER),
            "--repo", REPOSITORY,
            "--title", version,
            "--notes-file", str(notes_file),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ReleaseError(f"gh release create failed:\n{completed.stderr.strip()}")
    say((completed.stdout or "").strip() or "published")


def manual_instructions(version: str) -> None:
    print(
        f"\n--- Publish by hand ---------------------------------------------\n"
        f"The GitHub CLI isn't available or isn't signed in, so the release\n"
        f"itself wasn't created. Everything else is done and pushed.\n\n"
        f"Either install and authenticate it once:\n\n"
        f"    winget install --id GitHub.cli\n"
        f"    gh auth login\n"
        f"    python tools/release.py --publish-only\n\n"
        f"Or do it in the browser:\n\n"
        f"    https://github.com/{REPOSITORY}/releases/new?tag=v{version}\n\n"
        f"    Title:  {version}\n"
        f"    Notes:  the '## {version}' section of CHANGELOG.md\n"
        f"    Attach: {INSTALLER}\n"
        f"-----------------------------------------------------------------"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Cut an Arkon Launcher release.")
    parser.add_argument("--check", action="store_true", help="validate only")
    parser.add_argument("--no-publish", action="store_true", help="build and tag only")
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help="publish an already-built, already-tagged version",
    )
    parser.add_argument("--skip-build", action="store_true", help="reuse dist/")
    arguments = parser.parse_args()

    try:
        version = current_version()
        print(f"Arkon Launcher {version}")

        gh = find_gh()
        authenticated = bool(gh and gh_authenticated(gh))

        if arguments.publish_only:
            notes = changelog_section(version)
            if not authenticated:
                manual_instructions(version)
                return 1
            publish(version, notes, gh)
            return 0

        notes = validate(version)
        if arguments.check:
            print("\nAll checks passed. Nothing was changed.")
            if not authenticated:
                say("note: gh is not signed in, so publishing would need doing by hand")
            return 0

        if not arguments.skip_build:
            build()
        smoke_test()
        tag_and_push(version)

        if arguments.no_publish:
            print("\nTagged and pushed. Skipping the GitHub release as asked.")
            return 0

        if authenticated:
            publish(version, notes, gh)
            print(f"\nReleased {version}.")
        else:
            manual_instructions(version)
            return 1
        return 0

    except ReleaseError as exc:
        print(f"\nRelease stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
