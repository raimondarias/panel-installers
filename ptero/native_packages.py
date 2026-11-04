"""Standalone Panel: APT repositories, PHP/database packages, Composer and verified Panel source."""
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile

from .common import InstallerError, atomic_write, download, secure_extract


class NativePackagesMixin:
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
            with self.ui.busy("Downloading and verifying the Panel release"):
                value = download(expected_url, archive, 512 * 1024**2)
            if "sha256:" + value != asset["digest"]:
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
