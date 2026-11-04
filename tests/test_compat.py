"""Panel and Wings compatibility checks before install, update and rollback."""
import unittest
from unittest.mock import patch

from test_installer import BaseTest
import installer as app
from ptero.compat import check, local_versions, problem


class CompatibilityTests(BaseTest):
    def test_hard_rules(self):
        self.assertEqual(problem(app.PANEL_VERSION, app.WINGS_VERSION), "")
        self.assertIn("requires Wings v1.13.0", problem("v2.0.0-beta.3", "v1.12.9"))
        self.assertEqual(problem("v3.0.0", "v1.13.4"), "")  # Not a supported Panel version at all.
        self.assertEqual(problem("not-a-version", "v1.13.4"), "")

    def test_validated_pair_is_silent_and_untested_pair_needs_consent(self):
        self.installer.save_panel(self.state)
        self.wings()
        self.assertEqual(local_versions(self.installer), (app.PANEL_VERSION, app.WINGS_VERSION))
        check(self.installer)
        self.assertNotIn("Untested Panel and Wings combination", [m for m, _ in self.ui.messages])
        check(self.installer, wings="v1.13.5")
        self.assertIn("Untested Panel and Wings combination", [m for m, _ in self.ui.messages])
        self.ui.choices = [False]
        with self.assertRaises(app.Cancelled):
            check(self.installer, panel="v2.0.0-beta.3")

    def test_incompatible_wings_update_is_blocked_before_download(self):
        self.installer.save_panel(self.state)
        self.wings()
        with patch.object(self.installer, "choose_release", return_value={"tag_name": "v1.12.0"}), \
             patch.object(self.installer, "download_wings") as download:
            with self.assertRaisesRegex(app.InstallerError, "requires Wings"):
                self.installer.update_wings()
        download.assert_not_called()

    def test_missing_component_skips_the_check(self):
        check(self.installer, panel="v2.0.0-beta.3")
        self.assertEqual(self.ui.messages, [])


if __name__ == "__main__":
    unittest.main()
