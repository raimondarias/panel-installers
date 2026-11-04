"""Panel lifecycle: state, Compose configuration, install, backup, restore, update and removal."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile

from .common import (PROJECT, SCHEMA, InstallerError, atomic_write, file_sha256, secure_extract, valid_host,
                     write_checksum,
                     version_key, write_json)


class PanelMixin:
    def panel_state(self):
        if not self.p.state.is_file():
            raise InstallerError("Install the Panel first. Start with Install Panel or Install Panel and Wings.")
        state = self.load(self.p.state)
        settings = state.get("game_database", {})
        credentials = [state.get("db_password"), state.get("mail_password", ""), state.get("redis_password", "")]
        if not isinstance(settings, dict) or not all(isinstance(value, str) for value in credentials):
            raise InstallerError("This installation or backup does not belong to this installer.")
        if not isinstance(settings.get("password", ""), str):
            raise InstallerError("This installation or backup does not belong to this installer.")
        self.run.secrets.update([state["db_password"], state.get("mail_password", "")])
        self.run.secrets.add(state.get("redis_password", ""))
        self.run.secrets.add(settings.get("password", ""))
        return state

    def save_panel(self, state):
        write_json(self.p.state, state)
        if state.get("method", "docker") == "docker":
            if state.get("database_engine") == "mysql":
                # The bundled MariaDB CLI otherwise rejects MySQL's generated
                # private-cluster certificate during Laravel schema loading.
                atomic_write(self.p.data / "mysql-client.cnf", "[client]\nssl-verify-server-cert=0\n", 0o644)
            write_json(self.p.compose, self.compose_config(state))

    def compose_config(self, state):
        env = {
            "APP_URL": state["url"], "APP_TIMEZONE": state["timezone"],
            "APP_SERVICE_AUTHOR": state["email"], "APP_ENV": "production",
            "APP_DEBUG": "false", "APP_ENVIRONMENT_ONLY": "true",
            "DB_HOST": "database", "DB_PORT": "3306", "DB_DATABASE": "panel",
            "DB_USERNAME": "pterodactyl", "DB_PASSWORD": state["db_password"],
            "CACHE_STORE": "redis", "SESSION_DRIVER": "redis", "SESSION_CONNECTION": "sessions",
            "QUEUE_CONNECTION": "redis", "REDIS_HOST": "cache", "REDIS_QUEUE_RETRY_AFTER": "90",
            "QUEUE_WORKER_ENABLED": "true", "SCHEDULER_ENABLED": "true",
            "MAIL_MAILER": state.get("mail_mailer", "log"), "MAIL_FROM_ADDRESS": state["email"],
            "MAIL_HOST": state.get("mail_host", ""), "MAIL_PORT": state.get("mail_port", "587"),
            "MAIL_USERNAME": state.get("mail_user", ""), "MAIL_PASSWORD": state.get("mail_password", ""),
            "MAIL_ENCRYPTION": state.get("mail_encryption", "tls"),
            "SESSION_SECURE_COOKIE": "true" if state["url"].startswith("https://") else "false",
            "LE_EMAIL": state["email"] if state.get("tls") else "",
        }
        # Compose interpolates $ even in JSON; $$ passes a literal dollar sign.
        env = {key: str(value).replace("$", "$$") for key, value in env.items()}
        mounts = [("var", "/app/var"), ("nginx", "/etc/nginx/http.d"),
                  ("certs", "/etc/letsencrypt"), ("logs", "/app/storage/logs"),
                  ("extensions", "/app/extensions"), ("extension-assets", "/app/public/assets/extensions")]
        volumes = lambda name, target: {"type": "bind", "source": str(self.p.data / name), "target": target}
        config = {"name": PROJECT, "services": {
            "database": {"image": state.get("database_image", "mysql:8.4" if state.get("database_engine") == "mysql" else "mariadb:11.4"),
                         "restart": "unless-stopped", "volumes": [volumes("database", "/var/lib/mysql")],
                         "environment": {"MARIADB_PASSWORD": state["db_password"],
                                         "MARIADB_RANDOM_ROOT_PASSWORD": "yes", "MARIADB_DATABASE": "panel",
                                         "MARIADB_USER": "pterodactyl"},
                         "healthcheck": {"test": ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"],
                                         "interval": "10s", "timeout": "5s", "retries": 15, "start_period": "30s"}},
            "cache": {"image": state.get("cache_image", "redis:7.4-alpine"), "restart": "unless-stopped",
                      "command": ["redis-server", "--appendonly", "yes"],
                      "volumes": [volumes("redis", "/data")],
                      "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "10s",
                                      "timeout": "5s", "retries": 10}},
            "panel": {"image": state.get("panel_image", "ghcr.io/pterodactyl/panel:" + state["version"]),
                      "restart": "unless-stopped", "stop_grace_period": "90s",
                      "ports": [f"{state['bind']}:{state['http_port']}:80"] +
                               ([f"{state['bind']}:{state['https_port']}:443"] if state.get("tls") else []),
                      "depends_on": {key: {"condition": "service_healthy"} for key in ("database", "cache")},
                      "volumes": [volumes(name, target) for name, target in mounts], "environment": env}
        }}
        if state.get("database_engine") == "mysql":
            config["services"]["panel"]["volumes"].append({"type": "bind", "source": str(self.p.data / "mysql-client.cnf"),
                "target": "/etc/my.cnf.d/ptero-installer.cnf", "read_only": True})
            config["services"]["panel"]["volumes"].append({"type": "bind", "source": str(self.p.data / "mysql-plugins"),
                "target": "/usr/lib/mariadb/plugin", "read_only": True})
            db = config["services"]["database"]
            db["image"] = state.get("database_image", "mysql:8.4")
            db["environment"] = {"MYSQL_RANDOM_ROOT_PASSWORD": "yes", "MYSQL_DATABASE": "panel",
                                 "MYSQL_USER": "pterodactyl", "MYSQL_PASSWORD": state["db_password"]}
            db["healthcheck"]["test"] = ["CMD-SHELL", 'MYSQL_PWD="$$MYSQL_PASSWORD" mysql --host=127.0.0.1 --user="$$MYSQL_USER" --database="$$MYSQL_DATABASE" --execute="SELECT 1" --silent']
        return config

    def dc(self, *args, **kwargs):
        return self.run.run(["docker", "compose", "--project-name", PROJECT, "--file", self.p.compose,
                             *args], **kwargs)

    def prepare_mysql_client(self, state):
        if state.get("method", "docker") != "docker" or state.get("database_engine") != "mysql":
            return
        directory = self.p.data / "mysql-plugins"
        plugin = directory / "caching_sha2_password.so"
        source = state["panel_image"]
        if (plugin.is_file() and state.get("mysql_plugin_image") == source
                and state.get("mysql_plugin_arch") == platform.machine()
                and hashlib.sha256(plugin.read_bytes()).hexdigest() == state.get("mysql_plugin_sha256")):
            return
        directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        directory.chmod(0o755)
        self.run.run(["docker", "run", "--rm", "--entrypoint", "sh", "--mount",
                      f"type=bind,source={directory},target=/ptero-plugins", source, "-c",
                      "apk add --no-cache mariadb-connector-c && cp /usr/lib/mariadb/plugin/caching_sha2_password.so /ptero-plugins/caching_sha2_password.so && chmod 644 /ptero-plugins/caching_sha2_password.so"],
                     label="Preparing the MySQL authentication client", timeout=600)
        if not plugin.is_file():
            raise InstallerError("The MySQL authentication client was not prepared.")
        state.update(mysql_plugin_image=source, mysql_plugin_arch=platform.machine(),
                     mysql_plugin_sha256=hashlib.sha256(plugin.read_bytes()).hexdigest())
        self.save_panel(state)

    def new_panel(self):
        from .wizard import panel_plan
        return panel_plan(self)

    def native(self, state=None):
        from .native import NativeBackend
        return NativeBackend(self, state or self.panel_state())

    def check_plan(self, state):
        if state["method"] == "standalone":
            release = self.check_host()
            if state["database_engine"] == "mysql" and release["ID"] == "debian" and platform.machine() != "x86_64":
                raise InstallerError("Native MySQL on Debian requires amd64. Choose MariaDB or Docker on ARM64.")
            backend = self.native(state)
            backend.check_php_availability(release)
            if backend.app.exists() and any(backend.app.iterdir()):
                raise InstallerError("The standalone application directory already exists; unmanaged files will not be overwritten.")
            for path in (backend.site, backend.enabled, backend.pool, backend.redis_config, backend.units / backend.redis_unit):
                if path.exists():
                    raise InstallerError("A reserved standalone configuration already exists: " + str(path))
            self.port_free("127.0.0.1", 16379)
        for port in ([80, 443] if state["tls"] else [state["http_port"]]):
            self.port_free(state["bind"], port)

    def create_admin(self, state):
        if "admin" not in state:
            self.dc("run", "--rm", "panel", "php", "artisan", "p:user:make", "--admin=1", interactive=True)
            return
        if not getattr(self, "admin_password", None):
            self.admin_password = self.ui.ask("Administrator password (12+ characters)", secret=True,
                                              validator=lambda value: len(value) >= 12)
        from .admin import create
        if state.get("method") == "standalone":
            backend = self.native(state)
            create(self, state, ["runuser", "-u", "www-data", "--", backend.php], backend.app)
        else:
            create(self, state, ["docker", "compose", "--project-name", PROJECT, "--file", self.p.compose,
                                "run", "--rm", "-T", "panel", "php"], "/app")

    def smtp_settings(self, state):
        state.update(mail_mailer="smtp",
                     mail_host=self.ui.ask("SMTP host", validator=valid_host),
                     mail_port=self.ui.ask("SMTP port", "587", self.valid_port),
                     mail_user=self.ui.ask("SMTP username"),
                     mail_password=self.ui.ask("SMTP password", secret=True),
                     mail_encryption=self.ui.ask("SMTP encryption (tls/ssl/null)", "tls",
                                                 lambda s: s in ("tls", "ssl", "null")))
        self.run.secrets.add(state["mail_password"])

    def existing_game_database(self):
        from .game_database import GameDatabase
        database = GameDatabase(self)
        return database.load() if database.state_file.exists() else None

    def install_panel(self):
        self.check_host()
        if self.p.state.exists():
            state = self.panel_state()
            if state["status"] == "restoring":
                raise InstallerError("SQL restore incomplete. Keep the backup and retry in an empty target.")
            if state["status"] == "ready":
                self.ui.say("Panel already installed. Use update, restart or SMTP settings.", "ok")
                if state.get("game_database", {}).get("mode", "later") != "later" and not state["game_database"].get("registered"):
                    self.setup_game_database(state)
                return
            self.ui.say(f"Existing managed installation: {state['status']}, {state['version']}.")
            self.ui.confirm("RESUME PANEL")
        else:
            if self.p.panel.exists() and any(self.p.panel.iterdir()):
                raise InstallerError("The target contains unregistered files. They will not be overwritten.")
            state = self.new_panel()
            self.save_panel(state)
        if state.get("method") == "standalone":
            return self.native(state).install()
        from .phases import Phases
        phases = Phases(self, state, lambda: self.save_panel(state))
        phases.run("Docker runtime", self.docker)
        self.run.secrets.add(state["db_password"])
        for name in ("database", "redis", "var", "nginx", "certs", "logs", "extensions", "extension-assets"):
            (self.p.data / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        # The entrypoint gives .env to nginx but does not chown /app/var.
        # El bind mount debe permitir a nginx atravesar ese directorio.
        # Private /opt parents protect the secrets on the host.
        (self.p.data / "var").chmod(0o755)
        for key, ref in (("panel_image", "ghcr.io/pterodactyl/panel:" + state["version"]),
                         ("database_image", "mysql:8.4" if state.get("database_engine") == "mysql" else "mariadb:11.4"),
                         ("cache_image", "redis:7.4-alpine")):
            if key not in state:
                state[key] = self.pin_image(ref)
                self.save_panel(state)
        self.prepare_mysql_client(state)
        self.dc("config", "--quiet")
        phases.run("SQL and cache", lambda: self.dc("up", "-d", "--wait", "--wait-timeout", "240", "database", "cache", label="Preparing the database and Redis"))
        self.dc("stop", "panel", label="Keeping the Panel offline during migrations")
        phases.run("Panel migrations", lambda: self.dc("run", "--rm", "-T", "panel", "php", "artisan", "migrate", "--seed", "--force", "--no-interaction",
                label="Running migrations and installing official eggs"))
        if not state.get("admin_created"):
            self.ui.say("Creating the first administrator")
            self.create_admin(state)
            state["admin_created"] = True
            self.save_panel(state)
        phases.run("Service health", lambda: self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Starting the Panel, queue worker and scheduler"))
        state["status"] = "ready"
        self.save_panel(state)
        self.ui.say(f"Panel is available: {state['url']}", "ok")
        self.ui.say("Keep an off-server copy of data/var/.env (APP_KEY and HASHIDS_SALT).")
        self.setup_game_database(state)

    def setup_game_database(self, state=None):
        from .game_database import GameDatabase
        return GameDatabase(self).setup(state or self.panel_state())

    def configure_game_database(self):
        from .game_database import plan
        state = self.panel_state()
        if state["status"] != "ready":
            raise InstallerError("The Panel is not ready. Resume its installation/update first.")
        state["game_database"] = plan(self, state, advanced=True)
        self.ui.confirm("CONFIGURE DATABASE HOST")
        self.save_panel(state)
        self.setup_game_database(state)

    def game_database_credentials(self):
        from .game_database import GameDatabase
        GameDatabase(self).credentials(self.panel_state() if self.p.state.exists() else None)

    def database_command(self, state, dump=False):
        mysql = state.get("database_engine") == "mysql"
        user, password, database = (("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE") if mysql
                                    else ("MARIADB_USER", "MARIADB_PASSWORD", "MARIADB_DATABASE"))
        binary = ("mysqldump" if mysql else "mariadb-dump") if dump else ("mysql" if mysql else "mariadb")
        options = " --single-transaction --quick --hex-blob --triggers" if dump else ""
        if dump and mysql:
            options += " --no-tablespaces --column-statistics=0"
        return f'export MYSQL_PWD="${password}"; exec {binary} --user="${user}"{options} "${database}"'

    def running_services(self):
        _, text = self.dc("ps", "--services", "--status", "running")
        return set(text.split())

    def backup_panel(self):
        state = self.panel_state()
        if state.get("method") == "standalone":
            return self.native(state).backup()
        running = self.running_services()
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.p.backups / ("panel-" + self.stamp() + ".tar.gz")
        temporary = target.with_suffix(".partial")
        try:
            with tempfile.TemporaryDirectory(prefix="backup-", dir=self.p.backups) as name:
                work = Path(name)
                try:
                    if "panel" in running:
                        self.dc("stop", "panel", label="Pausing the Panel for a consistent backup")
                    if "database" not in running:
                        self.dc("up", "-d", "--wait", "--wait-timeout", "240", "database")
                    with (work / "database.sql").open("wb") as stream:
                        self.dc("exec", "-T", "database", "sh", "-c",
                                self.database_command(state, dump=True),
                                output=stream, label="Exporting the database")
                    if not (work / "database.sql").stat().st_size:
                        raise InstallerError("SQL dump is empty; the backup was not accepted.")
                    if "cache" in running:
                        self.dc("stop", "cache", label="Pausing Redis to preserve queued jobs")
                    write_json(work / "state.json", state)
                    write_json(work / "manifest.json", {"schema": SCHEMA, "owner": PROJECT, "type": "panel-backup"})
                    with self.ui.busy("Compressing configuration, original keys and data"), tarfile.open(temporary, "w:gz", dereference=False) as tar:
                        for item in work.iterdir():
                            tar.add(item, arcname=item.name, recursive=True)
                        from .backups import PANEL_DATA
                        for item in self.p.data.iterdir():
                            if item.name in PANEL_DATA:
                                tar.add(item, arcname="data/" + item.name, recursive=True)
                finally:
                    # Restore the previous service state even after disk or SQL errors.
                    if "cache" in running:
                        self.dc("up", "-d", "--wait", "cache")
                    if "panel" in running:
                        self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel")
                    if "database" not in running:
                        self.dc("stop", "database")
            temporary.chmod(0o600)
            os.replace(temporary, target)
            write_checksum(target)
            from .retention import prune
            prune(self)
            self.ui.say(f"Backup created: {target} (contains secrets; keep an off-server copy)", "ok")
            return target
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def restore_panel(self):
        archive = Path(self.ui.ask("Absolute path to the .tar.gz backup", validator=lambda s: Path(s).is_absolute() and Path(s).is_file()))
        checksum = Path(str(archive) + ".sha256")
        if not checksum.is_file():
            raise InstallerError("The backup's .sha256 file is missing.")
        recorded = checksum.read_text().split()
        if not recorded or file_sha256(archive) != recorded[0]:
            raise InstallerError("Backup checksum does not match.")
        from .backups import panel_archive
        panel_archive(archive)
        # Restore only into an empty target so keys and schemas never mix.
        if self.p.panel.exists() and any(self.p.panel.iterdir()):
            raise InstallerError("The target must be empty. Back up and uninstall/purge the current Panel first.")
        self.ui.confirm("RESTORE PANEL")
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.p.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=self.p.base) as name:
            work = Path(name)
            secure_extract(archive, work)
            manifest = self.load(work / "manifest.json")
            if manifest.get("type") != "panel-backup":
                raise InstallerError("This file is not a Panel backup.")
            state = self.load(work / "state.json")
            if state.get("method") == "standalone":
                return self.native(state).restore(work)
            self.docker()
            if not (work / "data/var/.env").is_file() or not (work / "database.sql").is_file():
                raise InstallerError("The backup is missing SQL or the Panel's original keys.")
            self.p.panel.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Same filesystem: keep owners, permissions and certificate symlinks.
            os.replace(work / "data", self.p.data)
            state["status"] = "restoring"
            self.save_panel(state)
            self.run.secrets.add(state["db_password"])
            for folder in ("database", "logs", "redis", "nginx", "certs", "extensions", "extension-assets"):
                (self.p.data / folder).mkdir(exist_ok=True, mode=0o700)
            self.dc("pull", label="Downloading the backup's original images")
            self.prepare_mysql_client(state)
            self.dc("up", "-d", "--wait", "--wait-timeout", "240", "database", "cache")
            with (work / "database.sql").open("rb") as stream:
                self.dc("exec", "-T", "database", "sh", "-c",
                        self.database_command(state, dump=False),
                        input_file=stream, label="Restoring SQL with its original application keys")
            self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Starting the restored Panel")
            state["status"] = "ready"
            self.save_panel(state)
        self.ui.say("Panel restored. Keep the original domain/IP or update DNS and configuration.", "ok")

    def update_panel(self):
        state = self.panel_state()
        if state["status"] != "ready":
            raise InstallerError("Complete/resume the current Panel installation first.")
        release = self.choose_release("panel", "auto")
        if release["tag_name"] == state["version"]:
            self.ui.say("This version is already installed.", "ok")
            return
        if version_key(release["tag_name"]) < version_key(state["version"]):
            raise InstallerError("Panel downgrades are not allowed. Restore a backup from that version.")
        previous = state["version"]
        from .compat import check
        check(self, panel=release["tag_name"])
        self.ui.card("Panel update plan", [
            self.ui.tr("Update: {current} → {target}").format(current=previous, target=release["tag_name"]),
            "A verified backup is created before any code or database change.",
            "The Panel is unavailable while the new migrations run.",
            self.ui.tr("If anything fails, you can roll back to {version}.").format(version=previous)])
        self.ui.confirm("UPDATE PANEL")
        try:
            if state.get("method") == "standalone":
                self.native(state).update(release)
            else:
                self.update_docker_panel(state, release)
        except InstallerError as exc:
            self.offer_rollback(previous, exc)
            raise
        self.ui.say(self.ui.tr("If something looks wrong, choose Roll back Panel update to return to {version}.").format(version=previous), "muted")

    def update_docker_panel(self, state, release):
        previous = state["version"]
        # Download before stopping the Panel; back up before changing the schema.
        image = self.pin_image("ghcr.io/pterodactyl/panel:" + release["tag_name"])
        backup = self.backup_panel()
        self.dc("stop", "panel", label="Stopping the Panel for its update")
        state.update(version=release["tag_name"], panel_image=image, status="updating", recovery_backup=str(backup),
                     previous_version=previous)
        self.save_panel(state)
        try:
            self.prepare_mysql_client(state)
            self.dc("run", "--rm", "-T", "panel", "php", "artisan", "migrate", "--seed", "--force", "--no-interaction",
                    label="Applying the new release's migrations")
            self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Checking the updated Panel")
        except BaseException:
            self.ui.say(f"Update incomplete. Panel stopped. Recovery backup: {backup}", "error")
            # Never start old code on a schema that may have changed.
            self.dc("stop", "panel", check=False)
            raise
        state["status"] = "ready"
        self.save_panel(state)
        self.ui.say("Panel updated and checked with the official healthcheck.", "ok")

    def offer_rollback(self, previous, error):
        """After a failed update, offer the matching rollback; otherwise keep the Panel stopped."""
        from .rollback import available
        try:
            state = self.panel_state()
        except InstallerError:
            return
        if state.get("status") != "updating" or not available(state):
            return
        self.ui.say(self.run.redact(str(error)), "error")
        if not self.ui.yes(self.ui.tr("Roll back to {version} now?").format(version=previous), True):
            self.ui.say("The Panel stays stopped. Choose Resume installation to retry, or Roll back Panel update.", "warn")
            return
        self.rollback_panel(after_failure=True)
        raise InstallerError(self.ui.tr("The update failed and the Panel was rolled back to {version}.").format(version=previous)) from error

    def rollback_panel(self, after_failure=False):
        from .rollback import rollback
        return rollback(self, after_failure)

    def uninstall_panel(self, confirmed=False):
        state = self.panel_state()
        if state.get("method") == "standalone":
            return self.native(state).uninstall(confirmed)
        if not confirmed:
            self.ui.confirm("UNINSTALL PANEL")
        purge = self.ui.yes("Also delete the Panel database and files? A backup is created first.")
        if purge:
            self.ui.confirm("DELETE PANEL")
        backup = self.backup_panel()
        self.dc("down", label="Removing only this Panel's containers and network")
        if purge:
            if self.p.panel.is_symlink() or self.p.panel.parent != self.p.base:
                raise InstallerError("Unsafe removal path.")
            shutil.rmtree(self.p.panel)
        else:
            state["status"] = "stopped"
            self.save_panel(state)
        self.ui.say(f"Panel uninstalled. Backup: {backup}", "ok")
        if not purge:
            self.ui.say("Data preserved. Install Panel can reactivate it.")
