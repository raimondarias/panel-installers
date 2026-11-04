import contextlib
import hashlib
import io
import json
import os
import pty
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import tempfile
import unittest
import errno
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer as app


def node_payload():
    return {
        "uuid": "3d8dd7f9-07a1-4d65-8db0-8c8921f2f4f7",
        "token_id": "abcdefgh12345678",
        "token": "valid-node-token",
        "api": {"host": "0.0.0.0", "port": 8080, "ssl": {"enabled": False, "cert": "", "key": ""}, "upload_limit": 100},
        "system": {"sftp": {"bind_port": 2022}},
    }


class QuietUI(app.UI):
    def __init__(self, answers=(), yes=()):
        super().__init__(False)
        self.answers = list(answers)
        self.choices = list(yes)
        self.messages = []
        self.confirmations = []

    def say(self, text, kind="info"):
        self.messages.append((text, kind))

    def card(self, title, rows=(), subtitle=None):
        self.messages.append((title, "info"))
        self.messages.extend((row, "info") for row in rows)

    def ask(self, label, default=None, validator=None, secret=False):
        result = self.answers.pop(0) if self.answers else default
        if result is None:
            raise AssertionError(f"Falta respuesta a {label}")
        if validator and not validator(result):
            raise AssertionError(f"Respuesta inválida a {label}: {result}")
        return result

    def yes(self, label, default=False):
        return self.choices.pop(0) if self.choices else default

    def confirm(self, phrase):
        self.confirmations.append(phrase)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.secrets = set()
        self.running = {"database", "cache", "panel"}
        self.failure = lambda args: False
        self.dump = b"-- SQL dump\nCREATE TABLE example(id INT);\n"
        self.restored = None
        self.active = True
        self.import_failure = False

    def redact(self, text):
        for value in self.secrets:
            if value:
                text = text.replace(value, "[REDACTED]")
        return text

    def run(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append((args, kwargs))
        if self.failure(args):
            if kwargs.get("check", True):
                raise app.InstallerError("fallo simulado")
            return 1, ""
        if args[:3] == ["docker", "image", "inspect"]:
            repository = args[-1].rsplit(":", 1)[0]
            return 0, json.dumps([repository + "@sha256:" + "a" * 64])
        if args[:2] == ["docker", "run"] and "--mount" in args:
            mount = args[args.index("--mount") + 1]
            if "target=/ptero-plugins" in mount:
                directory = Path(next(value[7:] for value in mount.split(",") if value.startswith("source=")))
                (directory / "caching_sha2_password.so").write_bytes(b"test authentication plugin")
        if args[:3] == ["systemctl", "is-active", "--quiet"]:
            return (0 if self.active else 3), ""
        if "--services" in args and "--status" in args:
            return 0, "\n".join(sorted(self.running))
        if any(binary in " ".join(args) for binary in ("mariadb-dump", "mysqldump")):
            kwargs["output"].write(self.dump)
        if "input_file" in kwargs:
            self.restored = kwargs["input_file"].read()
            if self.import_failure:
                raise app.InstallerError("importación fallida")
        if "compose" in args and "--file" in args:
            tail = args[args.index("--file") + 2 :]
            if tail and tail[0] == "stop":
                self.running.difference_update(tail[1:])
            elif tail and tail[0] == "down":
                self.running.clear()
            elif tail and tail[0] == "up":
                self.running.update(s for s in ("panel", "database", "cache") if s in tail)
        return 0, ""


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = app.Paths(Path(self.temporary.name))
        self.ui = QuietUI()
        self.runner = FakeRunner()
        self.installer = app.Installer(self.paths, self.ui, self.runner)
        self.state = {
            "schema": 1,
            "owner": app.PROJECT,
            "status": "ready",
            "version": app.PANEL_VERSION,
            "url": "http://panel.example.test:18080",
            "email": "admin@example.test",
            "timezone": "UTC",
            "bind": "127.0.0.1",
            "http_port": 18080,
            "https_port": 443,
            "tls": False,
            "db_password": "b" * 64,
            "admin_created": True,
        }

    def panel(self):
        self.installer.save_panel(self.state)
        for name in ("database", "redis", "var", "nginx", "certs", "logs", "extensions", "extension-assets"):
            (self.paths.data / name).mkdir(parents=True, exist_ok=True)
        (self.paths.data / "var/.env").write_text('APP_KEY="original-key"\nHASHIDS_SALT="original-salt"\n')
        (self.paths.data / "var/.env").chmod(0o640)
        (self.paths.data / "extensions/example.txt").write_text("extension data")
        (self.paths.data / "redis/dump.rdb").write_bytes(b"redis-snapshot")

    def wings(self):
        app.write_json(self.paths.wings_state, {"schema": 1, "owner": app.PROJECT, "version": app.WINGS_VERSION, "status": "ready"})
        app.atomic_write(self.paths.wings, "old binary", 0o755)
        app.atomic_write(self.paths.config, "token: node-secret\n")
        app.atomic_write(self.paths.service, self.installer.service_text(), 0o644)

    def tails(self):
        return [args[args.index("--file") + 2 :] for args, _ in self.runner.calls if "--file" in args]


class ValidationTests(BaseTest):
    def test_versions_accept_only_correct_major(self):
        self.assertTrue(app.valid_version("v2.0.0-beta.2", "panel"))
        self.assertTrue(app.valid_version("v2.1.0", "panel"))
        self.assertTrue(app.valid_version("v1.13.4", "wings"))
        for value in ("latest", "v1.15.1", "v2.0;rm -rf /", "../../v2.0.0", "v2.0.0-evil"):
            self.assertFalse(app.valid_version(value, "panel"))

    def test_urls(self):
        for value in ("https://panel.example.test", "http://127.0.0.1:8080", "https://[::1]"):
            self.assertTrue(app.valid_url(value), value)
        for value in (
            "file:///etc/passwd",
            "https://user:pass@host.test",
            "https://x.test/path",
            "https://x.test?a=b",
            "https://x.test#frag",
            "https://x.test:99999",
            "https://bad host",
        ):
            self.assertFalse(app.valid_url(value), value)

    def test_domains(self):
        self.assertTrue(app.valid_domain("node-1.example.com"))
        for value in ("127.0.0.1", "localhost", "example.com;whoami", "-bad.example.com"):
            self.assertFalse(app.valid_domain(value))

    def test_atomic_write_private_and_refuses_symlink(self):
        target = Path(self.temporary.name) / "private.txt"
        app.atomic_write(target, "secret")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        link = target.with_name("link")
        link.symlink_to(target)
        with self.assertRaises(app.InstallerError):
            app.atomic_write(link, "new")
        self.assertEqual(target.read_text(), "secret")

    def test_owner_marker_required(self):
        app.write_json(self.paths.state, {"schema": 1, "owner": "other"})
        with self.assertRaises(app.InstallerError):
            self.installer.panel_state()

    def test_port_conflict_detected(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            with self.assertRaises(app.InstallerError):
                self.installer.port_free("127.0.0.1", server.getsockname()[1])

    def test_root_and_systemd_required(self):
        with patch.object(app.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(app.InstallerError, "root"):
                self.installer.check_host()

    def test_supported_host_detection(self):
        app.atomic_write(self.paths.root / "etc/os-release", 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n')
        (self.paths.root / "run/systemd/system").mkdir(parents=True)
        with (
            patch.object(app.os, "geteuid", return_value=0),
            patch.object(app.platform, "machine", return_value="x86_64"),
            patch.object(app.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)),
        ):
            self.assertEqual(self.installer.check_host()["ID"], "ubuntu")
            app.atomic_write(self.paths.root / "etc/os-release", 'ID=ubuntu\nVERSION_ID="20.04"\n')
            with self.assertRaises(app.InstallerError):
                self.installer.check_host()


class ComposeTests(BaseTest):
    def test_database_and_redis_not_published_and_no_docker_socket(self):
        config = self.installer.compose_config(self.state)
        for name in ("database", "cache"):
            self.assertNotIn("ports", config["services"][name])
        self.assertNotIn("docker.sock", json.dumps(config))
        env = config["services"]["panel"]["environment"]
        self.assertEqual(env["SCHEDULER_ENABLED"], "true")
        self.assertEqual(env["QUEUE_WORKER_ENABLED"], "true")
        self.assertIn("v2.0.0-beta.2", config["services"]["panel"]["image"])

    def test_interpolation_escapes_smtp_dollars(self):
        self.state["mail_password"] = 'a$pass${HOME}$$"\\secret'
        config = self.installer.compose_config(self.state)
        self.assertEqual(config["services"]["panel"]["environment"]["MAIL_PASSWORD"], 'a$$pass$${HOME}$$$$"\\secret')

    def test_http_does_not_bind_https(self):
        config = self.installer.compose_config(self.state)
        self.assertEqual(config["services"]["panel"]["ports"], ["127.0.0.1:18080:80"])

    def test_real_docker_compose_config_without_daemon(self):
        if not shutil.which("docker"):
            self.skipTest("No está instalado el cliente Docker")
        self.state.update(tls=True, http_port=80, bind="127.0.0.1", mail_password='test$with${literal}')
        self.installer.save_panel(self.state)
        command = ["docker", "compose", "-f", str(self.paths.compose), "config", "--format", "json"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        # config vuelve a escapar dólares para producir un Compose reutilizable.
        self.assertEqual(config["services"]["panel"]["environment"]["MAIL_PASSWORD"], 'test$$with$${literal}')
        self.assertNotIn('variable is not set', result.stderr)

    def test_pin_image_requires_digest(self):
        digest = self.installer.pin_image("ghcr.io/pterodactyl/panel:v2.0.0-beta.2")
        self.assertEqual(digest, "ghcr.io/pterodactyl/panel@sha256:" + "a" * 64)


class ReleaseTests(BaseTest):
    def test_auto_never_selects_panel_v1(self):
        releases = [
            {"tag_name": "v1.99.0", "prerelease": False, "draft": False},
            {"tag_name": "v2.0.0-beta.2", "prerelease": True, "draft": False},
            {"tag_name": "v2.0.0-beta.10", "prerelease": True, "draft": False},
        ]
        with patch.object(self.installer, "fetch", return_value=json.dumps(releases).encode()):
            self.assertEqual(self.installer.release("panel", "auto")["tag_name"], "v2.0.0-beta.10")

    def test_stable_beats_beta_with_same_version(self):
        releases = [{"tag_name": tag, "draft": False, "prerelease": "-" in tag} for tag in ("v2.0.0-beta.99", "v2.0.0-rc.1", "v2.0.0")]
        with patch.object(self.installer, "fetch", return_value=json.dumps(releases).encode()):
            self.assertEqual(self.installer.release("panel", "auto")["tag_name"], "v2.0.0")

    def test_wings_auto_excludes_prereleases(self):
        releases = [{"tag_name": tag, "draft": False, "prerelease": "-" in tag} for tag in ("v1.14.0-beta.1", "v1.13.4")]
        with patch.object(self.installer, "fetch", return_value=json.dumps(releases).encode()):
            self.assertEqual(self.installer.release("wings", "auto")["tag_name"], "v1.13.4")

    def test_beta_requires_acceptance(self):
        self.ui.choices = [False]
        with patch.object(self.installer, "release", return_value={"tag_name": app.PANEL_VERSION, "prerelease": True}):
            with self.assertRaises(app.Cancelled):
                self.installer.choose_release("panel", app.PANEL_VERSION)

    def test_wings_sha256_verified_before_execution(self):
        blob = b"test binary"
        asset = {
            "name": "wings_linux_amd64",
            "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
            "browser_download_url": "https://github.com/pterodactyl/wings/releases/download/v1.13.4/wings_linux_amd64",
        }
        release = {"tag_name": "v1.13.4", "assets": [asset]}
        target = self.paths.wings
        with (
            patch.object(app.platform, "machine", return_value="x86_64"),
            patch.object(app.urllib.request, "urlopen", return_value=io.BytesIO(blob)),
        ):
            self.installer.download_wings(release, target)
        self.assertEqual(target.read_bytes(), blob)
        self.assertTrue(any(args == [str(target), "version"] for args, _ in self.runner.calls))

    def test_corrupt_wings_is_deleted_and_not_executed(self):
        asset = {
            "name": "wings_linux_amd64",
            "digest": "sha256:" + "0" * 64,
            "browser_download_url": "https://github.com/pterodactyl/wings/releases/download/v1.13.4/wings_linux_amd64",
        }
        with (
            patch.object(app.platform, "machine", return_value="x86_64"),
            patch.object(app.urllib.request, "urlopen", return_value=io.BytesIO(b"corrupt")),
        ):
            with self.assertRaises(app.InstallerError):
                self.installer.download_wings({"tag_name": "v1.13.4", "assets": [asset]}, self.paths.wings)
        self.assertFalse(self.paths.wings.exists())
        self.assertEqual(self.runner.calls, [])

    def test_missing_digest_is_refused(self):
        with self.assertRaises(app.InstallerError):
            self.installer.download_wings({"tag_name": "v1.13.4", "assets": []}, self.paths.wings)


class InstallTests(BaseTest):
    def test_panel_migrations_before_first_web_start(self):
        self.state.update(status="installing", admin_created=False)
        with (
            patch.object(self.installer, "check_host"),
            patch.object(self.installer, "docker"),
            patch.object(self.installer, "new_panel", return_value=self.state),
        ):
            self.installer.install_panel()
        tails = self.tails()
        migration = next(i for i, args in enumerate(tails) if "migrate" in args)
        start = next(i for i, args in enumerate(tails) if args[0] == "up" and args[-1] == "panel")
        admin = next(i for i, args in enumerate(tails) if "p:user:make" in args)
        self.assertLess(migration, admin)
        self.assertLess(admin, start)
        self.assertEqual(self.installer.panel_state()["status"], "ready")
        self.assertEqual((self.paths.data / "var").stat().st_mode & 0o777, 0o755)

    def test_migration_failure_keeps_state_resumable_without_serving_panel(self):
        self.state.update(status="installing", admin_created=False)
        self.runner.failure = lambda args: "migrate" in args
        with (
            patch.object(self.installer, "check_host"),
            patch.object(self.installer, "docker"),
            patch.object(self.installer, "new_panel", return_value=self.state),
        ):
            with self.assertRaises(app.InstallerError):
                self.installer.install_panel()
        self.assertEqual(self.installer.panel_state()["status"], "installing")
        self.assertFalse(any(args[0] == "up" and args[-1] == "panel" for args in self.tails()))

    def test_reinstall_ready_panel_is_noop(self):
        self.panel()
        with patch.object(self.installer, "check_host"):
            self.installer.install_panel()
        self.assertEqual(self.runner.calls, [])

    def test_pending_sql_restore_cannot_be_resumed_by_install(self):
        self.state["status"] = "restoring"
        self.panel()
        with patch.object(self.installer, "check_host"), self.assertRaises(app.InstallerError):
            self.installer.install_panel()

    def test_unmanaged_wings_is_never_overwritten(self):
        app.atomic_write(self.paths.config, "existing installation")
        with patch.object(self.installer, "check_host"), self.assertRaises(app.InstallerError):
            self.installer.install_wings()
        self.assertEqual(self.paths.config.read_text(), "existing installation")

    def test_wings_install_without_node_remains_pending(self):
        self.ui.choices = [False]

        def download(release, target):
            app.atomic_write(target, "verified binary", 0o755)

        with (
            patch.object(self.installer, "check_host"),
            patch.object(self.installer, "docker"),
            patch.object(self.installer, "choose_release", return_value={"tag_name": app.WINGS_VERSION}),
            patch.object(self.installer, "download_wings", side_effect=download),
        ):
            self.installer.install_wings()
        self.assertEqual(self.installer.load(self.paths.wings_state)["status"], "pending")
        self.assertFalse(any("enable" in args and "wings" in args for args, _ in self.runner.calls))


class BackupTests(BaseTest):
    def test_backup_restore_roundtrip_preserves_keys_sql_redis_extensions_and_links(self):
        self.panel()
        certs = self.paths.data / "certs"
        (certs / "archive/domain").mkdir(parents=True)
        (certs / "live/domain").mkdir(parents=True)
        (certs / "archive/domain/cert1.pem").write_text("certificate")
        (certs / "live/domain/cert.pem").symlink_to("../../archive/domain/cert1.pem")
        os.link(self.paths.data / "extensions/example.txt", self.paths.data / "extensions/hardlinked.txt")
        if os.geteuid() == 0:
            os.chown(self.paths.data / "var/.env", 101, 101)
        archive = self.installer.backup_panel()
        self.assertEqual(self.runner.running, {"panel", "database", "cache"})
        shutil.rmtree(self.paths.panel)
        self.ui.answers = [str(archive)]
        with patch.object(self.installer, "docker"):
            self.installer.restore_panel()
        self.assertIn("original-key", (self.paths.data / "var/.env").read_text())
        self.assertEqual((self.paths.data / "var/.env").stat().st_mode & 0o777, 0o640)
        if os.geteuid() == 0:
            self.assertEqual((self.paths.data / "var/.env").stat().st_uid, 101)
        self.assertEqual((self.paths.data / "extensions/example.txt").read_text(), "extension data")
        self.assertEqual((self.paths.data / "extensions/hardlinked.txt").read_text(), "extension data")
        self.assertEqual((self.paths.data / "redis/dump.rdb").read_bytes(), b"redis-snapshot")
        self.assertTrue((certs / "live/domain/cert.pem").is_symlink())
        self.assertEqual((certs / "live/domain/cert.pem").read_text(), "certificate")
        self.assertEqual(self.runner.restored, self.runner.dump)
        self.assertEqual(self.installer.panel_state()["status"], "ready")

    def test_dump_failure_restores_services_and_does_not_publish_backup(self):
        self.panel()
        self.runner.failure = lambda args: "mariadb-dump" in " ".join(args)
        with self.assertRaises(app.InstallerError):
            self.installer.backup_panel()
        self.assertEqual(self.runner.running, {"panel", "cache", "database"})
        self.assertEqual(list(self.paths.backups.glob("*.tar.gz")), [])

    def test_empty_dump_is_rejected(self):
        self.panel()
        self.runner.dump = b""
        with self.assertRaisesRegex(app.InstallerError, "empty"):
            self.installer.backup_panel()

    def test_backup_preserves_previously_stopped_services(self):
        self.panel()
        self.runner.running.clear()
        self.installer.backup_panel()
        self.assertEqual(self.runner.running, set())

    def test_restore_checksum_failure_does_not_touch_install(self):
        self.panel()
        archive = self.installer.backup_panel()
        archive.write_bytes(b"corrupt")
        self.ui.answers = [str(archive)]
        with self.assertRaisesRegex(app.InstallerError, "checksum"):
            self.installer.restore_panel()
        self.assertEqual(self.installer.panel_state()["status"], "ready")

    def test_restore_refuses_nonempty_destination(self):
        self.panel()
        archive = self.installer.backup_panel()
        self.ui.answers = [str(archive)]
        with self.assertRaisesRegex(app.InstallerError, "empty"):
            self.installer.restore_panel()

    def test_failed_import_never_starts_panel(self):
        self.panel()
        archive = self.installer.backup_panel()
        shutil.rmtree(self.paths.panel)
        self.runner.calls.clear()
        self.runner.import_failure = True
        self.ui.answers = [str(archive)]
        with patch.object(self.installer, "docker"), self.assertRaises(app.InstallerError):
            self.installer.restore_panel()
        self.assertFalse(any(t[0] == "up" and t[-1] == "panel" for t in self.tails()))
        self.assertEqual(self.installer.panel_state()["status"], "restoring")

    def test_backup_permissions_private(self):
        self.panel()
        backup = self.installer.backup_panel()
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(str(backup) + ".sha256").stat().st_mode & 0o777, 0o600)


class ArchiveTests(BaseTest):
    def archive(self, members):
        path = Path(self.temporary.name) / "test.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for member, data in members:
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
        return path

    def test_path_traversal_rejected(self):
        for name in ("../../outside", "/tmp/absolute", "safe/../../bad"):
            member = tarfile.TarInfo(name)
            member.size = 1
            with self.assertRaises(app.InstallerError):
                app.secure_extract(self.archive([(member, b"x")]), Path(self.temporary.name) / "out")

    def test_external_symlink_rejected(self):
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../etc/passwd"
        with self.assertRaises(app.InstallerError):
            app.secure_extract(self.archive([(member, None)]), Path(self.temporary.name) / "out")

    def test_file_under_archive_symlink_rejected(self):
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "inner"
        file = tarfile.TarInfo("link/payload")
        file.size = 1
        with self.assertRaises(app.InstallerError):
            app.secure_extract(self.archive([(link, None), (file, b"x")]), Path(self.temporary.name) / "out")

    def test_device_rejected(self):
        member = tarfile.TarInfo("device")
        member.type = tarfile.CHRTYPE
        with self.assertRaises(app.InstallerError):
            app.secure_extract(self.archive([(member, None)]), Path(self.temporary.name) / "out")


class MaintenanceTests(BaseTest):
    def test_downgrade_refused_before_backup_or_migration(self):
        self.panel()
        with patch.object(self.installer, "choose_release", return_value={"tag_name": "v2.0.0-beta.1"}):
            with self.assertRaisesRegex(app.InstallerError, "downgrades"):
                self.installer.update_panel()
        self.assertEqual(self.runner.calls, [])

    def test_diagnostics_reports_missing_containers_and_keeps_machine_overview(self):
        self.panel()
        self.installer.diagnostics()
        messages = " ".join(str(message) for message, _ in self.ui.messages)
        self.assertIn("Panel containers are missing", messages)
        self.assertIn("Machine overview", messages)
        self.assertNotIn("All Panel containers are healthy", messages)

    def test_update_failure_stays_stopped_with_recovery_backup(self):
        self.panel()
        self.runner.failure = lambda args: "migrate" in args
        self.ui.choices = [False]  # Decline the offered rollback.
        with patch.object(self.installer, "choose_release", return_value={"tag_name": "v2.0.0-beta.3"}):
            with self.assertRaises(app.InstallerError):
                self.installer.update_panel()
        state = self.installer.panel_state()
        self.assertEqual(state["status"], "updating")
        self.assertTrue(Path(state["recovery_backup"]).is_file())
        self.assertNotIn("panel", self.runner.running)

    def test_wings_update_rolls_back_binary_if_start_fails(self):
        self.wings()

        def download(release, path):
            app.atomic_write(path, "new binary", 0o755)

        with (
            patch.object(self.installer, "choose_release", return_value={"tag_name": "v1.13.5"}),
            patch.object(self.installer, "download_wings", side_effect=download),
            patch.object(self.installer, "start_wings", side_effect=app.InstallerError("bad binary")),
        ):
            with self.assertRaises(app.InstallerError):
                self.installer.update_wings()
        self.assertEqual(self.paths.wings.read_text(), "old binary")
        self.assertEqual(self.installer.load(self.paths.wings_state)["version"], app.WINGS_VERSION)

    def test_uninstall_keeps_data_by_default_and_never_prunes_docker(self):
        self.panel()
        self.installer.uninstall_panel()
        self.assertTrue((self.paths.data / "var/.env").exists())
        self.assertEqual(self.installer.panel_state()["status"], "stopped")
        self.assertFalse(any("prune" in args for args, _ in self.runner.calls))
        self.assertFalse(any("--volumes" in args for args, _ in self.runner.calls))

    def test_panel_purge_requires_backup_first(self):
        self.panel()
        self.ui.choices = [True]
        self.installer.uninstall_panel()
        self.assertFalse(self.paths.panel.exists())
        self.assertEqual(len(list(self.paths.backups.glob("*.tar.gz"))), 1)
        self.assertIn("DELETE PANEL", self.ui.confirmations)

    def test_uninstall_aborts_if_backup_fails(self):
        self.panel()
        self.ui.choices = [True]
        self.runner.dump = b""
        with self.assertRaises(app.InstallerError):
            self.installer.uninstall_panel()
        self.assertTrue(self.paths.panel.exists())
        self.assertFalse(any(t[0] == "down" for t in self.tails()))

    def test_uninstall_wings_preserves_game_files(self):
        self.wings()
        games = self.paths.root / "var/lib/pterodactyl/volumes/game/world.dat"
        app.atomic_write(games, "game data")
        self.installer.uninstall_wings()
        self.assertEqual(games.read_text(), "game data")
        self.assertFalse(self.paths.wings.exists())
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(any(args[:2] == ["docker", "rm"] for args, _ in self.runner.calls))

    def test_ssl_failure_brings_panel_back(self):
        self.panel()
        self.wings()
        self.state["http_port"] = 80
        self.installer.save_panel(self.state)
        self.ui.answers = ["node.example.test", "admin@example.test"]
        self.runner.failure = lambda args: args[0] == "certbot"
        with patch.object(self.installer, "port_free"), self.assertRaises(app.InstallerError):
            self.installer.wings_tls()
        self.assertIn("panel", self.runner.running)

    def test_configure_cancel_does_not_replace_previous_config(self):
        self.wings()
        self.ui.answers = ["https://panel.example.test", "1", "secret-auto-deploy"]
        before = self.paths.config.read_bytes()
        with (
            patch.object(self.installer, "docker"),
            patch.object(app.urllib.request.OpenerDirector, "open", side_effect=app.urllib.error.URLError("denied")),
            self.assertRaises(app.InstallerError),
        ):
            self.installer.configure_wings()
        self.assertEqual(self.paths.config.read_bytes(), before)
        self.assertFalse((self.paths.config_dir / ".config-installer.yml").exists())

    def test_configure_token_is_not_in_command_arguments_or_state(self):
        self.wings()
        self.ui.answers = ["https://panel.example.test", "1", "secret-auto-deploy"]
        self.ui.choices = [False]
        with (
            patch.object(self.installer, "docker"),
            patch.object(self.installer, "start_wings"),
            patch.object(
                app.urllib.request.OpenerDirector, "open", return_value=io.BytesIO(json.dumps(node_payload()).encode())
            ) as request,
        ):
            self.installer.configure_wings()
        self.assertNotIn("secret-auto-deploy", repr(self.runner.calls))
        self.assertNotIn("secret-auto-deploy", self.paths.wings_state.read_text())
        self.assertIn("valid-node-token", self.paths.config.read_text())
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o600)
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer secret-auto-deploy")
        self.assertEqual(sent.full_url, "https://panel.example.test/api/application/nodes/1/configuration")

    def test_node_config_rejects_token_indirection_and_discards_host_settings(self):
        payload = node_payload()
        payload.update(allowed_mounts=["/etc"], ignore_panel_config_updates=True)
        payload["system"]["data"] = "/etc"
        config = app.node_configuration(payload, "https://panel.example.test")
        self.assertNotIn("allowed_mounts", config)
        self.assertNotIn("data", config["system"])
        for token in ("file:///etc/passwd", "$HOME", ""):
            payload["token"] = token
            with self.assertRaises(app.InstallerError):
                app.node_configuration(payload, "https://panel.example.test")


class InteractionTests(BaseTest):
    def test_menu_lists_requested_options_and_exits_without_commands(self):
        real_ui = app.UI(False)
        self.installer.ui = real_ui
        stream = io.StringIO()
        with patch("builtins.input", side_effect=["invalid", "0"]), contextlib.redirect_stdout(stream):
            self.installer.menu()
        text = stream.getvalue()
        for expected in (
            "Install Panel and Wings",
            "Install Panel",
            "Install Wings",
            "Uninstall Panel and Wings",
            "Uninstall Panel",
            "Uninstall Wings",
        ):
            self.assertIn(expected, text)
        self.assertEqual(self.runner.calls, [])

    def test_confirmation_refused_by_default(self):
        with patch("builtins.input", return_value=""), self.assertRaises(app.Cancelled):
            app.UI(False).confirm("DELETE PANEL")

    def test_animation_in_real_pseudoterminal(self):
        master, slave = pty.openpty()
        try:
            env = {**os.environ, "TERM": "xterm"}
            env.pop("NO_COLOR", None)
            code = "import time; from installer import UI; ui=UI();\nwith ui.busy('prueba animada'): time.sleep(0.3)\n"
            result = subprocess.run(
                [sys.executable, "-c", code], stdout=slave, stderr=slave, env=env, cwd=Path(app.__file__).parent, timeout=5
            )
            os.close(slave)
            slave = None
            output = b""
            while True:
                try:
                    part = os.read(master, 4096)
                    if not part:
                        break
                    output += part
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(b"prueba animada", output)
            self.assertIn(b"\x1b[36m", output)
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)


class RunnerTests(BaseTest):
    def test_wings_secrets_redacted_after_reopening_installer(self):
        self.wings()
        self.installer.load(self.paths.wings_state)
        self.assertIn("node-secret", self.runner.secrets)
        real_runner = app.Runner(self.ui, self.paths.log)
        self.assertNotIn("secret-value", real_runner.redact("Authorization: Bearer secret-value"))

    def test_failure_propagates_and_secret_is_redacted(self):
        runner = app.Runner(self.ui, self.paths.log)
        runner.secrets.add("sensitive-value")
        with self.assertRaises(app.InstallerError) as raised:
            runner.run([sys.executable, "-c", "import sys; print('sensitive-value', file=sys.stderr); sys.exit(7)"])
        self.assertNotIn("sensitive-value", str(raised.exception))
        self.assertNotIn("sensitive-value", self.paths.log.read_text())
        self.assertIn("exit code 7", str(raised.exception))

    def test_command_arguments_are_not_shell_executed(self):
        runner = app.Runner(self.ui, self.paths.log)
        argument = '$(touch /tmp/SHOULD_NOT_EXIST); `whoami` $HOME'
        code, text = runner.run([sys.executable, "-c", "import sys; print(sys.argv[1])", argument])
        self.assertEqual(code, 0)
        self.assertEqual(text.strip(), argument)

    def test_dump_stream_is_not_logged(self):
        runner = app.Runner(self.ui, self.paths.log)
        with tempfile.TemporaryFile() as stream:
            runner.run([sys.executable, "-c", "import sys; sys.stdout.write('PRIVATE_' + 'SQL')"], output=stream)
            stream.seek(0)
            self.assertEqual(stream.read(), b"PRIVATE_SQL")
        self.assertNotIn("PRIVATE_SQL", self.paths.log.read_text())

    def test_closed_stdin_cannot_confirm(self):
        with patch("builtins.input", side_effect=EOFError), self.assertRaises(app.Cancelled):
            app.UI(False).confirm("DELETE PANEL")


if __name__ == "__main__":
    unittest.main()
