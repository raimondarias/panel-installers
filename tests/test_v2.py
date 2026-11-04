"""Regression coverage for localization, guided choices and both deployment backends."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import string
import subprocess
from unittest.mock import patch

from test_installer import BaseTest, QuietUI
import installer as app
from ptero import __version__
from ptero.cli import select_language
from ptero.i18n import Translator
from ptero.native import NativeBackend, dotenv
from ptero.wings import DockerWings
from ptero.wizard import panel_plan, valid_public_ipv4


class PlannedUI(QuietUI):
    def __init__(self, method, database, access, advanced=False):
        super().__init__()
        self.options = [advanced, method, database, access] + (["8.4"] if advanced and method == "standalone" else []) + ["later"]
        self.prompts = []
        self.choices = [True, True, True, False] if access == "domain" else [True, False]

    def choice(self, title, options, default=0):
        if title == "Panel deployment":
            assert options[0][0] == "standalone"
            assert options[0][1] == "Standalone (Recommended)"
        value = self.options.pop(0)
        assert value in [item[0] for item in options], (value, title)
        return value

    def ask(self, label, default=None, validator=None, secret=False):
        values = {
            "Domain / subdomain (without http://)": "panel.example.test",
            "Administrator email": "admin@example.test",
            "Administrator username": "admin",
            "Administrator password (12+ characters; empty generates one)": "A-strong-password-123!",
            "Confirm administrator password": "A-strong-password-123!",
        }
        value = values.get(label, default)
        self.prompts.append((label, secret))
        if validator and not validator(value):
            raise AssertionError((label, value))
        return value


class LocalizationTests(BaseTest):
    def test_all_catalogue_entries_have_three_languages_and_matching_fields(self):
        tr = Translator()

        def fields(text):
            return {field for _, field, _, _ in string.Formatter().parse(text) if field is not None}

        for source, values in tr.catalogue.items():
            for language in ("en", "es", "pt-BR"):
                self.assertTrue(values[language], source)
                self.assertEqual(fields(source), fields(values[language]), (source, language))

    def test_english_is_default_and_language_selector_has_three_distinct_numbers(self):
        ui = app.UI(False)
        stream = io.StringIO()
        with patch("builtins.input", return_value=""), contextlib.redirect_stdout(stream):
            language = select_language(ui)
        self.assertEqual(language, "en")
        for expected in ("1. English", "2. Español", "3. Português (Brasil)"):
            self.assertIn(expected, stream.getvalue())

    def test_each_language_localizes_recommendations_and_confirmations(self):
        for language, recommended, confirm in (
            ("en", "Recommended", "INSTALL PANEL"),
            ("es", "Recomendado", "INSTALAR PANEL"),
            ("pt-BR", "Recomendado", "INSTALAR PAINEL"),
        ):
            ui = app.UI(False, language)
            self.assertIn(recommended, ui.tr("Standalone (Recommended)"))
            with patch("builtins.input", return_value=confirm):
                ui.confirm("INSTALL PANEL")
            self.assertNotIn("{", ui.tr("Port 0.0.0.0:80 is occupied or unavailable."))

    def test_brazilian_portuguese_menu(self):
        self.installer.ui = app.UI(False, "pt-BR")
        stream = io.StringIO()
        with patch("builtins.input", return_value="0"), contextlib.redirect_stdout(stream):
            self.installer.menu()
        self.assertIn("Instalar Painel e Wings", stream.getvalue())
        self.assertIn("Alterar idioma", stream.getvalue())
        self.assertIn("Sair", stream.getvalue())

    def test_portuguese_yes_and_no(self):
        ui = app.UI(False, "pt-BR")
        with patch("builtins.input", side_effect=["sim", "não"]):
            self.assertTrue(ui.yes("Continue?"))
            self.assertFalse(ui.yes("Continue?"))


class WizardTests(BaseTest):
    def test_simple_plan_matrix_for_methods_engines_access_and_languages(self):
        for method in ("docker", "standalone"):
            for database in ("mysql", "mariadb"):
                for access in ("ip", "domain"):
                    for language in ("en", "es", "pt-BR"):
                        with self.subTest(method=method, database=database, access=access, language=language):
                            ui = PlannedUI(method, database, access)
                            ui.set_language(language)
                            self.installer.ui = ui
                            with (
                                patch.object(self.installer, "release", return_value={"tag_name": app.PANEL_VERSION, "prerelease": True}),
                                patch.object(self.installer, "fetch", return_value=b"8.8.8.8"),
                                patch.object(self.installer, "check_plan"),
                            ):
                                state = panel_plan(self.installer)
                            self.assertEqual(state["method"], method)
                            self.assertEqual(state["database_engine"], database)
                            self.assertEqual(state["tls"], access == "domain")
                            self.assertEqual(state["language"], language)
                            self.assertTrue(state["url"].startswith("http://8.8.8.8" if access == "ip" else "https://panel.example.test"))
                            self.assertNotIn("A-strong-password-123!", json.dumps(state))
                            self.assertIn(("Administrator password (12+ characters; empty generates one)", True), ui.prompts)

    def test_advanced_standalone_exposes_php_network_and_timezone(self):
        self.installer.ui = PlannedUI("standalone", "mysql", "ip", advanced=True)
        with (
            patch.object(self.installer, "release", return_value={"tag_name": app.PANEL_VERSION, "prerelease": True}),
            patch.object(self.installer, "fetch", return_value=b"8.8.8.8"),
            patch.object(self.installer, "check_plan"),
        ):
            state = panel_plan(self.installer)
        self.assertEqual(state["php"], "8.4")
        self.assertIn(("Listen IPv4 address", False), self.installer.ui.prompts)
        self.assertIn(("Timezone", False), self.installer.ui.prompts)

    def test_private_ips_are_never_reported_as_public(self):
        for value in ("127.0.0.1", "10.0.0.2", "192.168.1.1", "169.254.1.2", "0.0.0.0", "::1", "garbage"):
            self.assertFalse(valid_public_ipv4(value), value)
        self.assertTrue(valid_public_ipv4("8.8.8.8"))

    def test_standalone_dispatch_does_not_install_docker(self):
        self.state.update(method="standalone", status="installing")
        self.installer.save_panel(self.state)
        with (
            patch.object(self.installer, "check_host"),
            patch.object(NativeBackend, "install") as native,
            patch.object(self.installer, "docker") as docker,
        ):
            self.installer.install_panel()
        native.assert_called_once()
        docker.assert_not_called()

    def test_mysql_docker_install_pulls_mysql_and_uses_mysql_dump(self):
        self.state.update(method="docker", database_engine="mysql", status="installing")
        with (
            patch.object(self.installer, "check_host"),
            patch.object(self.installer, "docker"),
            patch.object(self.installer, "new_panel", return_value=self.state),
        ):
            self.installer.install_panel()
        pulls = [args[-1] for args, _ in self.runner.calls if args[:2] == ["docker", "pull"]]
        self.assertIn("mysql:8.4", pulls)
        self.assertNotIn("mariadb:11.4", pulls)
        config = self.installer.compose_config(self.state)
        db = config["services"]["database"]
        self.assertIn("MYSQL_PASSWORD", db["environment"])
        self.assertNotIn("MARIADB_PASSWORD", db["environment"])
        command = self.installer.database_command(self.state, dump=True)
        self.assertIn("mysqldump", command)
        self.assertIn("--no-tablespaces", command)
        self.assertNotIn(self.state["db_password"], command)

    def test_mysql_healthcheck_with_real_compose_keeps_runtime_variables(self):
        if not shutil.which("docker"):
            self.skipTest("Docker CLI unavailable")
        self.state["database_engine"] = "mysql"
        self.installer.save_panel(self.state)
        result = subprocess.run(
            ["docker", "compose", "-f", str(self.paths.compose), "config", "--format", "json"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("variable is not set", result.stderr)
        check = json.loads(result.stdout)["services"]["database"]["healthcheck"]["test"]
        self.assertIn("MYSQL_PASSWORD", check[1])
        self.assertIn("SELECT 1", check[1])

    def test_admin_credentials_are_supplied_via_stdin_not_argv(self):
        self.state["admin"] = {
            "email": "admin@example.test",
            "username": "admin",
            "name_first": "Test",
            "name_last": "Admin",
            "root_admin": True,
        }
        self.installer.admin_password = "A-strong-password-123!"
        self.installer.create_admin(self.state)
        args, _ = self.runner.calls[-1]
        self.assertNotIn("A-strong-password-123!", repr(args))
        self.assertEqual(json.loads(self.runner.restored)["password"], "A-strong-password-123!")
        self.assertTrue(json.loads(self.runner.restored)["root_admin"])


class NativeTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.state.update(method="standalone", database_engine="mariadb", php="8.3", redis_password="c" * 64)
        self.installer.save_panel(self.state)
        self.backend = NativeBackend(self.installer, self.state)
        self.backend.app.mkdir(parents=True)
        (self.backend.app / "artisan").write_text("<?php // fixture")
        (self.backend.app / ".env").write_text('APP_KEY="original-key"\nHASHIDS_SALT="original-salt"\n')
        (self.backend.app / "storage").mkdir()
        (self.backend.app / "storage/test.txt").write_text("preserve storage")
        self.backend.redis_data.mkdir(parents=True)
        (self.backend.redis_data / "dump.rdb").write_text("private redis")

    def test_environment_preserves_both_original_keys_and_private_redis(self):
        self.backend.application_environment()
        self.backend.application_environment()
        env = (self.backend.app / ".env").read_text()
        self.assertEqual(env.count('APP_KEY="original-key"'), 1)
        self.assertEqual(env.count('HASHIDS_SALT="original-salt"'), 1)
        self.assertIn('REDIS_PORT="16379"', env)
        self.assertIn('DB_DATABASE="ptero_panel"', env)
        self.assertEqual((self.backend.app / ".env").stat().st_mode & 0o777, 0o640)

    def test_dotenv_escapes_shell_and_dotenv_characters(self):
        text = dotenv({"MAIL_PASSWORD": 'a$HOME"\\secret'})
        self.assertIn(r'\$HOME\"\\secret', text)

    def test_runtime_uses_dedicated_units_pool_and_redis(self):
        self.backend.setup_runtime()
        self.assertIn("port 16379", self.backend.redis_config.read_text())
        self.assertIn(self.state["redis_password"], self.backend.redis_config.read_text())
        self.assertIn("ptero-installer.sock", self.backend.pool.read_text())
        self.assertIn("OnCalendar=*-*-* *:*:00", (self.backend.units / self.backend.scheduler).read_text())
        self.assertFalse(any("crontab" in args for args, _ in self.runner.calls))
        self.assertFalse(self.paths.compose.exists())

    def test_nginx_configuration_contains_static_asset_protections_and_php_pool(self):
        config = self.backend.nginx_config()
        self.assertIn("location ^~ /assets/extensions/", config)
        self.assertIn('Content-Security-Policy "sandbox"', config)
        self.assertIn("location ~ /\\.", config)
        self.assertIn("/run/php/ptero-installer.sock", config)
        self.assertNotIn("ssl_certificate", config)
        self.assertIn("return 503", self.backend.nginx_config(bootstrap=True))

    def test_tls_config_uses_its_own_certbot_state(self):
        self.state.update(tls=True, url="https://panel.example.test", http_port=80)
        config = self.backend.nginx_config()
        self.assertIn(str(self.backend.certdir / "live/panel.example.test/fullchain.pem"), config)
        self.assertIn("return 301 https://$host$request_uri", config)

    def test_native_backup_restore_roundtrip_without_docker(self):
        archive = self.backend.backup()
        shutil.rmtree(self.paths.panel)
        shutil.rmtree(self.backend.app)
        shutil.rmtree(self.backend.redis_data)
        self.ui.answers = [str(archive)]
        with (
            patch.object(NativeBackend, "dependencies"),
            patch.object(NativeBackend, "provision_database"),
            patch.object(NativeBackend, "health"),
        ):
            self.installer.restore_panel()
        self.assertIn("original-key", (self.backend.app / ".env").read_text())
        self.assertEqual((self.backend.app / "storage/test.txt").read_text(), "preserve storage")
        self.assertEqual((self.backend.redis_data / "dump.rdb").read_text(), "private redis")
        self.assertFalse(any("docker" in args for args, _ in self.runner.calls))
        self.assertEqual(self.installer.panel_state()["status"], "ready")

    def test_native_backup_rejects_empty_dump_and_recovers_units(self):
        self.runner.dump = b""
        with self.assertRaises(app.InstallerError):
            self.backend.backup()
        starts = [args for args, _ in self.runner.calls if args[:2] == ["systemctl", "start"]]
        self.assertTrue(any(self.backend.redis_unit in args for args in starts))
        self.assertEqual(list(self.paths.backups.glob("*.tar.gz")), [])

    def test_native_migrations_failure_does_not_start_workers_or_publish_panel(self):
        self.state.update(status="installing", source_installed=app.PANEL_VERSION)
        self.runner.failure = lambda args: "migrate" in args
        with (
            patch.object(self.backend, "dependencies"),
            patch.object(self.backend, "provision_database"),
            patch.object(self.backend, "setup_runtime"),
            patch.object(self.backend, "start") as start,
            patch.object(self.backend, "publish_site") as publish,
        ):
            with self.assertRaises(app.InstallerError):
                self.backend.install()
        start.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(self.state["status"], "installing")

    def test_uninstall_native_keeps_data_by_default_and_shared_services(self):
        self.backend.uninstall()
        self.assertTrue((self.backend.app / ".env").exists())
        self.assertEqual(self.installer.panel_state()["status"], "stopped")
        self.assertFalse(any(args[:2] == ["apt-get", "remove"] for args, _ in self.runner.calls))
        self.assertFalse(any("DROP DATABASE" in str(args) for args, _ in self.runner.calls))

    def test_mysql_native_backup_uses_compatible_utilities(self):
        self.state["database_engine"] = "mysql"
        backend = NativeBackend(self.installer, self.state)
        backend.backup()
        dump = next(args for args, _ in self.runner.calls if args[0] == "mysqldump")
        self.assertIn("--column-statistics=0", dump)
        self.assertNotIn(self.state["db_password"], repr(dump))

    def test_database_collision_is_rejected_before_creating_anything(self):
        with patch.object(self.runner, "run", return_value=(0, "1\n")), patch.object(self.backend, "sql") as sql:
            with self.assertRaises(app.InstallerError):
                self.backend.provision_database()
        sql.assert_not_called()

    def test_distribution_and_engine_dependency_matrix(self):
        for distro, version, codename in (
            ("ubuntu", "22.04", "jammy"),
            ("ubuntu", "24.04", "noble"),
            ("ubuntu", "26.04", "resolute"),
            ("debian", "12", "bookworm"),
            ("debian", "13", "trixie"),
        ):
            for engine in ("mariadb", "mysql"):
                with self.subTest(distro=distro, version=version, engine=engine):
                    self.runner.calls.clear()
                    self.state["database_engine"] = engine
                    backend = NativeBackend(self.installer, self.state)
                    with (
                        patch.object(
                            self.installer, "check_host", return_value={"ID": distro, "VERSION_ID": version, "VERSION_CODENAME": codename}
                        ),
                        patch.object(self.installer, "fetch", return_value=b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture"),
                        patch.object(backend, "composer"),
                        patch.object(app.platform, "machine", return_value="x86_64"),
                    ):
                        if version == "26.04":
                            with self.assertRaisesRegex(app.InstallerError, "configured PHP"):
                                backend.dependencies()
                            self.assertFalse(any(args[0] == "apt-get" for args, _ in self.runner.calls))
                            continue
                        backend.dependencies()
                    commands = [args for args, _ in self.runner.calls]
                    self.assertTrue(any("php8.3-intl" in args for args in commands))
                    expected = (
                        "mysql-community-server"
                        if engine == "mysql" and distro == "debian"
                        else "mysql-server"
                        if engine == "mysql"
                        else "mariadb-server"
                    )
                    self.assertTrue(any(expected in args and args[0] == "apt-get" for args in commands))
                    if engine == "mysql" and distro == "debian":
                        self.assertIn(codename, (self.paths.root / "etc/apt/sources.list.d/ptero-mysql.list").read_text())
                    if engine == "mariadb" and version == "22.04":
                        self.assertIn("11.4", (self.paths.root / "etc/apt/sources.list.d/ptero-mariadb.list").read_text())
                    self.assertFalse(any("docker" in args for args in commands))

    def test_opposite_installed_database_engine_is_not_replaced(self):
        original = self.runner.run

        def existing(args, **kwargs):
            if str(args[0]) == "dpkg-query" and args[-1] == "mysql-server":
                return 0, "install ok installed"
            return original(args, **kwargs)

        with (
            patch.object(self.installer, "check_host", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}),
            patch.object(self.runner, "run", side_effect=existing),
            self.assertRaises(app.InstallerError),
        ):
            self.backend.dependencies()
        self.assertFalse(any(args[0] == "apt-get" for args, _ in self.runner.calls))

    def test_existing_unsupported_mariadb_is_not_upgraded_implicitly(self):
        original = self.runner.run

        def existing(args, **kwargs):
            if str(args[0]) == "dpkg-query" and args[-1] == "mariadb-server":
                return 0, "install ok installed|1:10.6.22-0ubuntu0.22.04.1"
            return original(args, **kwargs)

        with (
            patch.object(self.installer, "check_host", return_value={"ID": "ubuntu", "VERSION_ID": "22.04"}),
            patch.object(self.runner, "run", side_effect=existing),
            self.assertRaises(app.InstallerError),
        ):
            self.backend.dependencies()
        self.assertFalse(any(args[0] == "apt-get" for args, _ in self.runner.calls))

    def test_nginx_real_syntax_http_https_and_maintenance(self):
        nginx, openssl = shutil.which("nginx"), shutil.which("openssl")
        if not nginx or not openssl or not Path("/etc/nginx/fastcgi_params").exists():
            self.skipTest("NGINX/OpenSSL binaries are not available")
        prefix = self.paths.root / "nginx-test"
        prefix.mkdir()
        (prefix / "fastcgi_params").write_bytes(Path("/etc/nginx/fastcgi_params").read_bytes())
        self.state.update(url="https://panel.example.test:18443", tls=True, http_port=18080, https_port=18443)
        cert = self.backend.certdir / "live/panel.example.test"
        cert.mkdir(parents=True)
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=panel.example.test",
                "-keyout",
                str(cert / "privkey.pem"),
                "-out",
                str(cert / "fullchain.pem"),
            ],
            capture_output=True,
            check=True,
        )
        for tls, maintenance in ((False, False), (True, False), (True, True)):
            self.state["tls"] = tls
            config = prefix / "nginx.conf"
            config.write_text(
                f"error_log stderr;\npid {prefix}/nginx.pid;\nevents {{}}\n"
                f"http {{\naccess_log off;\n{self.backend.nginx_config(maintenance)}\n"
                f"}}"
            )
            result = subprocess.run([nginx, "-t", "-p", str(prefix), "-c", str(config)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_purge_backs_up_before_deleting_reserved_database(self):
        self.ui.choices = [True]
        self.backend.uninstall()
        self.assertFalse(self.backend.app.exists())
        self.assertFalse(self.paths.panel.exists())
        self.assertIn(b"DROP DATABASE ptero_panel", self.runner.restored)
        self.assertEqual(len(list(self.paths.backups.glob("*.tar.gz"))), 1)

    def test_native_update_failure_keeps_maintenance_and_recovery_backup(self):
        with (
            patch.object(self.backend, "source"),
            patch.object(self.backend, "install", side_effect=app.InstallerError("migration failed")),
        ):
            with self.assertRaises(app.InstallerError):
                self.backend.update({"tag_name": "v2.0.0-beta.3"})
        self.assertEqual(self.state["status"], "updating")
        self.assertTrue(Path(self.state["recovery_backup"]).exists())


class DockerWingsTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.wings_state = {
            "schema": 1,
            "owner": app.PROJECT,
            "method": "docker",
            "version": app.WINGS_VERSION,
            "status": "pending",
            "image": "ghcr.io/pterodactyl/wings@sha256:" + "d" * 64,
        }
        self.backend = DockerWings(self.installer, self.wings_state)

    def test_wings_container_has_host_network_and_matching_game_paths(self):
        self.backend.save()
        config = app.read_json(self.backend.compose)["services"]["wings"]
        self.assertEqual(config["network_mode"], "host")
        targets = {volume["target"] for volume in config["volumes"]}
        self.assertTrue({"/var/run/docker.sock", "/run/wings", "/var/lib/pterodactyl", "/tmp/pterodactyl"}.issubset(targets))
        self.assertEqual(config["healthcheck"]["test"], ["CMD", "/usr/bin/wings", "version"])
        self.assertNotIn("privileged", config)

    def test_wings_container_requires_a_node_configuration(self):
        self.backend.save()
        with self.assertRaises(app.InstallerError):
            self.backend.start()

    def test_uninstall_wings_container_preserves_games(self):
        self.backend.save()
        app.atomic_write(self.paths.config, "token: test-secret\n")
        world = self.paths.root / "var/lib/pterodactyl/volumes/game/world.dat"
        app.atomic_write(world, "game files")
        self.backend.uninstall()
        self.assertEqual(world.read_text(), "game files")
        self.assertFalse(self.backend.compose.exists())
        self.assertFalse(any("prune" in args or "--volumes" in args for args, _ in self.runner.calls))

    def test_container_update_failure_restores_previous_image_and_node_config(self):
        self.backend.save()
        app.atomic_write(self.paths.config, "token: original-secret\n")
        self.wings_state["status"] = "ready"
        self.backend.save()
        with (
            patch.object(self.backend, "start", side_effect=app.InstallerError("failed startup")),
            patch.object(self.installer, "pin_image", return_value="ghcr.io/pterodactyl/wings@sha256:" + "e" * 64),
        ):
            with self.assertRaises(app.InstallerError):
                self.backend.update({"tag_name": "v1.13.5"})
        state = app.read_json(self.paths.wings_state)
        self.assertEqual(state["version"], app.WINGS_VERSION)
        self.assertEqual(state["image"], "ghcr.io/pterodactyl/wings@sha256:" + "d" * 64)
        self.assertIn("original-secret", self.paths.config.read_text())

    def test_real_wings_compose_schema_without_docker_daemon(self):
        if not shutil.which("docker"):
            self.skipTest("Docker CLI unavailable")
        self.backend.save()
        result = subprocess.run(["docker", "compose", "-f", str(self.backend.compose), "config", "--quiet"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class PreviewTests(BaseTest):
    def test_preview_in_all_languages_without_root_or_runtime_state(self):
        from ptero.cli import main

        for language, expected in (("en", "Ready to install"), ("es", "Listo para instalar"), ("pt-BR", "Pronto para instalar")):
            with patch.object(app.os, "geteuid", return_value=1000), contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(["--language", language, "--preview", "--no-animation"])
            self.assertEqual(result, 0)
            self.assertIn(expected, output.getvalue())

    def test_single_file_launcher_contains_and_runs_the_same_version(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_release_test", Path(__file__).resolve().parents[1] / "scripts/build_release.py"
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        builder.OUTPUT = Path(self.temporary.name)
        with contextlib.redirect_stdout(io.StringIO()):
            _, launcher = builder.build()
        result = subprocess.run(["bash", str(launcher), "--version"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pterodactyl Installer " + __version__, result.stdout)
