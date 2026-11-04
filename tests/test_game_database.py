"""Game SQL lifecycle, registration boundaries and real isolated MariaDB checks."""

import json
import contextlib
import io
import os
import secrets
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

from test_installer import BaseTest, FakeRunner, QuietUI
import installer as app
from ptero.game_database import GameDatabase, OWNER_LABEL, plan, valid_password, valid_endpoint


class DatabaseRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.gateway = "172.25.0.1"
        self.label = app.PROJECT
        self.network_exists = True
        self.registration_output = '{"id":7}'

    def run(self, args, **kwargs):
        code, output = super().run(args, **kwargs)
        args = list(map(str, args))
        if args[:3] == ["docker", "network", "inspect"]:
            return (
                (
                    0,
                    json.dumps(
                        [
                            {
                                "Driver": "bridge",
                                "Labels": {OWNER_LABEL: self.label},
                                "Options": {"com.docker.network.bridge.name": "ptero-db0"},
                                "IPAM": {"Config": [{"Gateway": self.gateway, "Subnet": "172.25.0.0/16"}]},
                            }
                        ]
                    ),
                )
                if self.network_exists
                else (1, "")
            )
        if args[:3] == ["docker", "network", "create"]:
            self.network_exists = True
        if "Registering the game database host" in kwargs.get("label", ""):
            return 0, self.registration_output
        return code, output


class HostUI(QuietUI):
    def __init__(self, mode, answers=()):
        super().__init__(answers)
        self.mode = mode

    def choice(self, title, options, default=0):
        return "docker" if title == "Game SQL deployment" else self.mode


class GameDatabaseTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.runner = DatabaseRunner()
        self.installer.run = self.runner
        self.host = GameDatabase(self.installer)
        self.state.update(method="standalone", database_engine="mariadb")
        self.settings = {
            "mode": "local",
            "engine": "mariadb",
            "name": "Node games",
            "port": 3307,
            "username": "ptero_games",
            "password": "a" * 64,
            "node_id": None,
            "registered": False,
        }
        self.state["game_database"] = self.settings
        self.installer.save_panel(self.state)

    def setup_local(self):
        with patch.object(self.installer, "docker"), patch.object(self.installer, "port_free"):
            self.host.setup(self.state)

    def test_one_engine_selection_is_reused_by_game_host(self):
        self.installer.ui = HostUI("local")
        self.state["database_engine"] = "mysql"
        settings = plan(self.installer, self.state)
        self.assertEqual(settings["engine"], "mysql")
        self.assertEqual(settings["mode"], "local")
        self.assertTrue(valid_password(settings["password"]))

    def test_advanced_host_keeps_entered_credentials_and_node_id(self):
        password = "A-strong-'\\$;password-123!"
        self.installer.ui = HostUI("local", ["Node databases", "3407", "game_manager", password, password, "12"])
        settings = plan(self.installer, self.state, advanced=True)
        self.assertEqual(settings["username"], "game_manager")
        self.assertEqual(settings["password"], password)
        self.assertEqual(settings["node_id"], 12)
        self.assertEqual(settings["port"], 3407)

    def test_remote_endpoint_validation_and_wizard(self):
        for value in ("localhost", "LOCALHOST", "127.0.0.2", "0.0.0.0", "169.254.2.3", "::1", "host/invalid", "224.0.0.2"):
            self.assertFalse(valid_endpoint(value), value)
        for value in ("10.0.0.9", "db.example.test"):
            self.assertTrue(valid_endpoint(value), value)
        self.installer.ui = HostUI("existing", ["10.0.0.9", "3306", "node_manager", "Existing-password-123!"])
        settings = plan(self.installer, self.state)
        self.assertEqual(settings["host"], "10.0.0.9")
        self.assertEqual(settings["password"], "Existing-password-123!")

    def test_skip_never_runs_package_or_service_commands(self):
        self.state["game_database"] = {"mode": "later"}
        self.host.setup(self.state)
        self.assertEqual(self.runner.calls, [])

    def test_local_registers_on_both_panel_backends_without_changing_app_database(self):
        for method in ("standalone", "docker"):
            with self.subTest(method=method):
                self.state["method"] = method
                self.setup_local()
                self.assertEqual(self.settings["host"], "172.25.0.1")
                self.assertEqual(self.settings["host_id"], 7)
                self.assertTrue(self.settings["registered"])
                self.assertEqual(self.state["db_password"], "b" * 64)
                config = json.loads(self.host.compose.read_text())
                self.assertEqual(config["services"]["database"]["ports"], ["172.25.0.1:3307:3306"])
                self.assertNotIn(str(self.paths.data), json.dumps(config))
                registration = [
                    (args, kw) for args, kw in self.runner.calls if "Registering the game database host" in kw.get("label", "")
                ][-1]
                self.assertEqual(registration[0][0], "runuser" if method == "standalone" else "docker")
                self.assertNotIn(self.settings["password"], repr(registration[0]))
                self.assertEqual(json.loads(self.runner.restored)["password"], self.settings["password"])

    def test_private_credential_report_and_log_redaction(self):
        self.setup_local()
        report = json.loads(self.host.report.read_text())
        self.assertEqual(report["password"], self.settings["password"])
        self.assertEqual(report["port"], 3307)
        self.assertEqual(self.host.report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.host.state_file.stat().st_mode & 0o777, 0o600)
        for secret in (self.settings["password"], self.host.load()["root_password"]):
            self.assertNotIn(secret, self.runner.redact(secret))

    def test_sql_credentials_use_private_stdin_after_readiness_and_before_supervision(self):
        captured = []
        original = self.runner.run

        def command(args, **kwargs):
            if kwargs.get('label') == 'Preparing the game SQL provisioning account':
                captured.append((list(args), kwargs['input_file'].read().decode()))
                kwargs['input_file'].seek(0)
            return original(args, **kwargs)

        with patch.object(self.runner, 'run', side_effect=command):
            self.setup_local()
        args, sql = captured[0]
        self.assertNotIn(self.settings['password'], repr(args))
        self.assertIn(self.settings['password'], sql)
        self.assertIn('CREATE USER IF NOT EXISTS', sql)
        self.assertIn('ALTER USER', sql)
        self.assertNotIn('docker-entrypoint-initdb.d', self.host.compose.read_text())
        self.assertEqual(self.host.init.stat().st_mode & 0o777, 0o600)
        commands = [args for args, _ in self.runner.calls]
        provision = next(index for index, args in enumerate(commands) if 'MYSQL_PWD=' in ' '.join(args))
        supervisor = commands.index(['systemctl', 'start', 'ptero-game-databases.service'])
        self.assertLess(provision, supervisor)

    def test_passwords_are_not_localized_as_interface_labels(self):
        self.settings.update(mode="existing", host="10.0.0.9", password="Standalone (Recommended)", registered=True, host_id=7)
        ui = app.UI(False, "es")
        self.host.ui = ui
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.host.credentials(self.state)
        self.assertIn("Contraseña: Standalone (Recommended)", stream.getvalue())

    def test_local_retry_preserves_root_credentials_and_image(self):
        self.setup_local()
        before = self.host.load()
        self.setup_local()
        after = self.host.load()
        for key in ("root_password", "password", "image", "host"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(sum(args[:2] == ["docker", "pull"] for args, _ in self.runner.calls), 1)

    def test_existing_local_instance_reuses_password_in_wizard(self):
        self.setup_local()
        self.installer.ui = HostUI("local")
        settings = plan(self.installer, self.state)
        self.assertEqual(settings["password"], self.settings["password"])

    def test_unowned_network_and_files_are_refused(self):
        self.runner.label = "someone-else"
        with self.assertRaises(app.InstallerError):
            self.setup_local()
        self.assertFalse(self.host.state_file.exists())
        self.runner.label = app.PROJECT
        self.host.base.mkdir(parents=True)
        (self.host.base / "existing-data").write_text("leave alone")
        with self.assertRaises(app.InstallerError):
            self.setup_local()
        self.assertEqual((self.host.base / "existing-data").read_text(), "leave alone")

    def test_create_network_and_refuse_wrong_gateway_on_retry(self):
        self.runner.network_exists = False
        self.setup_local()
        self.assertTrue(any(args[:3] == ["docker", "network", "create"] for args, _ in self.runner.calls))
        self.runner.gateway = "172.25.0.2"
        with self.assertRaises(app.InstallerError):
            self.setup_local()

    def test_private_gateway_validation(self):
        for gateway in ("8.8.8.8", "127.0.0.1", "::1", "192.168.1.1", "bad"):
            with self.subTest(gateway=gateway):
                self.runner.gateway = gateway
                with self.assertRaises(app.InstallerError):
                    self.host.network()

    def test_engine_or_credentials_change_requires_migration(self):
        self.setup_local()
        self.settings["password"] = "c" * 64
        with self.assertRaises(app.InstallerError):
            self.setup_local()

    def test_remote_registration_never_installs_docker_or_sql(self):
        self.settings.update(mode="existing", host="10.0.0.9", port=3306)
        with patch.object(self.installer, "docker") as docker:
            self.host.setup(self.state)
        docker.assert_not_called()
        self.assertFalse(self.host.state_file.exists())
        self.assertTrue(self.settings["registered"])
        self.assertFalse(hasattr(self.host, "backup"))

    def test_registration_failure_leaves_panel_ready_and_can_retry(self):
        self.runner.registration_output = "invalid output"
        with self.assertRaises(app.InstallerError):
            self.setup_local()
        self.assertEqual(self.installer.panel_state()["status"], "ready")
        self.assertFalse(self.installer.panel_state()["game_database"]["registered"])
        self.runner.registration_output = '{"id":7}'
        with patch.object(self.installer, "check_host"):
            self.installer.install_panel()
        self.assertTrue(self.installer.panel_state()["game_database"]["registered"])

    def test_registration_contract_php_passes_real_syntax_check(self):
        if not shutil.which("php"):
            self.skipTest("PHP CLI unavailable")
        self.settings.update(mode="existing", host="10.0.0.9")
        self.host.register(self.state, self.settings)
        args, _ = self.runner.calls[-1]
        code = args[args.index("-r") + 1]
        result = subprocess.run(["php", "-l"], input="<?php " + code, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CreatesDatabaseHosts", code)
        self.assertIn("Crypt::decrypt", code)
        self.assertIn("SELECT 1 FROM dual", code)

    def test_both_engine_configs_pass_real_compose_and_hide_password_interpolation(self):
        if not shutil.which("docker"):
            self.skipTest("Docker CLI unavailable")
        for engine in ("mysql", "mariadb"):
            state = {
                **self.settings,
                "engine": engine,
                "host": "172.25.0.1",
                "image": engine + ":8.4",
                "root_password": "password$with${variables}",
            }
            self.host.base.mkdir(parents=True, exist_ok=True)
            self.host.compose.write_text(json.dumps(self.host.config(state)))
            result = subprocess.run(
                ["docker", "compose", "-f", str(self.host.compose), "config", "--format", "json"], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("variable is not set", result.stderr)
            service = json.loads(result.stdout)["services"]["database"]
            self.assertEqual(service["ports"][0]["host_ip"], "172.25.0.1")
            self.assertNotIn("MYSQL_DATABASE", service["environment"])
            self.assertNotIn("MARIADB_DATABASE", service["environment"])
            if engine == 'mysql':
                self.assertIn('mysqladmin --host=127.0.0.1', service['healthcheck']['test'][1])
                self.assertIn('--protocol=socket', service['healthcheck']['test'][1])

    def test_firewall_installed_before_database_and_does_not_flush_other_rules(self):
        self.setup_local()
        script = self.host.firewall_script.read_text()
        self.assertNotIn(" -F", script)
        self.assertNotIn(" -P", script)
        self.assertIn("DOCKER-USER", script)
        self.assertIn("INPUT", script)
        self.assertIn("pterodactyl0", script)
        self.assertIn("-j DROP", script)
        result = subprocess.run(["sh", "-n", str(self.host.firewall_script)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [args for args, _ in self.runner.calls]
        firewall = commands.index(["systemctl", "restart", "ptero-game-db-firewall.service"])
        start = next(i for i, args in enumerate(commands) if "up" in args and "database" in args)
        self.assertLess(firewall, start)

    def test_no_supported_firewall_backend_never_starts_database(self):
        self.runner.failure = lambda args: args[:4] == ["iptables", "-w", "-S", "DOCKER-USER"]
        with self.assertRaises(app.InstallerError):
            self.setup_local()
        self.assertFalse(any("up" in args and "database" in args for args, _ in self.runner.calls))

    def test_boot_waits_for_firewall_and_uses_only_one_restart_manager(self):
        self.setup_local()
        service = self.host.service.read_text()
        self.assertIn("After=docker.service ptero-game-db-firewall.service", service)
        self.assertIn("Requires=docker.service ptero-game-db-firewall.service", service)
        self.assertIn("WantedBy=docker.service", service)
        self.assertIn("--abort-on-container-exit --exit-code-from database", service)
        self.assertEqual(json.loads(self.host.compose.read_text())["services"]["database"]["restart"], "no")

    def test_unhealthy_database_stops_its_supervisor_and_preserves_data(self):
        self.runner.failure = lambda args: "up" in args and "--wait" in args
        with self.assertRaises(app.InstallerError):
            self.setup_local()
        self.assertEqual(self.host.load()["status"], "installing")
        self.assertTrue(self.host.data.is_dir())
        self.assertIn(["systemctl", "stop", "ptero-game-databases.service"], [args for args, _ in self.runner.calls])
        self.assertNotIn("database", self.runner.running)

    def test_panel_purge_preserves_game_database_project_and_data(self):
        self.state["method"] = "docker"
        self.panel()
        self.setup_local()
        (self.host.data / "ibdata1").write_bytes(b"keep-game-database")
        self.ui.choices = [True]
        with patch.object(self.installer, "backup_panel", return_value=Path("backup.tar.gz")):
            self.installer.uninstall_panel()
        self.assertTrue(self.host.state_file.exists())
        self.assertEqual((self.host.data / "ibdata1").read_bytes(), b"keep-game-database")
        self.assertFalse(self.paths.panel.exists())
        self.installer.game_database_credentials()
        self.assertTrue(self.host.report.is_file())


class RealSQLTests(BaseTest):
    def test_real_mariadb_provisioning_and_game_user_lifecycle_with_special_password(self):
        binaries = {name: shutil.which(name) for name in ("mariadb-install-db", "mariadbd", "mariadb")}
        if not all(binaries.values()):
            self.skipTest("MariaDB binaries unavailable")
        # No system service or TCP listener: a disposable server uses only its
        # own Unix socket and temporary files, then terminates before cleanup.
        with tempfile.TemporaryDirectory(prefix="ptero-sql-") as directory:
            base = Path(directory)
            data, socket = base / "data", base / "mysql.sock"
            args = [
                binaries["mariadb-install-db"],
                "--no-defaults",
                "--datadir=" + str(data),
                "--auth-root-authentication-method=normal",
                "--skip-test-db",
            ]
            if os.geteuid() == 0:
                args += ["--user=root"]
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            server_args = [
                binaries["mariadbd"],
                "--no-defaults",
                "--datadir=" + str(data),
                "--socket=" + str(socket),
                "--skip-networking",
                "--pid-file=" + str(base / "server.pid"),
                "--log-error=" + str(base / "server.log"),
            ]
            if os.geteuid() == 0:
                server_args += ["--user=root"]
            with (base / "process.log").open("w") as log:
                process = subprocess.Popen(server_args, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail((base / "server.log").read_text())
                        if socket.exists():
                            result = subprocess.run(
                                [binaries["mariadb"], "--no-defaults", "--socket=" + str(socket), "--user=root", "--execute=SELECT 1"],
                                capture_output=True,
                            )
                            if result.returncode == 0:
                                break
                        time.sleep(0.05)
                    else:
                        self.fail("Temporary SQL server did not become ready")
                    password = "A-strong-'\\$;password-123!"
                    host = GameDatabase(self.installer)
                    settings = {"username": "ptero_games", "password": password}
                    command = [binaries["mariadb"], "--no-defaults", "--socket=" + str(socket), "--batch"]
                    result = subprocess.run([*command, "--user=root"], input=host.init_sql(settings), text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    game_password = secrets.token_hex(16)  # Disposable account in this temporary server.
                    sql = (
                        f"CREATE DATABASE s1_example; CREATE USER 'u1_test'@'%' IDENTIFIED BY '{game_password}';"
                        "GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,ALTER,REFERENCES,INDEX,LOCK TABLES,CREATE ROUTINE,ALTER "
                        "ROUTINE,EXECUTE,CREATE TEMPORARY TABLES,CREATE VIEW,SHOW VIEW,EVENT,TRIGGER ON s1_example.* "
                        "TO 'u1_test'@'%';"
                        "FLUSH PRIVILEGES; CREATE TABLE s1_example.test (id INT); INSERT INTO s1_example.test VALUES (42);"
                    )
                    result = subprocess.run(
                        [*command, "--user=ptero_games"],
                        input=sql,
                        env={**os.environ, "MYSQL_PWD": password},
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run(
                        [*command, "--user=u1_test", "--execute=SELECT id FROM s1_example.test"],
                        env={**os.environ, "MYSQL_PWD": game_password},
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("42", result.stdout)
                    result = subprocess.run(
                        [*command, "--user=ptero_games"],
                        input="DROP DATABASE s1_example; DROP USER 'u1_test'@'%';",
                        env={**os.environ, "MYSQL_PWD": password},
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
