# Changelog

## 1.3.0 — 2026-11-04

### Installer self-update and CI releases
- Publish every version from CI as a GitHub Release (per-language runtime archives, catalogues, `install.sh` and `SHA256SUMS`). Generated packages are no longer committed.
- When run again, the bootstrap checks the latest release after the language question. A newer, checksum-verified `install.sh` replaces the saved bootstrap and restarts with the chosen language; `--no-self-update` keeps the current version.

### Recovery and safety
- Add **Roll back Panel update** (shortcut 35, `rollback-panel`) for Docker and standalone Panels. Each update records its verified pre-update backup and previous version; a rollback recreates the database from that backup, reinstates the previous image or application and keeps keys, credentials, certificates and current settings.
- Offer an immediate rollback when a Panel update fails, and let **Resume installation** choose between continuing an incomplete update and rolling it back.
- Show a Panel update plan (current → target version and rollback availability) before confirmation.
- Check Panel ↔ Wings compatibility before Wings install/update and Panel update/rollback: block impossible pairs and ask before using a pair outside the validated matrix.
- Keep the newest 5 local Panel archives and Wings configuration copies of each kind (action 36, `backup-retention`); rollback points, safety copies and scheduled copies are never pruned.
- Rotate the private installer log at 5 MiB (three generations), stop inheriting tokens and storage credentials into child commands and cap Wings/Panel download sizes.

### Interface
- Show friendly component statuses, installed versions and an actionable hint for interrupted operations in the TUI dashboard and the numbered menu. Report each operation's duration.

### Maintainability
- Split the installer core into focused host, Panel, Wings and maintenance modules, and the standalone backend into package, web and lifecycle modules; `ptero.core` and `ptero.native` keep their public API.
- Use English as the single source language for every message; `scripts/check_translations.py` fails CI when a user-visible message has no translation. Log redaction now reads `[REDACTED]`.
- Add `pyproject.toml`, Ruff lint and formatting in CI, and a shared streaming SHA-256 helper.

## 1.2.1 — 2026-10-24

- Use the full **Pterodactyl Installer** name in the bootstrap, terminal interface and documentation in all three languages.
- Clarify that maintenance actions are implemented and expose their precise installation/configuration prerequisites as selectable menu entries.
- Restrict all backup and recovery tools to the Panel and Wings configuration. Remove hosted-game volume and game-database backup/restore commands while retaining SQL host provisioning.
- Support Panel-only, Wings-configuration-only and combined encrypted schedules. Safely normalize legacy profile scopes and reject legacy game-data snapshots before restoration.
- Keep detailed health reporting available when managed state is corrupt, so the report explains problems instead of treating them as a fresh installation.


## 1.2.0 — 2026-10-23

- Select languages, menu actions and wizard choices with a mouse click in compatible terminals; click sidebar groups and use the wheel to navigate. Keyboard controls remain available.
- Restore mouse tracking on exit and enforce the same action prerequisites for clicks and keyboard selection.
- Add automatic location/node/allocation creation through Panel v2 contracts, with duplicate checks and local Wings configuration.
- Add isolated native game SQL, verified physical SQL restoration and game-file backup/restore.
- Add encrypted Restic backups, systemd scheduling, per-instance retention and snapshot download.
- Add persisted installation phases, a resume action and a health report that detects pending migrations and queue failures in both backends.
- Add remote Wings and private game SQL provisioning over SSH with strict host-key verification.
- Add disposable QEMU integration runners and a manual Ubuntu/Debian deployment matrix.
- Fix native Artisan working directories, HTTP readiness after restore, Docker MySQL authentication and internal SQL certificate handling.
- Isolate native game SQL under a separate operating-system user and add scoped AppArmor rules.
- Detect Docker/host network conflicts before choosing the Wings bridge.
- Provision Docker game SQL through private stdin, check network readiness, omit its transient socket from backups and keep supervision stopped until physical restore finishes.
- Enforce secure session cookies for HTTPS URLs and explain the fresh Ubuntu 26.04 native PHP limitation before package changes.

## 1.1.0 — 2026-10-23

- Replace the short interface demo with a complete in-memory installation preview, sharing production Panel and Wings connection forms.
- Add simulated maintenance, game database credentials, backups, restore, removal and scenario reset with contextual prerequisites.
- Identify preview mode throughout the TUI and animate simulated task stages. Support an automatic full text tour for redirected input.
- Prevent both launchers from installing missing Python in preview mode, even as root.
- Handle malformed state objects and nested credentials as invalid managed state instead of crashing.
- Reset the progress display to Wings installation phases when installing Panel and Wings together.
- Shorten the README and move detailed operations and preview instructions into dedicated guides.
- Expand regression coverage for simulation isolation, installation choices, lifecycle and terminal interaction.

Upstream Panel v2 remains beta. See the verification report for executed deployments and remaining infrastructure limits.
