"""Local retention of Panel archives and Wings configuration copies."""
import json
import os
from pathlib import Path
import unittest

from test_installer import BaseTest
from ptero.navigation import inventory, prerequisite
from ptero.retention import keep_count, prune


class RetentionTests(BaseTest):
    def archive(self, index, kind="panel"):
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        name = f"{kind}-20260101T0000{index:02d}Z-{index:06x}" + (".tar.gz" if kind.startswith("panel") else "")
        path = self.paths.backups / name
        if kind.startswith("panel"):
            path.write_text("archive")
            Path(str(path) + ".sha256").write_text("0" * 64 + "\n")
        else:
            path.mkdir()
            (path / "config.yml").write_text("config")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        return path

    def test_keeps_newest_copies_and_every_protected_one(self):
        archives = [self.archive(index) for index in range(8)]
        standalone = self.archive(20, "panel-standalone")
        wings = [self.archive(index, "wings") for index in range(4)]
        self.state["recovery_backup"] = str(archives[0])
        self.installer.save_panel(self.state)
        scheduled = archives[1]
        (self.paths.base / "backup-history.json").write_text(json.dumps({"runs": [[str(scheduled)]]}))
        manual = self.paths.backups / "my-own-copy.tar.gz"
        manual.write_text("user file")
        removed = prune(self.installer, keep=3)
        remaining = sorted(path.name for path in self.paths.backups.iterdir())
        for path in (archives[0], scheduled, standalone, *archives[6:], *wings[1:]):
            self.assertIn(path.name, remaining)
        for path in (*archives[2:6], wings[0]):
            self.assertNotIn(path.name, remaining)
            self.assertFalse(Path(str(path) + ".sha256").exists())
        self.assertTrue(manual.exists())
        self.assertEqual(removed, 5)

    def test_zero_keeps_everything_and_new_backups_trigger_pruning(self):
        for index in range(7):
            self.archive(index)
        self.assertEqual(prune(self.installer, keep=0), 0)
        self.assertEqual(keep_count(self.installer), 5)
        self.panel()
        self.installer.backup_panel()
        self.assertEqual(len(list(self.paths.backups.glob("panel-*.tar.gz"))), 5)

    def test_menu_action_saves_the_setting_without_prerequisites(self):
        for index in range(4):
            self.archive(index)
        self.assertEqual(prerequisite("backup-retention", inventory(self.installer)), "")
        self.ui.answers = ["2"]
        self.installer.backup_retention()
        self.assertEqual(keep_count(self.installer), 2)
        self.assertEqual(len(list(self.paths.backups.glob("panel-*.tar.gz"))), 2)

    def test_invalid_saved_setting_falls_back_to_the_default(self):
        self.paths.base.mkdir(parents=True, exist_ok=True)
        (self.paths.base / "settings.json").write_text(json.dumps({"schema": 1, "owner": "ptero-installer", "keep_local_backups": -4}))
        self.assertEqual(keep_count(self.installer), 5)


if __name__ == "__main__":
    unittest.main()
