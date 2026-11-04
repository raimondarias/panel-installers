"""Guided Panel update rollback for Docker and standalone deployments."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_installer import BaseTest
import installer as app
from ptero.native import NativeBackend
from ptero.navigation import inventory, prerequisite

NEW = "v2.0.0-beta.3"


def commands(runner):
    return [" ".join(args) for args, _ in runner.calls]


class DockerRollbackTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.panel()
        (self.paths.data / "extensions/old-extension.txt").write_text("compatible with the old release")
        self.original_image = self.installer.panel_state().get("panel_image")

    def update(self, failure=None, choices=()):
        self.runner.failure = failure or (lambda args: False)
        self.ui.choices = list(choices)
        with patch.object(self.installer, "choose_release", return_value={"tag_name": NEW}):
            self.installer.update_panel()

    def test_update_shows_plan_and_records_a_rollback_point(self):
        self.update()
        state = self.installer.panel_state()
        self.assertEqual((state["status"], state["version"], state["previous_version"]), ("ready", NEW, app.PANEL_VERSION))
        self.assertTrue(Path(state["recovery_backup"]).is_file())
        messages = " ".join(str(message) for message, _ in self.ui.messages)
        self.assertIn("Panel update plan", messages)
        self.assertIn("Roll back Panel update", messages)
        dashboard = inventory(self.installer)
        self.assertTrue(dashboard["rollback"])
        self.assertEqual(dashboard["panel_version"], NEW)
        self.assertEqual(prerequisite("rollback-panel", dashboard), "")

    def test_manual_rollback_restores_previous_release_sql_and_extensions(self):
        self.update()
        (self.paths.data / "extensions/old-extension.txt").unlink()
        (self.paths.data / "extensions/new-extension.txt").write_text("needs the new release")
        self.runner.calls.clear()
        self.installer.rollback_panel()
        state = self.installer.panel_state()
        self.assertEqual((state["status"], state["version"], state["rolled_back_from"]), ("ready", app.PANEL_VERSION, NEW))
        self.assertNotIn("recovery_backup", state)
        self.assertNotIn("previous_version", state)
        self.assertEqual(state.get("panel_image"), self.original_image or "ghcr.io/pterodactyl/panel:" + app.PANEL_VERSION)
        self.assertTrue(Path(state["rollback_safety_backup"]).is_file())
        self.assertIn("ROLL BACK PANEL", self.ui.confirmations)
        self.assertTrue((self.paths.data / "extensions/old-extension.txt").is_file())
        self.assertFalse((self.paths.data / "extensions/new-extension.txt").exists())
        self.assertEqual((self.paths.data / "var/.env").read_text(), 'APP_KEY="original-key"\nHASHIDS_SALT="original-salt"\n')
        executed = commands(self.runner)
        reset = next(index for index, command in enumerate(executed) if "DROP DATABASE IF EXISTS panel" in command)
        restore = next(index for index, command in enumerate(executed) if "--execute" not in command
                       and ("exec mariadb " in command or "exec mysql " in command))
        self.assertLess(reset, restore)
        self.assertEqual(self.runner.restored, self.runner.dump)
        self.assertIn("panel", self.runner.running)
        compose = json.loads(self.paths.compose.read_text())
        self.assertNotIn(NEW, compose["services"]["panel"]["image"])
        self.assertEqual(prerequisite("rollback-panel", inventory(self.installer)).split(".")[0],
                         "No Panel update can be rolled back")

    def test_failed_update_offers_rollback_without_a_safety_copy(self):
        with self.assertRaisesRegex(app.InstallerError, "rolled back"):
            self.update(failure=lambda args: "migrate" in args, choices=[True])
        state = self.installer.panel_state()
        self.assertEqual((state["status"], state["version"]), ("ready", app.PANEL_VERSION))
        self.assertNotIn("rollback_safety_backup", state)
        self.assertNotIn("ROLL BACK PANEL", self.ui.confirmations)
        self.assertEqual(len(list(self.paths.backups.glob("panel-*.tar.gz"))), 1)

    def test_resume_after_failed_update_can_roll_back(self):
        with self.assertRaises(app.InstallerError):
            self.update(failure=lambda args: "migrate" in args, choices=[False])
        self.assertEqual(self.installer.panel_state()["status"], "updating")
        dashboard = inventory(self.installer)
        self.assertEqual(prerequisite("rollback-panel", dashboard), "")
        self.runner.failure = lambda args: False
        self.ui.answers = ["2"]  # Roll back instead of retrying.
        self.installer.resume()
        self.assertEqual(self.installer.panel_state()["version"], app.PANEL_VERSION)
        self.assertEqual(self.installer.panel_state()["status"], "ready")

    def test_rollback_refuses_unverified_or_incompatible_backups_before_changes(self):
        self.update()
        archive = Path(self.installer.panel_state()["recovery_backup"])
        sidecar = Path(str(archive) + ".sha256")
        original = sidecar.read_text()
        sidecar.write_text("0" * 64 + "\n")
        self.runner.calls.clear()
        with self.assertRaisesRegex(app.InstallerError, "checksum"):
            self.installer.rollback_panel()
        sidecar.write_text(original)
        state = self.installer.panel_state()
        state["version"] = app.PANEL_VERSION
        self.installer.save_panel(state)
        with self.assertRaisesRegex(app.InstallerError, "not older"):
            self.installer.rollback_panel()
        state.update(version=NEW, method="standalone")
        self.installer.save_panel(state)
        with self.assertRaisesRegex(app.InstallerError, "different deployment"):
            self.installer.rollback_panel()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.ui.confirmations.count("ROLL BACK PANEL"), 0)

    def test_rollback_refuses_changed_application_keys(self):
        self.update()
        (self.paths.data / "var/.env").write_text('APP_KEY="other-key"\nHASHIDS_SALT="other-salt"\n')
        with self.assertRaisesRegex(app.InstallerError, "different application keys"):
            self.installer.rollback_panel()
        self.assertEqual(self.installer.panel_state()["version"], NEW)

    def test_missing_recovery_backup_is_explained(self):
        self.update()
        Path(self.installer.panel_state()["recovery_backup"]).unlink()
        dashboard = inventory(self.installer)
        self.assertFalse(dashboard["rollback"])
        self.assertIn("No Panel update can be rolled back", prerequisite("rollback-panel", dashboard))

    def test_absent_panel_never_offers_installation_for_rollback(self):
        self.paths.state.unlink()
        reason = prerequisite("rollback-panel", inventory(self.installer))
        self.assertIn("no update to roll back", reason)
        self.assertFalse(reason.startswith("Install the Panel first"))


class NativeRollbackTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.state.update(method="standalone", database_engine="mariadb", php="8.3", redis_password="c" * 64,
                          database_claimed=True)
        self.installer.save_panel(self.state)
        self.backend = NativeBackend(self.installer, self.state)
        self.backend.app.mkdir(parents=True)
        (self.backend.app / "artisan").write_text("<?php // old release")
        (self.backend.app / ".env").write_text('APP_KEY="original-key"\nHASHIDS_SALT="original-salt"\n')
        (self.backend.app / "storage/logs").mkdir(parents=True)
        self.backend.redis_data.mkdir(parents=True)

    def test_failed_native_update_rolls_back_application_and_database(self):
        def fail_after_replacing_code(*_):
            (self.backend.app / "artisan").write_text("<?php // new release")
            raise app.InstallerError("migration failed")
        self.ui.choices = [True]
        with patch.object(NativeBackend, "source"), patch.object(NativeBackend, "install", side_effect=fail_after_replacing_code), \
             patch.object(NativeBackend, "start"), patch.object(self.installer, "choose_release", return_value={"tag_name": NEW}), \
             self.assertRaisesRegex(app.InstallerError, "rolled back"):
            self.installer.update_panel()
        state = self.installer.panel_state()
        self.assertEqual((state["status"], state["version"], state["source_installed"]), ("ready", app.PANEL_VERSION, app.PANEL_VERSION))
        self.assertEqual((self.backend.app / "artisan").read_text(), "<?php // old release")
        env = (self.backend.app / ".env").read_text()
        self.assertIn('APP_KEY="original-key"', env)
        self.assertIn('DB_DATABASE="ptero_panel"', env)
        self.assertFalse(list(self.backend.app.parent.glob(".ptero-previous-*")))
        self.assertFalse(list(self.backend.app.parent.glob(".ptero-rollback-*")))
        executed = commands(self.runner)
        self.assertTrue(any("view:clear" in command for command in executed))
        self.assertEqual(self.runner.restored, self.runner.dump)


class DashboardTests(BaseTest):
    def test_friendly_status_labels_versions_and_attention_hints(self):
        from ptero.navigation import attention, status_label
        self.panel()
        state = inventory(self.installer)
        self.assertEqual(status_label("panel", state), "Configured")
        self.assertEqual(status_label("database", state), "Not configured")
        self.assertEqual(state["panel_version"], app.PANEL_VERSION)
        self.assertEqual(attention(state), "")
        for status, label in (("updating", "Update incomplete"), ("installing", "Installation incomplete"),
                              ("restoring", "Restore incomplete")):
            self.installer.save_panel({**self.state, "status": status})
            state = inventory(self.installer)
            self.assertEqual(status_label("panel", state), label)
            self.assertIn("Resume installation", attention(state))
        self.assertIn("roll back", attention(inventory(self.installer) | {"panel": "updating"}))

    def test_duration_formatting(self):
        from ptero.cli import duration
        self.assertEqual([duration(value) for value in (4.9, 75, 3725)], ["4s", "1m 15s", "1h 02m"])

    def test_mysql_reset_uses_mysql_client_variables(self):
        from ptero.rollback import database_reset
        self.assertIn('exec mysql --user="$MYSQL_USER"', database_reset({"database_engine": "mysql"}))
        self.assertIn('exec mariadb --user="$MARIADB_USER"', database_reset({}))


if __name__ == "__main__":
    unittest.main()
