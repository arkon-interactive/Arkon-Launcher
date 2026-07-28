# Publishing a release

The in-app updater checks
`https://api.github.com/repos/arkon-interactive/Arkon-Launcher/releases/latest`
and compares the tag against `__version__`. For it to find an update, a release
needs a version tag and the installer attached as an asset.

## Steps

1. **Bump the version** in `arkon_launcher/__init__.py`. That single value flows
   into the window title, the installer, and Add/Remove Programs.

2. **Update `CHANGELOG.md`** — move the Unreleased notes under the new version.

3. **Build:**

```bash
python build.py --clean
```

   Produces `dist/ArkonLauncherSetup.exe`.

4. **Commit and push** (GitHub Desktop, or `git push`).

5. **Tag and publish.** On GitHub → Releases → Draft a new release:
   - Tag: `v0.6.0` — the leading `v` is optional, the updater strips it.
   - Title: `0.6.0`
   - Body: paste that version's changelog section. It's shown in the update
     dialog's details.
   - **Attach `dist/ArkonLauncherSetup.exe`.** Without it the updater can only
     send people to the releases page.

## What the updater expects

| | |
|---|---|
| Tag | Anything parsing as `MAJOR.MINOR.PATCH`; `v` prefix fine |
| Asset | Filename matching `ArkonLauncher*Setup*.exe` |
| Pre-releases | Ignored — the API's `latest` excludes them |

Version comparison is numeric, not lexical, so `0.10.0` correctly beats `0.9.0`.

## What the updater does

On startup it checks once, in the background. If a newer release exists it says
so and offers to download — nothing is fetched without a yes, and nothing is run
without a second yes. The download is size-checked before being offered.

Portable copies are detected and *not* auto-updated: the installer can't sensibly
replace a folder someone may have put on a USB stick, so those are pointed at the
releases page instead.

If the server is running, updating warns first — the installer closes the
launcher to replace it.

Turn the check off with `check_for_updates` in `settings.json`.

## Using the GitHub CLI instead

`gh` isn't installed here. If you'd rather script releases:

```bash
winget install --id GitHub.cli
```

Then authenticate once — this is interactive, so it needs a real terminal:

```bash
gh auth login
```

After that a release is one command:

```bash
gh release create v0.6.0 "dist/ArkonLauncherSetup.exe" --title "0.6.0" --notes-file CHANGELOG.md
```
