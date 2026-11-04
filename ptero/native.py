"""Standalone Panel backend. No Docker dependency for the Panel itself."""
import base64
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

from .core import InstallerError, atomic_write, write_json, secure_extract


def dotenv(values):
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\n', '\\n').replace('\r', '\\r') + '"'
    return ''.join(f'{key}={quote(value)}\n' for key, value in values.items())


class NativeBackend:
    def __init__(self, installer, state):
        self.i, self.p, self.s = installer, installer.p, state
        self.run, self.ui = installer.run, installer.ui
        self.php = self.p.root / ("usr/bin/php" + state.get("php", "8.3"))
        self.app = self.p.root / "var/www/pterodactyl"
        self.units = self.p.root / "etc/systemd/system"
        self.site = self.p.root / "etc/nginx/sites-available/ptero-installer.conf"
        self.enabled = self.p.root / "etc/nginx/sites-enabled/ptero-installer.conf"
        self.pool = self.p.root / ("etc/php/" + state.get("php", "8.3") + "/fpm/pool.d/ptero-installer.conf")
        self.redis_data = self.p.root / "var/lib/pterodactyl-installer/redis"
        self.redis_config = self.p.root / "etc/pterodactyl-installer/redis.conf"
        self.acme = self.p.root / "var/lib/pterodactyl-installer/acme"
        self.certdir = self.p.data / "certs"
        self.mysql = state.get("database_engine", "mariadb") == "mysql"
        self.db_client = "mysql" if self.mysql else "mariadb"
        self.db_unit = "mysql" if self.mysql else "mariadb"
        self.queue = "ptero-panel-queue.service"
        self.scheduler = "ptero-panel-scheduler.timer"
        self.redis_unit = "ptero-panel-redis.service"
        self.cert_timer = "ptero-panel-cert.timer"

    def save(self):
        self.i.save_panel(self.s)

    def apt(self, packages):
        self.run.run(["apt-get", "install", "-y", *packages], label="Installing standalone dependencies")

    def repository(self, name, key_url, repository):
        key = self.p.root / f"etc/apt/keyrings/ptero-{name}.asc"
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        key.parent.chmod(0o755)
        text = self.i.fetch(key_url).decode("ascii")
        if "BEGIN PGP PUBLIC KEY BLOCK" not in text:
            raise InstallerError("Invalid repository signing key.")
        atomic_write(key, text, 0o644)
        atomic_write(self.p.root / f"etc/apt/sources.list.d/ptero-{name}.list",
                     f"deb [signed-by={key}] {repository}\n", 0o644)

    def dependencies(self):
        release = self.i.check_host()
        self.check_php_availability(release)
        opposites = ("mariadb-server",) if self.mysql else ("mysql-server", "mysql-community-server")
        for package in opposites:
            code, status = self.run.run(["dpkg-query", "-W", "-f=${Status}", package], check=False)
            if code == 0 and "install ok installed" in status:
                raise InstallerError("A different database engine is already installed. Choose the matching engine or Docker.")
        selected = ("mysql-server", "mysql-community-server") if self.mysql else ("mariadb-server",)
        existing = False
        for package in selected:
            code, status = self.run.run(["dpkg-query", "-W", "-f=${Status}|${Version}", package], check=False)
            if code == 0 and "install ok installed|" in status:
                existing = True
                version = re.search(r"\|(?:\d+:)?(\d+)\.(\d+)", status)
                if version and ((self.mysql and int(version[1]) < 8) or
                                (not self.mysql and (int(version[1]), int(version[2])) < (10, 11))):
                    raise InstallerError("The existing database server is too old for Panel v2. Choose Docker or upgrade it separately.")
        if "created_database_service" not in self.s:
            self.s["created_database_service"] = not existing
            self.save()
        _, redis_status = self.run.run(["dpkg-query", "-W", "-f=${Status}", "redis-server"], check=False)
        self.run.run(["apt-get", "update"], label="Updating APT indexes")
        self.apt(["ca-certificates", "curl", "gnupg", "unzip", "tar", "nginx", "redis-server", "certbot"])
        if "install ok installed" not in redis_status:
            # This package's new default instance is unused; the Panel gets its own instance.
            self.run.run(["systemctl", "disable", "--now", "redis-server"])
        php = self.s.get("php", "8.3")
        _, available = self.run.run(["apt-cache", "policy", "php" + php + "-cli"])
        if "Candidate: (none)" in available or not available.strip():
            if release["ID"] == "ubuntu":
                self.apt(["software-properties-common"])
                self.run.run(["add-apt-repository", "-y", "ppa:ondrej/php"], label="Adding the PHP repository")
            else:
                keyring = self.p.base / "debsuryorg-archive-keyring.deb"
                keyring.write_bytes(self.i.fetch("https://packages.sury.org/debsuryorg-archive-keyring.deb"))
                self.run.run(["dpkg", "-i", keyring])
                atomic_write(self.p.root / "etc/apt/sources.list.d/ptero-php.list",
                             f"deb [signed-by=/usr/share/keyrings/debsuryorg-archive-keyring.gpg] https://packages.sury.org/php/ {release['VERSION_CODENAME']} main\n", 0o644)
            self.run.run(["apt-get", "update"], label="Updating PHP packages")
        self.apt(["php" + php + "-" + extension for extension in
                  ("cli", "common", "fpm", "gd", "mysql", "mbstring", "bcmath", "xml", "curl", "zip", "intl")])
        if self.mysql and release["ID"] == "debian":
            if platform.machine() != "x86_64":
                raise InstallerError("Native MySQL on Debian requires amd64. Choose MariaDB or Docker on ARM64.")
            self.repository("mysql", "https://repo.mysql.com/RPM-GPG-KEY-mysql-2025",
                            f"https://repo.mysql.com/apt/debian/ {release['VERSION_CODENAME']} mysql-8.4-lts")
            self.run.run(["apt-get", "update"], label="Updating database packages")
            self.apt(["mysql-community-server", "mysql-community-client"])
        elif not self.mysql and release["ID"] == "ubuntu" and release["VERSION_ID"] == "22.04":
            self.repository("mariadb", "https://mariadb.org/mariadb_release_signing_key.asc",
                            "https://mirror.mariadb.org/repo/11.4/ubuntu jammy main")
            self.run.run(["apt-get", "update"], label="Updating database packages")
            self.apt(["mariadb-server", "mariadb-client"])
        else:
            self.apt(["mysql-server", "mysql-client"] if self.mysql else ["mariadb-server", "mariadb-client"])
        if self.s["created_database_service"]:
            directory = "mysql.conf.d" if self.mysql else "mariadb.conf.d"
            config = "[mysqld]\nbind-address=127.0.0.1\n" + ("loose-mysqlx-bind-address=127.0.0.1\n" if self.mysql else "")
            atomic_write(self.p.root / "etc/mysql" / directory / "99-ptero-installer.cnf", config, 0o644)
            self.run.run(["systemctl", "restart", self.db_unit], label="Binding the new database service to localhost")
        self.run.run(["systemctl", "enable", "--now", self.db_unit])
        self.composer()

    def check_php_availability(self, release):
        if release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "26.04":
            _, policy = self.run.run(["apt-cache", "policy", "php" + self.s.get("php", "8.3") + "-cli"], check=False)
            if not policy.strip() or "Candidate: (none)" in policy:
                raise InstallerError("Ubuntu 26.04 needs a configured PHP 8.3/8.4 package source for standalone. Use Docker or Ubuntu 24.04.")

    def composer(self):
        tool = self.p.base / "tools/composer"
        if tool.exists():
            return
        tool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        setup = tool.parent / "composer-setup.php"
        content = self.i.fetch("https://getcomposer.org/installer")
        expected = self.i.fetch("https://composer.github.io/installer.sig").decode().strip()
        if hashlib.sha384(content).hexdigest() != expected:
            raise InstallerError("Composer installer signature does not match.")
        try:
            setup.write_bytes(content)
            self.run.run([self.php, setup, "--2", "--install-dir=" + str(tool.parent), "--filename=composer"],
                         label="Installing verified Composer 2")
        finally:
            setup.unlink(missing_ok=True)

    def sql(self, sql, output=None, application=False):
        with tempfile.TemporaryFile() as stream:
            stream.write(sql.encode())
            stream.seek(0)
            self.run.run([self.db_client, "--batch", "--skip-column-names"], input_file=stream, output=output,
                         label="Provisioning the Panel database")

    def provision_database(self):
        if not self.s.get("database_claimed"):
            _, count = self.run.run([self.db_client, "--batch", "--skip-column-names", "--execute",
                                    "SELECT (SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='ptero_panel') + (SELECT COUNT(*) FROM mysql.user WHERE User='ptero_panel');"])
            if count.strip() != "0":
                raise InstallerError("The reserved ptero_panel database/user already exists and is not managed by this installation.")
            self.s["database_claimed"] = True
            self.save()
        password = self.s["db_password"]
        if not all(c in "0123456789abcdef" for c in password):
            raise InstallerError("Invalid generated database password.")
        self.sql("CREATE DATABASE IF NOT EXISTS ptero_panel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
                 f"CREATE USER IF NOT EXISTS 'ptero_panel'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
                 f"ALTER USER 'ptero_panel'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
                 "GRANT ALL PRIVILEGES ON ptero_panel.* TO 'ptero_panel'@'127.0.0.1';\n")

    def application_environment(self):
        env = {"APP_ENV": "production", "APP_DEBUG": "false", "APP_URL": self.s["url"],
               "APP_TIMEZONE": self.s["timezone"], "APP_ENVIRONMENT_ONLY": "true",
               "APP_SERVICE_AUTHOR": self.s["email"], "DB_CONNECTION": "mysql", "DB_HOST": "127.0.0.1",
               "DB_PORT": "3306", "DB_DATABASE": "ptero_panel", "DB_USERNAME": "ptero_panel",
               "DB_PASSWORD": self.s["db_password"], "CACHE_STORE": "redis", "SESSION_DRIVER": "redis",
               "SESSION_CONNECTION": "sessions", "QUEUE_CONNECTION": "redis", "REDIS_HOST": "127.0.0.1",
               "REDIS_PORT": "16379", "REDIS_PASSWORD": self.s["redis_password"],
               "MAIL_MAILER": self.s.get("mail_mailer", "log"), "MAIL_FROM_ADDRESS": self.s["email"],
               "MAIL_HOST": self.s.get("mail_host", ""), "MAIL_PORT": self.s.get("mail_port", "587"),
               "MAIL_USERNAME": self.s.get("mail_user", ""), "MAIL_PASSWORD": self.s.get("mail_password", ""),
               "MAIL_ENCRYPTION": self.s.get("mail_encryption", "tls"),
               "SESSION_SECURE_COOKIE": "true" if self.s["url"].startswith("https://") else "false"}
        previous = self.app / ".env"
        preserved = []
        if previous.exists():
            preserved = [line for line in previous.read_text().splitlines() if line.startswith(("APP_KEY=", "HASHIDS_SALT="))]
            if len(preserved) != 2:
                raise InstallerError("Existing .env is missing its original application keys. Restore a backup.")
        else:
            env["APP_KEY"] = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
            env["HASHIDS_SALT"] = secrets.token_hex(32)
        atomic_write(previous, dotenv(env) + "\n".join(preserved) + ("\n" if preserved else ""), 0o640)
        self.run.run(["chown", "www-data:www-data", previous])

    def source(self, release):
        asset = next((item for item in release["assets"] if item["name"] == "panel.tar.gz"), None)
        if not asset or not str(asset.get("digest", "")).startswith("sha256:"):
            raise InstallerError("The Panel release does not provide a SHA-256 digest.")
        expected_url = f"https://github.com/pterodactyl/panel/releases/download/{release['tag_name']}/panel.tar.gz"
        if asset["browser_download_url"] != expected_url:
            raise InstallerError("Unexpected Panel release download URL.")
        self.app.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with tempfile.TemporaryDirectory(prefix=".ptero-release-", dir=self.app.parent) as directory:
            stage = Path(directory)
            archive = stage / "panel.tar.gz"
            sha, size = hashlib.sha256(), 0
            with self.ui.busy("Downloading and verifying the Panel release"), urllib.request.urlopen(expected_url, timeout=90) as response, archive.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 512 * 1024**2:
                        raise InstallerError("Panel archive exceeds the download size limit.")
                    sha.update(chunk)
                    output.write(chunk)
            if "sha256:" + sha.hexdigest() != asset["digest"]:
                raise InstallerError("Panel archive checksum does not match.")
            fresh = stage / "app"
            fresh.mkdir(mode=0o755)
            secure_extract(archive, fresh, preserve_ownership=False)
            for required in ("artisan", "composer.json", "public/index.php", ".env.example"):
                if not (fresh / required).is_file():
                    raise InstallerError("The Panel archive is incomplete.")
            if self.app.exists():
                for name in (".env", "storage", "extensions", "public/assets/extensions"):
                    original, target = self.app / name, fresh / name
                    if original.is_file():
                        shutil.copy2(original, target)
                    elif original.is_dir():
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(original, target, symlinks=True)
                old = self.app.with_name(".ptero-previous-" + self.i.stamp())
                os.replace(self.app, old)
                try:
                    os.replace(fresh, self.app)
                except BaseException:
                    os.replace(old, self.app)
                    raise
                shutil.rmtree(old)
            else:
                os.replace(fresh, self.app)
        self.s["source_installed"] = release["tag_name"]
        self.save()

    def artisan(self, *args, **kwargs):
        kwargs.setdefault("cwd", str(self.app))
        return self.run.run(["runuser", "-u", "www-data", "--", self.php, self.app / "artisan", *args], **kwargs)

    def setup_runtime(self):
        self.redis_data.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.redis_data.parent.chmod(0o755)
        self.run.run(["chown", "-R", "redis:redis", self.redis_data])
        atomic_write(self.redis_config, f"bind 127.0.0.1\nport 16379\nprotected-mode yes\nrequirepass {self.s['redis_password']}\ndir {self.redis_data}\nappendonly yes\ndaemonize no\nsupervised no\n", 0o640)
        self.redis_config.parent.chmod(0o755)
        self.run.run(["chown", "root:redis", self.redis_config])
        atomic_write(self.units / self.redis_unit,
                     f"[Unit]\nDescription=Pterodactyl Installer: private Redis\nAfter=network.target\n[Service]\nUser=redis\nGroup=redis\nExecStart=/usr/bin/redis-server {self.redis_config}\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n", 0o644)
        atomic_write(self.pool, "[ptero-installer]\nuser=www-data\ngroup=www-data\nlisten=/run/php/ptero-installer.sock\nlisten.owner=www-data\nlisten.group=www-data\npm=ondemand\npm.max_children=12\npm.process_idle_timeout=10s\npm.max_requests=500\ncatch_workers_output=yes\n", 0o644)
        atomic_write(self.units / self.queue,
                     f"[Unit]\nDescription=Pterodactyl Installer: queue worker\nAfter={self.redis_unit} {self.db_unit}.service\nRequires={self.redis_unit}\nStartLimitIntervalSec=180\nStartLimitBurst=30\n[Service]\nUser=www-data\nGroup=www-data\nWorkingDirectory={self.app}\nExecStart={self.php} {self.app}/artisan queue:work --queue=high,standard,low --sleep=3 --tries=3\nRestart=always\nRestartSec=5\nTimeoutStopSec=90\n[Install]\nWantedBy=multi-user.target\n", 0o644)
        atomic_write(self.units / "ptero-panel-scheduler.service",
                     f"[Unit]\nDescription=Pterodactyl Installer: scheduler\nAfter={self.redis_unit}\n[Service]\nType=oneshot\nUser=www-data\nGroup=www-data\nWorkingDirectory={self.app}\nExecStart={self.php} {self.app}/artisan schedule:run\n", 0o644)
        atomic_write(self.units / self.scheduler,
                     "[Unit]\nDescription=Pterodactyl Installer: scheduler timer\n[Timer]\nOnCalendar=*-*-* *:*:00\nPersistent=true\n[Install]\nWantedBy=timers.target\n", 0o644)
        self.run.run(["systemctl", "daemon-reload"])
        self.run.run(["systemctl", "enable", "--now", self.redis_unit, "php" + self.s.get("php", "8.3") + "-fpm"])
        self.run.run(["systemctl", "reload", "php" + self.s.get("php", "8.3") + "-fpm"])

    def nginx_config(self, bootstrap=False):
        host = urllib.parse.urlsplit(self.s["url"]).hostname
        listen = self.s.get("bind", "0.0.0.0") + ":" + str(self.s["http_port"])
        challenge = f"location ^~ /.well-known/acme-challenge/ {{ root {self.acme}; default_type text/plain; }}\n"
        if bootstrap:
            return f"server {{ listen {listen}; server_name {host}; {challenge} location / {{ return 503; }} }}\n"
        headers = ('add_header X-Content-Type-Options nosniff always;\n'
                   'add_header X-Frame-Options DENY always;\n'
                   'add_header Referrer-Policy same-origin always;\n')
        application = (f"root {self.app}/public;\nindex index.php;\nclient_max_body_size 100m;\n{headers}"
                       "location / { try_files $uri $uri/ /index.php?$query_string; }\n"
                       'location ^~ /assets/extensions/ { add_header Content-Security-Policy "sandbox" always; add_header X-Content-Type-Options nosniff always; location ~ /\\. { deny all; } location ~ \\.m?js$ { add_header X-Content-Type-Options nosniff always; try_files $uri =404; } try_files $uri =404; }\n'
                       'location ^~ /assets/ { location ~ /\\. { deny all; } try_files $uri =404; }\n'
                       'location ~ \\.php$ { try_files $uri =404; include fastcgi_params; fastcgi_pass unix:/run/php/ptero-installer.sock; fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name; fastcgi_param HTTP_PROXY ""; fastcgi_read_timeout 300; }\n'
                       'location ~ /\\. { deny all; }\n')
        if not self.s["tls"]:
            return f"server {{ listen {listen}; server_name {host}; {challenge}{application} }}\n"
        cert = self.certdir / "live" / host
        return (f"server {{ listen {listen}; server_name {host}; {challenge} location / {{ return 301 https://$host$request_uri; }} }}\n"
                f"server {{ listen {self.s.get('bind', '0.0.0.0')}:{self.s.get('https_port', 443)} ssl; server_name {host};\n"
                f"ssl_certificate {cert}/fullchain.pem;\nssl_certificate_key {cert}/privkey.pem;\n"
                f"ssl_protocols TLSv1.2 TLSv1.3;\n{application} }}\n")

    def publish_site(self, bootstrap=False):
        atomic_write(self.site, self.nginx_config(bootstrap), 0o644)
        self.enabled.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if not self.enabled.exists():
            self.enabled.symlink_to(self.site)
        self.run.run(["nginx", "-t"], label="Validating NGINX configuration")
        self.run.run(["systemctl", "enable", "--now", "nginx"])
        self.run.run(["systemctl", "reload", "nginx"])

    def certificate(self):
        if not self.s["tls"]:
            return
        for directory in (self.acme, self.acme / ".well-known", self.acme / ".well-known/acme-challenge"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            directory.chmod(0o755)
        host = urllib.parse.urlsplit(self.s["url"]).hostname
        self.publish_site(bootstrap=True)
        self.run.run(["certbot", "certonly", "--webroot", "-w", self.acme, "--config-dir", self.certdir,
                      "--work-dir", self.p.data / "acme-work", "--logs-dir", self.p.data / "acme-logs",
                      "--non-interactive", "--agree-tos", "--email", self.s["email"], "-d", host],
                     label="Issuing the Panel HTTPS certificate", umask=0o022)
        self.renewal_units()

    def renewal_units(self):
        if not self.s["tls"]:
            return
        atomic_write(self.units / "ptero-panel-cert.service",
                     f"[Service]\nType=oneshot\nExecStart=/usr/bin/certbot renew --config-dir {self.certdir} --work-dir {self.p.data}/acme-work --logs-dir {self.p.data}/acme-logs --quiet --deploy-hook \"systemctl reload nginx\"\n", 0o644)
        atomic_write(self.units / self.cert_timer,
                     "[Unit]\nDescription=Renew Pterodactyl Panel certificates\n[Timer]\nOnCalendar=*-*-* 03,15:30:00\nRandomizedDelaySec=1800\nPersistent=true\n[Install]\nWantedBy=timers.target\n", 0o644)
        self.run.run(["systemctl", "daemon-reload"])
        self.run.run(["systemctl", "enable", "--now", self.cert_timer])

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
            sha = hashlib.sha256()
            with target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    sha.update(chunk)
            atomic_write(Path(str(target) + ".sha256"), sha.hexdigest() + "\n")
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

    def wings_tls(self):
        from .core import valid_domain, valid_email
        domain = self.ui.ask("Wings domain / subdomain", validator=valid_domain)
        email = self.ui.ask("Let's Encrypt email", self.s["email"], valid_email)
        self.ui.confirm("ISSUE WINGS SSL")
        for directory in (self.acme, self.acme / ".well-known", self.acme / ".well-known/acme-challenge"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            directory.chmod(0o755)
        site = self.site.with_name("ptero-wings-acme.conf")
        enabled = self.enabled.with_name(site.name)
        host = urllib.parse.urlsplit(self.s["url"]).hostname
        # Same hostname is already served by the Panel's ACME location.
        if domain != host:
            atomic_write(site, f"server {{ listen 80; server_name {domain}; location ^~ /.well-known/acme-challenge/ {{ root {self.acme}; }} location / {{ return 404; }} }}\n", 0o644)
            if not enabled.exists():
                enabled.symlink_to(site)
        self.run.run(["nginx", "-t"])
        self.run.run(["systemctl", "reload", "nginx"])
        atomic_write(self.p.hook, self.i.wings_renew_hook(), 0o700)
        self.run.run(["certbot", "certonly", "--webroot", "-w", self.acme, "--non-interactive", "--agree-tos",
                      "--email", email, "-d", domain], label="Issuing the Wings HTTPS certificate", umask=0o022)
        self.run.run(["systemctl", "enable", "--now", "certbot.timer"])
        self.ui.say("Wings certificate is ready. Synchronize the node configuration after changing SSL settings.", "ok")
