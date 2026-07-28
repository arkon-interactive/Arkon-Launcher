# Publishing a release

One command does the whole thing:

```bash
python tools/release.py
```

It runs in an order chosen so everything that can fail cheaply fails *before*
anything irreversible happens — an aborted release that already pushed a tag is
far more annoying to clean up than one that refused to start:

| Step | What it checks or does |
|---|---|
| **Validate** | `CHANGELOG.md` has a section for this version; working tree is clean; the tag doesn't already exist |
| **Build** | `build.py --clean` → `dist/ArkonLauncherSetup.exe` |
| **Smoke-test** | Launches the packaged exe and confirms it's still alive 20s later |
| **Tag** | `v<version>`, annotated |
| **Push** | Commits, then the tag |
| **Publish** | Creates the GitHub release with the installer attached |

The smoke test earns its twenty seconds: a build that dies on launch has shipped
from here before, when PyInstaller's entry point broke every relative import.

## Before you run it

1. **Bump the version** in `arkon_launcher/__init__.py`. That one value flows
   into the window title, the installer, and Add/Remove Programs.
2. **Add a `## <version>` section to `CHANGELOG.md`.** The release refuses to
   start without one, and its contents become the release notes.
3. **Commit everything.** A dirty tree stops the release.

## Options

```bash
python tools/release.py --check         # validate only, change nothing
python tools/release.py --no-publish    # build, tag and push; no GitHub release
python tools/release.py --publish-only  # publish an already-tagged version
python tools/release.py --skip-build    # reuse whatever is in dist/
```

`--check` is worth running any time — it's read-only and takes a second.

## One-time setup

Publishing needs the GitHub CLI signed in. It's installed already; the login is
interactive, so it has to be run from a real terminal:

```bash
gh auth login
```

Choose GitHub.com → HTTPS → authenticate in browser. After that
`python tools/release.py` works end to end.

Without it, everything local still happens and the script prints the manual
steps.

## Publishing by hand

If you'd rather not use the CLI:

1. Build: `python build.py --clean`
2. Push and tag: `git tag -a v0.6.0 -m "Arkon Launcher 0.6.0" && git push origin main --tags`
3. Go to <https://github.com/arkon-interactive/Arkon-Launcher/releases/new>
   - Tag: `v0.6.0`
   - Title: `0.6.0`
   - Notes: the `## 0.6.0` section of `CHANGELOG.md`
   - **Attach `dist/ArkonLauncherSetup.exe`**

## What the in-app updater expects

It reads
`https://api.github.com/repos/arkon-interactive/Arkon-Launcher/releases/latest`
and compares against `__version__`.

| | |
|---|---|
| Tag | Anything parsing as `MAJOR.MINOR.PATCH`; a `v` prefix is fine |
| Asset | Filename matching `ArkonLauncher*Setup*.exe` |
| Pre-releases | Ignored — the API's `latest` excludes them |

**A release with no installer attached is the one thing that breaks it** — the
updater can then only send people to the releases page. `tools/release.py`
always attaches it.

Version comparison is numeric, not lexical, so `0.10.0` correctly beats `0.9.0`.

Nothing is downloaded without the user agreeing and nothing is run without a
second confirmation. Portable copies are pointed at the releases page rather
than being replaced, since an installer can't sensibly overwrite a folder
someone may have put on a USB stick. Turn the check off with
`check_for_updates` in `settings.json`.
