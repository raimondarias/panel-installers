"""Standalone Panel backend. No Docker dependency for the Panel itself.

NativeBackend composes package, web and lifecycle mixins. ``http.client`` and
``time`` stay importable here for callers that patch ``ptero.native.*``.
"""

import base64
import http.client  # noqa: F401  (compatibility: ptero.native.http.client)
import secrets
import tempfile
import time  # noqa: F401  (compatibility: ptero.native.time)

from .common import InstallerError, atomic_write
from .native_lifecycle import NativeLifecycleMixin
from .native_packages import NativePackagesMixin
from .native_web import NativeWebMixin


def dotenv(values):
    def quote(value):
        return (
            '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\n', '\\n').replace('\r', '\\r') + '"'
        )

    return ''.join(f'{key}={quote(value)}\n' for key, value in values.items())


class NativeBackend(NativePackagesMixin, NativeWebMixin, NativeLifecycleMixin):
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

    def sql(self, sql, output=None, application=False):
        with tempfile.TemporaryFile() as stream:
            stream.write(sql.encode())
            stream.seek(0)
            self.run.run(
                [self.db_client, "--batch", "--skip-column-names"],
                input_file=stream,
                output=output,
                label="Provisioning the Panel database",
            )

    def provision_database(self):
        if not self.s.get("database_claimed"):
            _, count = self.run.run(
                [
                    self.db_client,
                    "--batch",
                    "--skip-column-names",
                    "--execute",
                    (
                        "SELECT (SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='ptero_panel') + (SELECT "
                        "COUNT(*) FROM mysql.user WHERE User='ptero_panel');"
                    ),
                ]
            )
            if count.strip() != "0":
                raise InstallerError("The reserved ptero_panel database/user already exists and is not managed by this installation.")
            self.s["database_claimed"] = True
            self.save()
        password = self.s["db_password"]
        if not all(c in "0123456789abcdef" for c in password):
            raise InstallerError("Invalid generated database password.")
        self.sql(
            "CREATE DATABASE IF NOT EXISTS ptero_panel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
            f"CREATE USER IF NOT EXISTS 'ptero_panel'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
            f"ALTER USER 'ptero_panel'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
            "GRANT ALL PRIVILEGES ON ptero_panel.* TO 'ptero_panel'@'127.0.0.1';\n"
        )

    def application_environment(self):
        env = {
            "APP_ENV": "production",
            "APP_DEBUG": "false",
            "APP_URL": self.s["url"],
            "APP_TIMEZONE": self.s["timezone"],
            "APP_ENVIRONMENT_ONLY": "true",
            "APP_SERVICE_AUTHOR": self.s["email"],
            "DB_CONNECTION": "mysql",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "3306",
            "DB_DATABASE": "ptero_panel",
            "DB_USERNAME": "ptero_panel",
            "DB_PASSWORD": self.s["db_password"],
            "CACHE_STORE": "redis",
            "SESSION_DRIVER": "redis",
            "SESSION_CONNECTION": "sessions",
            "QUEUE_CONNECTION": "redis",
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "16379",
            "REDIS_PASSWORD": self.s["redis_password"],
            "MAIL_MAILER": self.s.get("mail_mailer", "log"),
            "MAIL_FROM_ADDRESS": self.s["email"],
            "MAIL_HOST": self.s.get("mail_host", ""),
            "MAIL_PORT": self.s.get("mail_port", "587"),
            "MAIL_USERNAME": self.s.get("mail_user", ""),
            "MAIL_PASSWORD": self.s.get("mail_password", ""),
            "MAIL_ENCRYPTION": self.s.get("mail_encryption", "tls"),
            "SESSION_SECURE_COOKIE": "true" if self.s["url"].startswith("https://") else "false",
        }
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

    def artisan(self, *args, **kwargs):
        kwargs.setdefault("cwd", str(self.app))
        return self.run.run(["runuser", "-u", "www-data", "--", self.php, self.app / "artisan", *args], **kwargs)

    def setup_runtime(self):
        self.redis_data.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.redis_data.parent.chmod(0o755)
        self.run.run(["chown", "-R", "redis:redis", self.redis_data])
        atomic_write(
            self.redis_config,
            (
                f"bind 127.0.0.1\nport 16379\nprotected-mode yes\n"
                f"requirepass {self.s['redis_password']}\ndir {self.redis_data}\n"
                f"appendonly "
                f"yes\ndaemonize no\nsupervised no\n"
            ),
            0o640,
        )
        self.redis_config.parent.chmod(0o755)
        self.run.run(["chown", "root:redis", self.redis_config])
        atomic_write(
            self.units / self.redis_unit,
            (
                f"[Unit]\nDescription=Pterodactyl Installer: private Redis\n"
                f"After=network.target\n[Service]\nUser=redis\n"
                f"Group=redis\nExecStart=/usr/bin/redis-server "
                f"{self.redis_config}\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n"
            ),
            0o644,
        )
        atomic_write(
            self.pool,
            "[ptero-installer]\nuser=www-data\ngroup=www-data\nlisten=/run/php/ptero-installer.sock\nlisten.owner=www-data\nlisten.group=www-data\npm=ondemand\npm.max_children=12\npm.process_idle_timeout=10s\npm.max_requests=500\ncatch_workers_output=yes\n",
            0o644,
        )
        atomic_write(
            self.units / self.queue,
            (
                f"[Unit]\nDescription=Pterodactyl Installer: queue worker\n"
                f"After={self.redis_unit} {self.db_unit}.service\n"
                f"Requires={self.redis_unit}\nStartLimitIntervalSec=180\n"
                f"StartLimitBurst=30\n[Service]\nUser=www-data\n"
                f"Group=www-data\nWorkingDirectory={self.app}\n"
                f"ExecStart={self.php} "
                f"{self.app}/artisan queue:work --queue=high,standard,low --sleep=3 --tries=3\n"
                f"Restart=always\nRestartSec=5\nTimeoutStopSec=90\n"
                f"[Install]\nWantedBy=multi-user.target\n"
            ),
            0o644,
        )
        atomic_write(
            self.units / "ptero-panel-scheduler.service",
            (
                f"[Unit]\nDescription=Pterodactyl Installer: scheduler\n"
                f"After={self.redis_unit}\n[Service]\nType=oneshot\n"
                f"User=www-data\nGroup=www-data\nWorkingDirectory={self.app}\n"
                f"ExecStart={self.php} "
                f"{self.app}/artisan schedule:run\n"
            ),
            0o644,
        )
        atomic_write(
            self.units / self.scheduler,
            (
                "[Unit]\nDescription=Pterodactyl Installer: scheduler timer\n[Timer]\nOnCalendar=*-*-* "
                "*:*:00\nPersistent=true\n[Install]\nWantedBy=timers.target\n"
            ),
            0o644,
        )
        self.run.run(["systemctl", "daemon-reload"])
        self.run.run(["systemctl", "enable", "--now", self.redis_unit, "php" + self.s.get("php", "8.3") + "-fpm"])
        self.run.run(["systemctl", "reload", "php" + self.s.get("php", "8.3") + "-fpm"])
