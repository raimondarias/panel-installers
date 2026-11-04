"""Preview isolation and lifecycle tests; never install packages or services."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ptero.cli import ENTRIES
from ptero.core import Cancelled, InstallerError
from ptero.preview import Simulation, TourUI, show
from ptero.ui import UI
from ptero.wizard import panel_plan
from test_installer import BaseTest

ROOT = Path(__file__).resolve().parents[1]


class ScenarioUI(TourUI):
    def __init__(self, ui, **choices):
        super().__init__(ui)
        self.choices = choices
        self.prompts = []

    def choice(self, title, options, default=0):
        if title in self.choices:
            return self.choices[title]
        return super().choice(title, options, default)

    def ask(self, label, default=None, validator=None, secret=False):
        self.prompts.append((label, secret))
        if label == "Game database hostname / IPv4":
            value = "sql.example.com"
        elif label == "Database host provisioning username" and default is None:
            value = "preview_manager"
        elif label == "Database host provisioning password":
            value = "preview-database-password-123!"
        else:
            return super().ask(label, default, validator, secret)
        assert not validator or validator(value)
        return value


class SimulationTests(unittest.TestCase):
    def test_full_tour_all_languages_cannot_use_system_network_or_files(self):
        # Construct the catalogue before denying filesystem APIs.
        for language in ("en", "es", "pt-BR"):
            ui = TourUI(UI(False, language))
            simulation = Simulation(ui)
            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(output))
                for name in (
                    "subprocess.Popen",
                    "urllib.request.OpenerDirector.open",
                    "socket.socket",
                    "ptero.core.Runner.run",
                    "ptero.core.atomic_write",
                    "pathlib.Path.open",
                    "os.mkdir",
                    "os.replace",
                    "os.unlink",
                ):
                    stack.enter_context(patch(name, side_effect=AssertionError("Preview side effect: " + name)))
                simulation.dispatch("install-all")
                for action in ("backup-panel", "backup-wings", "diagnose", "logs", "firewall"):
                    simulation.dispatch(action)
            self.assertEqual(simulation.inventory()["panel"], "ready")
            self.assertEqual(simulation.inventory()["wings"], "ready")
            self.assertEqual(simulation.inventory()["database"], "ready")
            for secret in ("Preview-only-password-123!", "preview-token", "preview-smtp-password"):
                self.assertNotIn(secret, output.getvalue())
            self.assertFalse(hasattr(simulation, "p"))
            self.assertFalse(hasattr(simulation.run, "run"))

    def test_actual_panel_form_matrix_and_game_database_modes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for advanced in (False, True):
                for method in ("standalone", "docker"):
                    for engine in ("mariadb", "mysql"):
                        for access in ("domain", "ip"):
                            for mode in ("local", "existing", "later"):
                                with self.subTest(advanced=advanced, method=method, engine=engine, access=access, mode=mode):
                                    ui = ScenarioUI(
                                        UI(False),
                                        **{
                                            "Setup experience": advanced,
                                            "Panel deployment": method,
                                            "Database engine": engine,
                                            "How will you access the Panel?": access,
                                            "Where are the game databases?": mode,
                                        },
                                    )
                                    simulation = Simulation(ui)
                                    simulation.dispatch("install-panel")
                                    state = simulation.panel
                                    self.assertEqual((state["method"], state["database_engine"]), (method, engine))
                                    self.assertEqual(state["tls"], access == "domain")
                                    self.assertEqual(state["game_database"]["mode"], mode)
                                    self.assertEqual(simulation.inventory()["credentials"], mode != "later")
                                    self.assertIn(("SMTP password", True), ui.prompts)
                                    self.assertNotIn(simulation.admin_password, json.dumps(state))

    def test_every_menu_action_has_a_simulated_path_and_lifecycle(self):
        ui = TourUI(UI(False))
        simulation = Simulation(ui)
        visited = set()
        with contextlib.redirect_stdout(io.StringIO()):
            for action in (
                "install-panel",
                "install-wings",
                "install-all",
                "configure-database",
                "create-node",
                "remote-node",
                "remote-database",
                "health",
                "backup-schedule",
                "backup-run",
                "backup-download",
                "backup-retention",
                "database-credentials",
                "backup-panel",
                "backup-wings",
                "configure-wings",
                "tls-wings",
                "update-panel",
                "rollback-panel",
                "update-wings",
                "smtp",
                "admin",
                "diagnose",
                "restart",
                "logs",
                "firewall",
                "uninstall-all",
                "restore-panel",
                "uninstall-panel",
                "install-wings",
                "uninstall-wings",
            ):
                simulation.dispatch(action)
                visited.add(action)
            simulation.wings = {"status": "pending"}
            simulation.dispatch("resume")
            simulation.wings = None
            visited.add("resume")
        self.assertEqual(visited, {action for _, action in ENTRIES})
        self.assertIsNone(simulation.panel)
        self.assertIsNone(simulation.wings)
        self.assertIsNotNone(simulation.database)  # Independent SQL lifecycle.

    def test_removed_game_data_backup_operations_have_no_simulated_path(self):
        simulation = Simulation(TourUI(UI(False)))
        with contextlib.redirect_stdout(io.StringIO()):
            simulation.dispatch("install-all")
        for action in ("backup-games", "restore-games", "backup-game-database", "restore-game-database"):
            with self.assertRaisesRegex(InstallerError, "Unknown preview action"):
                simulation.dispatch(action)
        self.assertEqual(simulation.backups, {})

    def test_restore_requires_backup_and_empty_destination(self):
        simulation = Simulation(TourUI(UI(False)))
        with self.assertRaisesRegex(InstallerError, "backup first"):
            simulation.dispatch("restore-panel")
        with contextlib.redirect_stdout(io.StringIO()):
            simulation.dispatch("install-panel")
            simulation.dispatch("backup-panel")
        with self.assertRaisesRegex(InstallerError, "empty destination"):
            simulation.dispatch("restore-panel")

    def test_cancel_does_not_commit_partial_panel_plan(self):
        ui = TourUI(UI(False))
        simulation = Simulation(ui)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(ui, "confirm", side_effect=Cancelled()):
            with self.assertRaises(Cancelled):
                simulation.dispatch("install-panel")
        self.assertIsNone(simulation.panel)
        self.assertIsNone(simulation.database)

    def test_fresh_preview_prerequisites_fail_before_questions(self):
        simulation = Simulation(TourUI(UI(False)))
        with patch.object(simulation.ui, "ask", side_effect=AssertionError("unexpected question")):
            with self.assertRaisesRegex(InstallerError, "Install the Panel first"):
                simulation.dispatch("configure-database")

    def test_generated_admin_password_is_displayed_and_not_saved_in_state(self):
        ui = ScenarioUI(UI(False))
        original = ui.ask

        def ask(label, *args, **kwargs):
            return "" if label.startswith("Administrator password") else original(label, *args, **kwargs)

        simulation = Simulation(ui)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(ui, "ask", side_effect=ask),
            patch.object(ui.base, "card", wraps=ui.base.card) as card,
        ):
            simulation.panel = panel_plan(simulation)
        self.assertTrue(simulation.generated_admin_password)
        self.assertNotIn(simulation.admin_password, json.dumps(simulation.panel))
        self.assertTrue(any(call.args[0] == "Save your generated password" for call in card.call_args_list))

    def test_noninteractive_tour_does_not_ask_for_input(self):
        ui = UI(False, "es")
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            patch("sys.stdin.isatty", return_value=False),
            patch("builtins.input", side_effect=AssertionError("input in automatic tour")),
            patch("getpass.getpass", side_effect=AssertionError("password in automatic tour")),
        ):
            show(ui)
        self.assertIn("Simulación completada", output.getvalue())
        self.assertIn("Configurando el servidor web", output.getvalue())

    def test_both_launchers_refuse_to_install_missing_python_in_preview_even_as_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary)
            for name in ("dirname", "curl"):
                (binary / name).symlink_to("/usr/bin/" + name)
            marker = binary / "apt-called"
            apt = binary / "apt-get"
            apt.write_text("#!/bin/bash\nprintf touched > '" + str(marker) + "'\nexit 99\n")
            apt.chmod(0o755)
            for launcher in ("install.sh", "scripts/run_local.sh"):
                result = subprocess.run(
                    ["/bin/bash", str(ROOT / launcher), "--language", "en", "--preview"],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PATH": str(binary)},
                    timeout=5,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Python 3", result.stderr)
                self.assertFalse(marker.exists())


class StateShapeTests(BaseTest):
    def test_invalid_nested_credentials_are_reported_without_overwriting(self):
        from ptero.navigation import inventory

        self.paths.state.parent.mkdir(parents=True)
        for invalid in ({"game_database": []}, {"db_password": []}, {"game_database": {"password": []}}):
            text = json.dumps({**self.state, **invalid})
            self.paths.state.write_text(text)
            self.assertEqual(inventory(self.installer)["panel"], "invalid")
            self.assertEqual(self.paths.state.read_text(), text)
        self.assertEqual(self.runner.calls, [])

    def test_json_lists_and_scalars_are_invalid_dashboard_state_without_writes(self):
        from ptero.navigation import inventory

        self.paths.state.parent.mkdir(parents=True)
        for value in ([], None, "invalid", 1, True):
            text = json.dumps(value)
            self.paths.state.write_text(text)
            self.assertEqual(inventory(self.installer)["panel"], "invalid")
            self.assertEqual(self.paths.state.read_text(), text)
        self.assertEqual(self.runner.calls, [])
