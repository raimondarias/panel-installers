# Releasing

Releases are published by CI. Nobody uploads archives by hand, and generated
packages are not committed to the repository.

## How a release happens

1. Bump the version in **both** `ptero/__init__.py` (`__version__`) and
   `install.sh` (`VERSION`). The build refuses mismatched versions.
2. Add a `## <version> — <date>` section to `CHANGELOG.md`. It becomes the
   release notes; the build fails without it.
3. Open a pull request. The **Installer verification** workflow runs Ruff, the
   translation coverage check, a full release-asset build and the regression
   suite.
4. Merge to `main`. The **Release** workflow sees that `v<version>` is not
   published yet, runs the checks again, builds the assets and creates the
   GitHub Release `v<version>`, marked as latest. Pushes that do not change the
   version publish nothing.

Release assets:

| Asset | Purpose |
| --- | --- |
| `install.sh` | The bootstrap for this version (also used for self-update) |
| `installer-en.tar.gz`, `installer-es.tar.gz`, `installer-pt-BR.tar.gz` | Shared runtime code with only that language's catalogue |
| `messages-<language>.json` | Catalogues for changing language from the running menu |
| `SHA256SUMS` | Checksums of every asset above |

Rebuilding unchanged sources produces byte-identical archives:

```bash
python3 scripts/build_language_packs.py dist
```

## What the user runs

```bash
curl -fsSL https://raw.githubusercontent.com/raimondarias/pterodactyl-installer/main/install.sh -o ptero-setup.sh && sudo bash ptero-setup.sh
```

1. The first question, in English, selects the language. Nothing is downloaded before it.
2. The bootstrap asks GitHub for the latest release. If it is newer than the
   bootstrap's own `VERSION`, it downloads that release's `install.sh`, checks
   it against the release's `SHA256SUMS` and its version line, replaces the
   saved `ptero-setup.sh` (keeping its permissions) and restarts with the chosen
   language. If the check fails or GitHub is unreachable, it continues with the
   current version and says so. `--no-self-update` or `PTERO_NO_SELF_UPDATE=1`
   skips the check.
3. It downloads `SHA256SUMS` and `installer-<language>.tar.gz` from release
   `v<VERSION>`, verifies the checksum, the archive members and the embedded
   language/version/repository, and runs the installer.
4. Changing language later downloads `messages-<language>.json` from the same
   release and checks it against the hash embedded in the running package.

Between merging a version bump and the Release workflow finishing (a few
minutes), `main`'s `install.sh` already names the new version while its assets
do not exist yet; downloads fail safely in that window. Run the published check
after the workflow succeeds.

## Verify a publication

```bash
python3 tests/published_check.py
```

It confirms the latest release matches this checkout's version, verifies the
bootstrap and English archive checksums, runs the real online preview in all
three languages (including the full-screen TUI) and a language change. It
installs no services. Complete the clean-VPS procedure in
[TESTING.md](TESTING.md) before deploying services.

## Maintain one codebase

- Shared functionality lives in `ptero/`.
- Every message is keyed by its English text. Edit `scripts/translations.txt`
  (`English|||Spanish|||Brazilian Portuguese`), then run
  `python3 scripts/build_catalog.py`. `python3 scripts/check_translations.py`
  lists any user-visible message without a translation.
- `scripts/run_local.sh` runs the checkout directly for development.
- `python3 scripts/build_release.py` still builds a self-contained `.run`
  launcher with all three languages for offline use.
