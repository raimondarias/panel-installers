# Installation and operations

Detailed setup and lifecycle reference. For the quick start, see [README](../README.md).

## Guided setup

The simple path asks for the installation method, database, access method, address, administrator and optional SMTP settings. Recommended defaults are clearly labeled. The final review shows the choices before installation.

| Question | Choices |
| --- | --- |
| Language | English / Spanish / Brazilian Portuguese |
| Experience | Simple **(Recommended)** / Advanced |
| Panel deployment | **1. Standalone (Recommended)** / 2. Docker |
| Database engine (asked once) | MariaDB **(Recommended)** / MySQL, reused for Panel and game SQL |
| Address | Domain/subdomain **(Recommended)** / machine's public IPv4 |
| Transport | HTTPS recommended for domains; HTTP forced for an IP |
| Administrator | Email, username and hidden password; empty password generates a strong one |
| SMTP | Configure now or later |
| Game database host | Local isolated Docker instance / existing host / configure later (default) |
| Advanced database host | Display name, local port/manager/password, optional existing node ID |
| Advanced only | Panel release, PHP 8.3/8.4 for standalone, HTTP port, IPv4 bind address, timezone |
| Wings deployment | Standalone daemon **(Recommended)** / Docker container |

Generated **administrator** passwords are displayed once before deployment so you can save them in your password manager. They are not written to a credentials file. Database and private Redis passwords are generated automatically and stored in private configuration files.

The public IPv4 default is discovered using HTTPS services (`api.ipify.org`, with `ipv4.icanhazip.com` as fallback). If detection fails, enter the machine's public IPv4 manually. Private/loopback addresses are not silently used as public addresses. A custom IP must actually route to this machine; the installer cannot configure your provider's NAT.

## Deployment choices

| Component | Docker | Standalone |
| --- | --- | --- |
| Panel | Official versioned image, pinned to its pulled digest | Verified release archive + PHP 8.3/8.4 + verified Composer 2 |
| Database | MariaDB 11.4 or MySQL 8.4 LTS container, no public DB port | Compatible native MariaDB or MySQL packages; private `ptero_panel` database/user |
| Redis | Redis 7.4 container, no public Redis port | Dedicated password-protected instance on `127.0.0.1:16379` |
| Web server | NGINX in the official Panel image | Managed NGINX site and dedicated PHP-FPM pool |
| Queue / scheduler | Managed inside the official Panel image | Dedicated systemd worker and scheduler timer |
| Wings | Official versioned image with host networking and required host bind mounts | Verified binary and systemd service |

**Wings always requires Docker for game servers**, including when the Panel itself is installed in standalone mode. The Panel never receives the Docker socket. The optional Wings container needs that socket to manage game containers.

Standalone PHP uses distribution packages when the selected PHP version is available, or Ondřej's PHP PPA on Ubuntu / the Sury PHP repository on Debian. Ubuntu 22.04 gets MariaDB 11.4 from MariaDB's repository. Debian MySQL uses Oracle's MySQL 8.4 repository. Native MySQL on Debian is restricted to amd64 because the upstream Debian repository does not publish the required ARM64 packages; use MariaDB or Docker on Debian ARM64.

A pre-existing **different database engine is not replaced**. An existing reserved `ptero_panel` database/user is not adopted without an ownership record. Shared PHP, NGINX and database packages are retained when removing the Panel; only this installer's site, pool, worker, timer and private Redis are managed.

## Supported hosts

Ubuntu **22.04, 24.04, 26.04**, and Debian **12, 13**. Linux with systemd, root access and amd64/arm64. Python 3.10 or newer; no pip dependencies.

At least 4 GiB free space is required. At least 2 GiB RAM is recommended, **plus** your games' memory and room for backups. Wings needs a Docker-capable host; KVM VPSes or dedicated servers are appropriate. OpenVZ/LXC and nested Docker/Podman environments are rejected by its host check.

Use a fresh server for installation. This installer does not automatically adapt an existing reverse proxy or overwrite another installer's Panel/Wings. Existing listeners on the selected Panel ports are detected before deployment.

For HTTPS, your domain's A/AAAA records must point at the server and public TCP 80/443 must be reachable. An incorrect AAAA record can also prevent certificate issuance. IP mode does not request a certificate.

## Game database hosts

The database engine is selected **once**. The Panel gets its own application database and restricted application user. The optional game database host is a separate SQL server: Pterodactyl creates a database and account there **for each game server** when requested. A node itself does not need a single shared game database.

Choose one of three options in either simple or advanced setup:

1. **Local node (isolated game SQL):** choose an isolated native instance (recommended) or a separate, digest-pinned MariaDB/MySQL container. See [native SQL setup](ADVANCED.md). The Docker option installs its own project, even when the **Panel is standalone**. This explicit option installs Docker if needed. The privileged provisioning user cannot access the Panel SQL instance. Docker selects a private bridge subnet; SQL publishes only on that bridge's IPv4 gateway, on port 3307 by default. This address works for the native/Docker Panel and game containers on this machine. It does not use the public Panel IP and does not work for remote nodes.
2. **Existing database host:** asks for its reachable hostname/IPv4, port and existing provisioning credentials, then registers it in the Panel. Remote SQL packages, grants, private routing and firewall must already be prepared; this registration option does not alter that host. To provision a new private SQL host over verified SSH, use **Prepare remote game SQL** separately. Use the selected MariaDB/MySQL engine there as well.
3. **Configure later (default):** installs the Panel without game SQL. Use menu option 20 later. A simple Panel installation does not acquire another SQL container without choosing it.

The local SQL firewall admits host loopback, Docker's default bridge, standard `br-*` bridges and Wings' default `pterodactyl0` interface. It drops other incoming SQL traffic in both `DOCKER-USER` and `INPUT`, including traffic routed to the private gateway. It does not flush other rules or change SSH/firewall policy. It requires Docker's **iptables** backend with `DOCKER-USER`; setup stops before starting SQL if that backend is unavailable. Custom Wings bridge names need an explicit firewall adaptation. The dedicated `ptero-game-db-firewall.service` reapplies those rules after boot/Docker restart. Docker SQL uses `ptero-game-databases.service`, ordered after the firewall, with Docker restart policy disabled so it cannot automatically start before those rules. Systemd supervises the attached Compose process and recovers a stopped SQL process. Native SQL instead uses `ptero-game-sql-native.service`, with its own data at `/var/lib/pterodactyl-installer/game-sql/`.

Registration calls Panel v2's own `CreatesDatabaseHosts` contract: it checks the connection and encrypts the password. Repeating setup reuses the local password/image and matches an existing host's settings instead of creating duplicates. If registration fails, the Panel stays installed and the private SQL data remains; retry **Install Panel** or use the database menu. An engine/password change on an existing local instance requires a separate migration.

The final card shows **host, port, provisioning username and password** and writes a root-only report at `/opt/pterodactyl-installer/game-database-credentials.json` (mode 0600). Advanced setup lets you enter your own manager/password, with confirmation. These credentials are for the Panel to create game accounts; **do not give them to game users**. They differ from the Panel's application DB credentials.

An advanced setup can link the host to an existing numeric node ID. Leave it at **0** to register a global host when the first node has not been created. Each game server still needs a **database limit greater than zero** in the Panel. Then creating a database from that server's Databases page generates its separate database/user; setup does not invent a first game server.

Local SQL data lives outside the Panel project at `/opt/pterodactyl-installer/game-databases/`. **Removing or purging Panel/Wings preserves this project, its data, SQL service, firewall and connection report.** Game databases may still serve running games. The installer provisions and registers the host; game database data backups and restores belong to Pterodactyl or your own database backup system.

[Pterodactyl SQL setup](https://docs.pterodactyl.io/v2/guides/tutorials/mysql-setup) · [v2 host creation contract](https://github.com/pterodactyl/panel/blob/v2.0.0-beta.2/app/Actions/Databases/CreateDatabaseHost.php) · [Docker publishing and routing](https://docs.docker.com/engine/network/port-publishing/).

## First node and game server

1. Complete Panel installation and sign in with the administrator created by the wizard.
2. Create a location and a node in the Panel. Set its FQDN/IP, SSL, API/SFTP ports, memory and disk.
3. Add allocations using the node's reachable IP and your chosen game ports.
4. Generate a token under `Nodes → Configuration → Auto-Deploy`. The Wings wizard asks for the Panel URL, numeric node ID and hidden token. Paste the token itself, not the full shell command.
5. If using HTTPS, enable SSL in the node and use `/etc/letsencrypt/live/NODE_DOMAIN/fullchain.pem` and `privkey.pem`. Issue the certificate from the menu and synchronize the node configuration again if you changed its SSL settings.
6. Check that the node is connected in the Panel, then create a test game server using an egg/image compatible with your CPU architecture.

Wings can be installed on the Panel machine or another node. If you postpone linking it, it remains **pending and stopped**. Use **Create node and allocations** to collect and create these resources automatically, or keep the existing token-based linking flow. [Node setup and remote provisioning](ADVANCED.md).

The node API is accessed over verified HTTPS without putting the Auto-Deploy token in process arguments. JSON is written as valid YAML and Wings normalizes it on startup. Only the remote fields allowed by Wings' `PanelConfiguration` are accepted. Relinking creates a fresh node configuration after backing up the previous one; reapply custom host settings from that copy if needed.

## Operations menu

| Option | Operation |
| --- | --- |
| 1–3 | Install Panel + Wings / Panel / Wings |
| 4–6 | Uninstall Panel + Wings / Panel / Wings |
| 7–8 | Update Panel / Wings |
| 9–10 | Link Wings / issue Wings HTTPS certificate |
| 11–13 | Back up Panel / Wings configuration / restore Panel |
| 14–16 | Diagnostics / restart / logs |
| 17–19 | SMTP / administrator / ports and firewall |
| 20–21 | Configure game SQL host / view its credentials |
| 23 | Change language |
| 24 | Create node and allocations |
| 28–30 | Schedule encrypted backups / run backup now / download snapshot |
| 31–32 | Detailed health / resume installation |
| 33–34 | Install remote Wings / prepare remote game SQL |
| 35 | Roll back the last Panel update |
| 0 | Exit |

All listed operations are implemented. A component must be installed and configured before actions that depend on it can run. On a fresh machine, select an action to see its prerequisite and the installation step that prepares it.

In the TUI, these operations are grouped under **Install**, **Maintenance**, **Game databases**, **Backups**, **Remove**, **Nodes** and **Preferences**. The numbers above are stable keyboard shortcuts and the layout used by `--plain`.

These operations are also available as direct commands: `install-all`, `install-panel`, `install-wings`, `uninstall-all`, `uninstall-panel`, `uninstall-wings`, `update-panel`, `update-wings`, `configure-wings`, `tls-wings`, `backup-panel`, `backup-wings`, `restore-panel`, `diagnose`, `restart`, `logs`, `smtp`, `admin`, `firewall`, `configure-database`, `database-credentials`, `create-node`, `backup-schedule`, `backup-run`, `backup-download`, `health`, `resume`, `remote-node`, `remote-database` and `rollback-panel`. Confirmations still apply.

## Backups, updates and removal

Panel backups contain SQL, original application keys, Redis and persistent configuration. Standalone backups also include the complete application; its Panel certificate/account state is kept in a dedicated managed Certbot directory. Backups pause the Panel's workers/web access and Redis as appropriate, and recover previously running services when an operation fails. Each archive has a `.sha256` companion; retain both and store a copy off the server.

**Panel and Wings-configuration backups are the only supported backup scope.** No hosted game SQL data, worlds, files, volumes or game backups are copied or restored. Protect hosted servers through Pterodactyl or your own backup system.

Scheduled encrypted backups select Panel and/or Wings configuration, including Wings-only machines. Actions 28, 29 and 30 configure the schedule, run it immediately and download a verified snapshot. See [encrypted scheduled backups](ADVANCED.md#encrypted-scheduled-backups).

Restoration requires empty managed Panel/application directories and preserves the original APP_KEY/HASHIDS_SALT. Standalone restore also requires an empty private Redis directory. The backup's deployment method and database engine are restored; this is not a Docker ↔ standalone or MySQL ↔ MariaDB migration tool. Keep the original domain/IP/DNS or explicitly adapt the configuration afterward.

Panel updates show a plan (current → target version) and make a verified backup before migration; that backup becomes the update's rollback point. If the update fails, the installer offers an immediate rollback; declining leaves the new code offline/in maintenance and **Resume installation** then offers to continue the update or roll back. **Roll back Panel update** (35) also works after a successful update. Code is never started on a schema it does not match: see [guided update rollback](ADVANCED.md#guided-panel-update-rollback). Wings updates recover the previous executable/image and node configuration if startup fails.

Uninstall Panel backs it up first and preserves data by default. Permanent data deletion requires another written confirmation and a successful backup. Uninstall Wings backs up configuration, removes only its daemon/service container and keeps game containers, worlds, Docker and certificates. Games can remain running after removing Wings; stop them from the Panel first if that is your intention. No global Docker prune or volume deletion is performed.

Panel HTTPS renews inside its Docker image or through its dedicated standalone Certbot timer. Wings certificates renew through host Certbot with a daemon/container restart hook. When sharing a host with the Docker Panel, Wings' standalone certificate challenge briefly pauses that Panel to free port 80. With a standalone NGINX Panel, it uses an ACME webroot without that pause.

## Files and project structure

```text
install.sh                  # English-first selector, self-update, verified release download
installer.py                # Compatibility Python entry point
pyproject.toml              # Project metadata and Ruff lint configuration
ptero/
  cli.py                    # Language selection, grouped menu, process lock
  ui.py                     # Interface adapter, fallback and terminal lifecycle
  tui.py                    # Full-screen ncurses menus, forms, progress and results
  navigation.py             # Read-only dashboard inventory and action prerequisites
  terminal_text.py          # Unicode cell widths, safe clipping and wrapping
  i18n.py, locales/         # Three-language catalogue keyed by English source text
  distribution.py           # Verified language changes from the running release
  wizard.py, admin.py       # Simple / advanced configuration; administrator over stdin
  core.py                   # Installer facade composed from the modules below
  common.py, runtime.py     # Shared validators/file helpers; command runner and paths
  host.py                   # Host checks, Docker runtime, official releases
  panel_ops.py, rollback.py # Docker Panel lifecycle; guided update rollback
  wings_ops.py, wings.py    # Wings lifecycle; Docker Wings backend
  native*.py                # Standalone Panel: packages, web/TLS and lifecycle
  compat.py, retention.py   # Panel/Wings compatibility; local backup retention
  maintenance.py            # Diagnostics, logs, restart and the action table
  game_database.py          # Independent game SQL host + Panel registration
  preview.py                # Safe UI preview
scripts/
  run_local.sh              # Local/offline runtime entry point
  translations.txt          # English|||Spanish|||Portuguese message table
  build_catalog.py          # Build the catalogue from the translation table
  check_translations.py     # Fail when a user-visible message is untranslated
  build_language_packs.py   # Reproducible release assets, one locale per archive
  release_notes.py          # CHANGELOG section for a release
  build_release.py          # Source archive and self-contained offline launcher
tests/                      # Unit, integration-of-commands and remote checks
docs/                       # Operator guides, previews, test and release procedures
test-results/               # Verification report and captured test output
```

| Runtime path | Purpose |
| --- | --- |
| `/opt/pterodactyl-installer/panel/` | Private state, Compose/data or standalone certificate state |
| `/opt/pterodactyl-installer/game-databases/` | Independent game SQL project, data, privileged state and firewall script |
| `/opt/pterodactyl-installer/game-database-credentials.json` | Private game SQL provisioning connection report |
| `/var/www/pterodactyl/` | Standalone application; its original keys are in `.env` |
| `/var/lib/pterodactyl-installer/redis/` | Standalone private Redis persistence |
| `/etc/pterodactyl/config.yml` | Private Wings node configuration |
| `/opt/pterodactyl-installer/wings-compose.json` | Optional Wings container project |
| `/var/backups/pterodactyl-installer/` | Private backups; the newest 5 of each kind are kept by default (action 36) |
| `/var/log/pterodactyl-installer/installer.log` | Private redacted log, rotated at 5 MiB with three generations |
| `/var/log/pterodactyl-installer/installer.log` | Private, redacted installer log |
| `/var/lib/pterodactyl/` | Wings game data; preserved on removal |

For Docker Panel installations, keep an off-server copy of `/opt/pterodactyl-installer/panel/data/var/.env`. For standalone, preserve `/var/www/pterodactyl/.env`. Losing original application keys makes encrypted data unrecoverable even with a SQL backup.

## Testing and building

```bash
python3 scripts/build_catalog.py
python3 scripts/build_language_packs.py
bash tests/run.sh
# Optional upstream release/binary checks:
bash tests/run.sh --live
# After publishing: actual GitHub downloads and language changes, no service installation
python3 tests/published_check.py
python3 scripts/build_release.py
```

TUI tests use real pseudoterminals for keyboard navigation, masked fields, resize, animation, fresh-machine diagnostics and terminal restoration after cancellation/Ctrl+C. Distribution tests run the real Bash bootstrap against recorded GitHub URL fixtures, exercise all three selected packages and subsequent language downloads, and reject missing/corrupt/unsafe archives. They also require committed language packages to match the shared source exactly. The local suite uses temporary paths and simulated package/service commands, plus the real Compose parser, NGINX/PHP syntax validation and a disposable MariaDB instance using only a temporary Unix socket when those binaries are available. Remote checks verify official release artifacts, execute Wings' `version`/`configure` against a local mock API, extract the real Panel release and inspect OCI manifests. These checks do **not** install Pterodactyl on the test host.

Full clean-VPS deployments, public certificates, email delivery and running a game still need the integration procedure in [docs/TESTING.md](TESTING.md). See [test-results/REPORT.md](../test-results/REPORT.md) for exactly what was executed. No production readiness claim is made from simulated service tests.

[Guía rápida en español](ES.md) · [Guia rápido em português brasileiro](PT-BR.md).

Next priorities: [ARM64 execution, independent provider tests, notifications](ROADMAP.md).

## Upstream references

[Panel v2](https://docs.pterodactyl.io/v2) · [Native installation](https://docs.pterodactyl.io/v2/panel/getting-started) · [Docker deployment](https://docs.pterodactyl.io/v2/panel/docker) · [Requirements](https://docs.pterodactyl.io/v2/panel/requirements) · [Wings](https://docs.pterodactyl.io/v2/wings/installing) · [PHP packages](https://deb.sury.org/) · [MySQL APT](https://dev.mysql.com/doc/mysql-apt-repo-quick-guide/en/).

Ubuntu 26.04 is covered by Docker integration. Fresh standalone setup needs a separately configured PHP 8.3/8.4 package source: the tested Ondřej PPA currently has no resolute Release index. The installer stops before package changes when that prerequisite is missing. The automated fresh-VM matrix excludes those two unavailable native cases.
