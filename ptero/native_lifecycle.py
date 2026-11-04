"""Standalone Panel lifecycle: install, health, backup, restore, update, rollback and removal."""
import contextlib
import http.client
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

from .common import InstallerError, atomic_write, write_checksum, write_json


class NativeLifecycleMixin:
    def install(self):
        self.p.panel.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.p.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        from .phases import Phases
        phases = Phases(self.i, self.s, self.save)
        phases.run("Dependencies", self.dependencies, lambda: self.php.exists() and (self.p.base / "tools/composer").exists() and self.run.run(["systemctl", "is-active", "--quiet", self.db_unit], check=False)[0] == 0)
        phases.run("Panel database", self.provision_database)
        self.pause()
        if self.s.get("source_installed") != self.s["version"] or not (self.app / "artisan").is_file():
            phases.run("Panel source", lambda: self.source(self.i.release("panel", self.s["version"])))
        self.application_environment()
        phases.run("PHP dependencies", lambda: self.run.run([self.php, self.p.base / "tools/composer", "install", "--working-dir=" + str(self.app),
                      "--no-dev", "--optimize-autoloader", "--no-interaction", "--no-progress", "--no-scripts", "--no-plugins"],
                     label="Installing verified PHP dependencies", env={"COMPOSER_ALLOW_SUPERUSER": "1"}), lambda: (self.app / "vendor/autoload.php").is_file())
        self.run.run(["chown", "-R", "www-data:www-data", self.app])
        self.setup_runtime()
        self.artisan("config:clear")
        self.artisan("package:discover", "--no-interaction")
        phases.run("Panel migrations", lambda: self.artisan("migrate", "--seed", "--force", "--no-interaction", label="Running Panel migrations"))
        if not self.s.get("admin_created"):
            self.i.create_admin(self.s)
            self.s["admin_created"] = True
            self.save()
        try:
            phases.run("Panel certificate", self.certificate)
            phases.run("Web configuration", self.publish_site)
            phases.run("Service health", self.start)
        except BaseException:
            self.pause()
            raise
        self.s["status"] = "ready"
        self.save()
        self.ui.say("Standalone Panel is ready: {url}".format(url=self.s["url"]), "ok")
        self.i.setup_game_database(self.s)

    def pause(self):
        for unit in (self.scheduler, self.queue):
            if (self.units / unit).exists():
                self.run.run(["systemctl", "stop", unit])
        if self.enabled.exists():
            self.publish_site(bootstrap=True)

    def start(self):
        self.run.run(["systemctl", "enable", "--now", self.queue, self.scheduler], label="Starting queue worker and scheduler")
        self.health()

    def health(self):
        self.run.run(["nginx", "-t"])
        for unit in (self.db_unit, self.redis_unit, "php" + self.s.get("php", "8.3") + "-fpm", self.queue, self.scheduler, "nginx"):
            self.run.run(["systemctl", "is-active", "--quiet", unit])
        self.artisan("migrate:status", "--no-interaction", label="Checking database migrations")
        address = "127.0.0.1" if self.s.get("bind", "0.0.0.0") == "0.0.0.0" else self.s["bind"]
        with self.ui.busy("Waiting for Panel HTTP readiness"):
            for attempt in range(15):
                connection = http.client.HTTPConnection(address, self.s["http_port"], timeout=5)
                try:
                    connection.request("GET", "/", headers={"Host": urllib.parse.urlsplit(self.s["url"]).netloc})
                    response = connection.getresponse()
                    response.read(4096)
                    if response.status in (200, 301, 302, 303):
                        return
                    if attempt == 14:
                        raise InstallerError("The Panel returned an unexpected HTTP status: " + str(response.status))
                except (OSError, http.client.HTTPException):
                    if attempt == 14:
                        raise InstallerError("The Panel HTTP endpoint did not become ready.")
                finally:
                    connection.close()
                time.sleep(1)

    @contextlib.contextmanager
    def client_config(self):
        with tempfile.TemporaryDirectory(dir=self.p.base) as directory:
            config = Path(directory) / "client.cnf"
            atomic_write(config, f"[client]\nhost=127.0.0.1\nuser=ptero_panel\npassword={self.s['db_password']}\n")
            yield config

    def backup(self):
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.p.backups / ("panel-standalone-" + self.i.stamp() + ".tar.gz")
        partial = Path(str(target) + ".partial")
        active = {unit for unit in (self.queue, self.scheduler, self.redis_unit)
                  if self.run.run(["systemctl", "is-active", "--quiet", unit], check=False)[0] == 0}
        was_web = self.enabled.exists() and self.s["status"] == "ready"
        try:
            with tempfile.TemporaryDirectory(dir=self.p.backups) as directory:
                work = Path(directory)
                try:
                    self.pause()
                    with self.client_config() as config, (work / "database.sql").open("wb") as output:
                        command = ["mysqldump" if self.mysql else "mariadb-dump", "--defaults-extra-file=" + str(config),
                                   "--single-transaction", "--quick", "--hex-blob", "--triggers"]
                        if self.mysql:
                            command += ["--no-tablespaces", "--column-statistics=0"]
                        self.run.run([*command, "ptero_panel"], output=output, label="Exporting the Panel database")
                    if not (work / "database.sql").stat().st_size:
                        raise InstallerError("SQL dump is empty; the backup was not accepted.")
                    if self.redis_unit in active:
                        self.run.run(["systemctl", "stop", self.redis_unit])
                    write_json(work / "state.json", self.s)
                    write_json(work / "manifest.json", {"schema": 1, "owner": "ptero-installer", "type": "panel-backup", "method": "standalone"})
                    with self.ui.busy("Compressing application, original keys and private Redis"), tarfile.open(partial, "w:gz") as tar:
                        for file in work.iterdir():
                            tar.add(file, arcname=file.name)
                        tar.add(self.app, arcname="app")
                        if self.redis_data.exists():
                            tar.add(self.redis_data, arcname="redis")
                        if self.p.data.exists():
                            from .backups import PANEL_DATA
                            for item in self.p.data.iterdir():
                                if item.name in PANEL_DATA:
                                    tar.add(item, arcname="data/" + item.name)
                finally:
                    for unit in (self.redis_unit, self.queue, self.scheduler):
                        if unit in active:
                            self.run.run(["systemctl", "start", unit])
                    if was_web:
                        self.publish_site()
            partial.chmod(0o600)
            os.replace(partial, target)
            write_checksum(target)
            from .retention import prune
            prune(self.i)
            self.ui.say("Backup created: {path}".format(path=target), "ok")
            return target
        finally:
            partial.unlink(missing_ok=True)

    def restore(self, work):
        if self.app.exists() and any(self.app.iterdir()):
            raise InstallerError("The standalone application directory must be empty before restore.")
        if self.redis_data.exists() and any(self.redis_data.iterdir()):
            raise InstallerError("The standalone Redis directory must be empty before restore.")
        if not (work / "app/.env").is_file() or not (work / "database.sql").is_file():
            raise InstallerError("Backup is missing the original .env or SQL dump.")
        self.s["status"] = "restoring"
        self.s["database_claimed"] = False
        self.save()
        self.dependencies()
        self.provision_database()
        self.app.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.app.exists():
            self.app.rmdir()
        shutil.copytree(work / "app", self.app, symlinks=True)
        if (work / "data").exists():
            shutil.copytree(work / "data", self.p.data, symlinks=True, dirs_exist_ok=True)
        if (work / "redis").exists():
            shutil.copytree(work / "redis", self.redis_data, symlinks=True, dirs_exist_ok=True)
        self.run.run(["chown", "-R", "www-data:www-data", self.app])
        self.setup_runtime()
        with self.client_config() as config, (work / "database.sql").open("rb") as stream:
            self.run.run([self.db_client, "--defaults-extra-file=" + str(config), "ptero_panel"], input_file=stream,
                         label="Restoring SQL with the original application keys")
        self.renewal_units()
        self.publish_site()
        self.start()
        self.s["status"] = "ready"
        self.save()

    def update(self, release):
        backup = self.backup()
        self.pause()
        self.s.update(status="updating", version=release["tag_name"], recovery_backup=str(backup),
                      previous_version=self.s["version"])
        self.save()
        try:
            self.source(release)
            self.install()
        except BaseException:
            self.pause()
            self.ui.say("Update incomplete. Panel remains in maintenance. Recovery backup: {path}".format(path=backup), "error")
            raise

    def rollback(self, work, saved):
        """Reinstate the pre-update application and SQL from an extracted recovery backup."""
        from .rollback import application_keys
        if not (work / "app/artisan").is_file() or not (work / "app/.env").is_file():
            raise InstallerError("Backup is missing the original .env or SQL dump.")
        if self.app.exists() and application_keys(work / "app/.env") != application_keys(self.app / ".env"):
            raise InstallerError("The recovery backup has different application keys; use Restore Panel from backup instead.")
        self.pause()
        stamp = self.i.stamp()
        staged = self.app.with_name(".ptero-rollback-" + stamp)
        shutil.copytree(work / "app", staged, symlinks=True)
        if (self.app / "storage/logs").is_dir():
            # Keep logs written since the backup; everything else matches the old release.
            shutil.copytree(self.app / "storage/logs", staged / "storage/logs", symlinks=True, dirs_exist_ok=True)
        old = self.app.with_name(".ptero-previous-" + stamp)
        if self.app.exists():
            os.replace(self.app, old)
        try:
            os.replace(staged, self.app)
        except BaseException:
            if old.exists():
                os.replace(old, self.app)
            raise
        # Tables added by newer migrations must not survive: recreate the database.
        self.sql("DROP DATABASE IF EXISTS ptero_panel;\n")
        self.provision_database()
        with self.client_config() as config, (work / "database.sql").open("rb") as stream:
            self.run.run([self.db_client, "--defaults-extra-file=" + str(config), "ptero_panel"], input_file=stream,
                         label="Restoring the pre-update database")
        self.s.update(version=saved["version"], source_installed=saved["version"])
        self.save()
        self.run.run(["chown", "-R", "www-data:www-data", self.app])
        self.application_environment()
        self.artisan("config:clear")
        self.artisan("view:clear")
        self.publish_site()
        self.start()
        shutil.rmtree(old, ignore_errors=True)

    def uninstall(self, confirmed=False):
        if not confirmed:
            self.ui.confirm("UNINSTALL PANEL")
        purge = self.ui.yes("Also delete the Panel application and database? A backup is created first.")
        if purge:
            self.ui.confirm("DELETE PANEL")
        backup = self.backup()
        self.pause()
        for unit in (self.queue, self.scheduler, self.redis_unit, self.cert_timer):
            if (self.units / unit).exists():
                self.run.run(["systemctl", "disable", "--now", unit])
        for name in (self.queue, self.scheduler, self.redis_unit, self.cert_timer,
                     "ptero-panel-scheduler.service", "ptero-panel-cert.service"):
            (self.units / name).unlink(missing_ok=True)
        self.enabled.unlink(missing_ok=True)
        self.site.unlink(missing_ok=True)
        self.pool.unlink(missing_ok=True)
        self.run.run(["nginx", "-t"])
        self.run.run(["systemctl", "reload", "nginx"])
        self.run.run(["systemctl", "reload", "php" + self.s.get("php", "8.3") + "-fpm"])
        self.run.run(["systemctl", "daemon-reload"])
        if purge:
            self.sql("DROP DATABASE ptero_panel;\nDROP USER 'ptero_panel'@'127.0.0.1';\n")
            if self.app.is_symlink() or self.p.panel.is_symlink():
                raise InstallerError("Unsafe removal path.")
            shutil.rmtree(self.app)
            shutil.rmtree(self.p.panel)
            shutil.rmtree(self.redis_data, ignore_errors=True)
            self.redis_config.unlink(missing_ok=True)
        else:
            self.s["status"] = "stopped"
            self.save()
        self.ui.say("Panel uninstalled. Backup: {path}".format(path=backup), "ok")
