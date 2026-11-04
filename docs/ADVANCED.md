# Nodes, recovery and backups

These operations are available in the grouped TUI and by direct action. Preview mode simulates the workflows without contacting hosts or installing services.

## Automatic first node

Use **Nodes → Create node and allocations**, or choose automatic creation when linking Wings on the managed Panel machine. Supply a location code, node name/address, memory/disk limits, API/SFTP ports and allocation IPv4/range. The installer uses Panel v2's location, node and allocation creation contracts. Existing matching nodes are reused; conflicting settings stop the operation.

HTTPS Panels require HTTPS node addresses. An existing reverse proxy can terminate node TLS; otherwise the local or remote node needs its own working DNS and certificate. Creation alone does not open game allocation ports at the hosting provider. Local linking backs up the old configuration before writing the new one.

## Isolated native game SQL

Choose the database engine once in the Panel wizard. For local game SQL, select an isolated native instance or Docker. Native SQL has a separate operating-system user, datadir, socket, port, systemd service and provisioning account; it never grants the game provisioning user access to the Panel SQL instance. Where a packaged AppArmor profile applies, only the managed SQL paths are added to its local rules.

The local native instance binds to the private game bridge. Docker remains required for game containers and this bridge, even though SQL itself runs natively. Its service starts after Docker, and its own input rules restrict SQL to local bridges. Game SQL and its credentials remain independent when the Panel is removed.

## Panel and Wings-configuration backups

Use **Back up Panel** for the Panel application, its SQL database, original encryption keys and persistent configuration. Use **Back up Wings configuration** for the node configuration. Archives have a SHA-256 companion file; retain both. **Restore Panel** checks the archive and requires an empty managed destination with the original deployment method and database engine.

These tools do not read hosted game volumes, copy game SQL data, or pause game containers. Protect hosted servers through Pterodactyl's backup features or a separate backup system.

## Guided Panel update rollback

Every **Update Panel v2** records the verified backup taken before its code or schema changed, together with the previous version. **Roll back Panel update** (shortcut 35, direct action `rollback-panel`) returns to that version:

1. The archive checksum, its Panel-only contents and its saved state are verified. The backup must be older than the installed Panel and use the same deployment method, database engine and Panel database.
2. A plan shows the installed and target versions. A manual rollback requires the written confirmation and first creates a safety copy of the current Panel; Panel changes made after the backup are lost.
3. The Panel stops, its database is recreated so tables from newer migrations cannot remain, and the pre-update SQL is imported.
4. Docker restores the previous pinned image digest and the extension directories. Standalone reinstates the previous application (including PHP dependencies), keeps logs written since then and rebuilds `.env` from the current settings.
5. The Panel starts and passes its health checks. The rollback point is then consumed and `rolled_back_from` records the version that was replaced.

Application keys, credentials, certificates, Redis data and current settings such as SMTP are retained; the rollback refuses a backup whose application keys differ. When an update fails, the Panel stayed stopped after its backup, so the offered rollback skips the safety copy without losing data. If you decline, **Resume installation** later offers to continue the update or roll back. Wings updates already revert their binary or image automatically when the new version does not start.

## Encrypted scheduled backups

Use **Schedule encrypted backups**. Choose a local Restic repository, `sftp:user@host:path` or `s3:https://endpoint/bucket`, supply the encryption password, select daily/weekly/hourly. The schedule includes the installed Panel and/or Wings configuration. A Wings-only machine can schedule configuration backups without a local Panel. S3 credentials and the encryption password are stored in private root-only files. SFTP requires preconfigured SSH keys and a verified host key.

The installer installs Restic if needed, validates or initializes the encrypted repository and installs `ptero-backup.timer`. A private runtime copy makes scheduled jobs independent of the online launcher's temporary directory. **Run scheduled backup now** performs the same job immediately. Hosted game files and game databases are excluded from every schedule. Older profiles are saved in the current Panel/Wings-only format; obsolete game flags are discarded, with a notice if they were enabled.

Restic uploads the installed component archives, applies seven daily/four weekly retention within this installation's unique snapshot scope and checks repository integrity. Only successful runs trigger cleanup of older scheduler-created local archives; the latest three runs are retained. Manually created archives are not deleted.

**Download remote backup** lists this installation's snapshots, restores the selected one into a new private directory and verifies archive checksums. Game-data snapshots from earlier releases are rejected before Restic restores their files. Downloading an accepted Panel/Wings snapshot does not overwrite an installation. Use **Restore Panel** for Panel recovery; inspect and apply a Wings configuration backup on the matching node. Keep the repository password outside the server: the installer cannot recover a lost encryption password.

## Resume and health

Installation phases persist `running`, `failed` and `done` states. A completed phase is skipped only when it supplies a successful validation for the same release. Migrations remain idempotent and run again when needed; error text containing credentials is never saved in phase state. Use **Resume installation** after correcting the reported problem.

**Detailed health report** collects disk/memory pressure, service/container failures, migrations, queue failures, Panel HTTP/TLS, Wings and backup timer results. One failing component does not hide the other findings. It reports issues without restarting services or changing firewall rules.

## Remote machines

**Install remote Wings node** and **Prepare remote game SQL** use root SSH, an existing private key and an explicitly selected verified `known_hosts` file. Host-key checking is strict. Python 3 must already be present remotely. Runtime source travels over SSH with a SHA-256 check; credentials travel through encrypted stdin instead of command arguments.

Remote Wings creates the Panel node and allocations, then installs and configures the daemon. A failed remote operation leaves the Panel node available for diagnosis/retry. Remote SQL requires an assigned private IPv4 and explicit private source addresses. Its separate native instance and firewall restrict provisioning access to those sources; the Panel then validates and registers the host. VPN/private routing and the provider firewall must already connect the machines. No private keys are copied into the runtime package.

For Docker + MySQL, the official Panel image's bundled MariaDB client needs its MySQL authentication plugin. The installer extracts that signed Alpine package in a disposable container, verifies/caches the plugin by SHA-256 and mounts it read-only. Its private SQL client accepts the database container's generated certificate; HTTPS and SSH verification are unaffected. These client files are included in Panel backups.
