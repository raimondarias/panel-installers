"""Panel lifecycle: state, Compose configuration, install, backup, restore, update and removal."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile

from .common import (PROJECT, SCHEMA, InstallerError, atomic_write, secure_extract, valid_host,
                     version_key, write_json)


class PanelMixin:
    def panel_state(self):
        if not self.p.state.is_file():
            raise InstallerError("Install the Panel first. Start with Install Panel or Install Panel and Wings.")
        state = self.load(self.p.state)
        settings = state.get("game_database", {})
        credentials = [state.get("db_password"), state.get("mail_password", ""), state.get("redis_password", "")]
        if not isinstance(settings, dict) or not all(isinstance(value, str) for value in credentials):
            raise InstallerError("La instalación o copia no pertenece a este instalador.")
        if not isinstance(settings.get("password", ""), str):
            raise InstallerError("La instalación o copia no pertenece a este instalador.")
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
        # Compose interpola $ incluso en JSON; $$ transmite un dólar literal.
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
                     mail_host=self.ui.ask("Host SMTP", validator=valid_host),
                     mail_port=self.ui.ask("Puerto SMTP", "587", self.valid_port),
                     mail_user=self.ui.ask("Usuario SMTP"),
                     mail_password=self.ui.ask("Contraseña SMTP", secret=True),
                     mail_encryption=self.ui.ask("Cifrado SMTP (tls/ssl/null)", "tls",
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
                raise InstallerError("Restauración SQL incompleta. Conserva el backup y repite la restauración en un destino vacío.")
            if state["status"] == "ready":
                self.ui.say("El Panel ya está instalado. Usa actualizar, reiniciar o configurar SMTP.", "ok")
                if state.get("game_database", {}).get("mode", "later") != "later" and not state["game_database"].get("registered"):
                    self.setup_game_database(state)
                return
            self.ui.say(f"Instalación gestionada existente: {state['status']}, {state['version']}.")
            self.ui.confirm("REANUDAR PANEL")
        else:
            if self.p.panel.exists() and any(self.p.panel.iterdir()):
                raise InstallerError("El directorio de destino contiene archivos sin registro; no se sobrescriben.")
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
        # El entrypoint da .env a nginx, pero no cambia el dueño de /app/var.
        # El bind mount debe permitir a nginx atravesar ese directorio.
        # Los padres privados de /opt protegen los secretos en el host.
        (self.p.data / "var").chmod(0o755)
        for key, ref in (("panel_image", "ghcr.io/pterodactyl/panel:" + state["version"]),
                         ("database_image", "mysql:8.4" if state.get("database_engine") == "mysql" else "mariadb:11.4"),
                         ("cache_image", "redis:7.4-alpine")):
            if key not in state:
                state[key] = self.pin_image(ref)
                self.save_panel(state)
        self.prepare_mysql_client(state)
        self.dc("config", "--quiet")
        phases.run("SQL and cache", lambda: self.dc("up", "-d", "--wait", "--wait-timeout", "240", "database", "cache", label="Preparando base de datos y Redis"))
        self.dc("stop", "panel", label="Manteniendo el Panel detenido durante las migraciones")
        phases.run("Panel migrations", lambda: self.dc("run", "--rm", "-T", "panel", "php", "artisan", "migrate", "--seed", "--force", "--no-interaction",
                label="Migraciones y eggs oficiales"))
        if not state.get("admin_created"):
            self.ui.say("Creating the first administrator")
            self.create_admin(state)
            state["admin_created"] = True
            self.save_panel(state)
        phases.run("Service health", lambda: self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Iniciando Panel, colas y scheduler"))
        state["status"] = "ready"
        self.save_panel(state)
        self.ui.say(f"Panel disponible: {state['url']}", "ok")
        self.ui.say("Guarda fuera del servidor una copia de data/var/.env (APP_KEY y HASHIDS_SALT).")
        self.setup_game_database(state)

    def setup_game_database(self, state=None):
        from .game_database import GameDatabase
        return GameDatabase(self).setup(state or self.panel_state())

    def configure_game_database(self):
        from .game_database import plan
        state = self.panel_state()
        if state["status"] != "ready":
            raise InstallerError("El Panel no está listo; reanuda la instalación/actualización primero.")
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
                        self.dc("stop", "panel", label="Pausando Panel para una copia consistente")
                    if "database" not in running:
                        self.dc("up", "-d", "--wait", "--wait-timeout", "240", "database")
                    with (work / "database.sql").open("wb") as stream:
                        self.dc("exec", "-T", "database", "sh", "-c",
                                self.database_command(state, dump=True),
                                output=stream, label="Exportando base de datos")
                    if not (work / "database.sql").stat().st_size:
                        raise InstallerError("El dump SQL está vacío; no se acepta la copia.")
                    if "cache" in running:
                        self.dc("stop", "cache", label="Pausando Redis para guardar las colas")
                    write_json(work / "state.json", state)
                    write_json(work / "manifest.json", {"schema": SCHEMA, "owner": PROJECT, "type": "panel-backup"})
                    with self.ui.busy("Comprimiendo configuración, claves y datos"), tarfile.open(temporary, "w:gz", dereference=False) as tar:
                        for item in work.iterdir():
                            tar.add(item, arcname=item.name, recursive=True)
                        from .backups import PANEL_DATA
                        for item in self.p.data.iterdir():
                            if item.name in PANEL_DATA:
                                tar.add(item, arcname="data/" + item.name, recursive=True)
                finally:
                    # Restaurar el estado previo incluso ante errores de disco/SQL.
                    if "cache" in running:
                        self.dc("up", "-d", "--wait", "cache")
                    if "panel" in running:
                        self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel")
                    if "database" not in running:
                        self.dc("stop", "database")
            temporary.chmod(0o600)
            os.replace(temporary, target)
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            atomic_write(Path(str(target) + ".sha256"), digest.hexdigest() + "\n")
            self.ui.say(f"Backup creado: {target} (contiene secretos; guárdalo fuera del servidor)", "ok")
            return target
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def restore_panel(self):
        archive = Path(self.ui.ask("Ruta absoluta al backup .tar.gz", validator=lambda s: Path(s).is_absolute() and Path(s).is_file()))
        checksum = Path(str(archive) + ".sha256")
        if not checksum.is_file():
            raise InstallerError("Falta el archivo .sha256 del backup.")
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != checksum.read_text().strip():
            raise InstallerError("El checksum del backup no coincide.")
        from .backups import panel_archive
        panel_archive(archive)
        # La restauración se realiza en destino vacío para no mezclar claves ni esquemas.
        if self.p.panel.exists() and any(self.p.panel.iterdir()):
            raise InstallerError("El destino debe estar vacío. Desinstala/purga el Panel actual después de respaldarlo.")
        self.ui.confirm("RESTAURAR PANEL")
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.p.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=self.p.base) as name:
            work = Path(name)
            secure_extract(archive, work)
            manifest = self.load(work / "manifest.json")
            if manifest.get("type") != "panel-backup":
                raise InstallerError("El archivo no es un backup de Panel.")
            state = self.load(work / "state.json")
            if state.get("method") == "standalone":
                return self.native(state).restore(work)
            self.docker()
            if not (work / "data/var/.env").is_file() or not (work / "database.sql").is_file():
                raise InstallerError("La copia no contiene SQL y claves originales del Panel.")
            self.p.panel.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Mismo filesystem: conservar dueños, permisos y symlinks de certificados.
            os.replace(work / "data", self.p.data)
            state["status"] = "restoring"
            self.save_panel(state)
            self.run.secrets.add(state["db_password"])
            for folder in ("database", "logs", "redis", "nginx", "certs", "extensions", "extension-assets"):
                (self.p.data / folder).mkdir(exist_ok=True, mode=0o700)
            self.dc("pull", label="Descargando imágenes originales de la copia")
            self.prepare_mysql_client(state)
            self.dc("up", "-d", "--wait", "--wait-timeout", "240", "database", "cache")
            with (work / "database.sql").open("rb") as stream:
                self.dc("exec", "-T", "database", "sh", "-c",
                        self.database_command(state, dump=False),
                        input_file=stream, label="Restaurando SQL con sus claves originales")
            self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Arrancando Panel restaurado")
            state["status"] = "ready"
            self.save_panel(state)
        self.ui.say("Panel restaurado. Mantén el dominio/IP original o adapta DNS y configuración.", "ok")

    def update_panel(self):
        state = self.panel_state()
        if state["status"] != "ready":
            raise InstallerError("Completa/reanuda primero la instalación actual del Panel.")
        release = self.choose_release("panel", "auto")
        if release["tag_name"] == state["version"]:
            self.ui.say("Ya tienes esa versión.", "ok")
            return
        if version_key(release["tag_name"]) < version_key(state["version"]):
            raise InstallerError("No se permiten downgrades del Panel; restaura un backup de esa versión.")
        self.ui.confirm("ACTUALIZAR PANEL")
        if state.get("method") == "standalone":
            return self.native(state).update(release)
        # Descargar antes de interrumpir el Panel; copia antes de modificar el esquema.
        image = self.pin_image("ghcr.io/pterodactyl/panel:" + release["tag_name"])
        backup = self.backup_panel()
        self.dc("stop", "panel", label="Deteniendo Panel para actualizar")
        state.update(version=release["tag_name"], panel_image=image, status="updating", recovery_backup=str(backup))
        self.save_panel(state)
        try:
            self.prepare_mysql_client(state)
            self.dc("run", "--rm", "-T", "panel", "php", "artisan", "migrate", "--seed", "--force", "--no-interaction",
                    label="Aplicando migraciones de la versión nueva")
            self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Verificando nuevo Panel")
        except BaseException:
            self.ui.say(f"Actualización incompleta. Panel detenido; copia de recuperación: {backup}", "error")
            # No arrancar código viejo con un esquema que puede haber cambiado.
            self.dc("stop", "panel", check=False)
            raise
        state["status"] = "ready"
        self.save_panel(state)
        self.ui.say("Panel actualizado y comprobado por el healthcheck oficial.", "ok")

    def uninstall_panel(self, confirmed=False):
        state = self.panel_state()
        if state.get("method") == "standalone":
            return self.native(state).uninstall(confirmed)
        if not confirmed:
            self.ui.confirm("DESINSTALAR PANEL")
        purge = self.ui.yes("¿Borrar también base de datos y archivos del Panel? (antes se crea backup)")
        if purge:
            self.ui.confirm("BORRAR PANEL")
        backup = self.backup_panel()
        self.dc("down", label="Eliminando solo contenedores y red de este Panel")
        if purge:
            if self.p.panel.is_symlink() or self.p.panel.parent != self.p.base:
                raise InstallerError("Ruta de borrado no segura.")
            shutil.rmtree(self.p.panel)
        else:
            state["status"] = "stopped"
            self.save_panel(state)
        self.ui.say(f"Panel desinstalado. Backup: {backup}", "ok")
        if not purge:
            self.ui.say("Datos conservados. «Instalar Panel» permite reactivarlo.")
