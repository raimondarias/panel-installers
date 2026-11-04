"""Log rotation, inherited-credential filtering and bounded downloads."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_installer import QuietUI
import installer as app
from ptero.common import download, file_sha256, write_checksum
from ptero.runtime import LOG_GENERATIONS, Runner, child_environment


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_log_rotates_and_keeps_a_bounded_number_of_generations(self):
        log = self.root / "installer.log"
        runner = Runner(QuietUI(), log)
        with patch("ptero.runtime.LOG_LIMIT", 64):
            for index in range(12):
                runner.record(f"entry {index} " + "x" * 60)
        generations = sorted(path.name for path in self.root.iterdir())
        self.assertEqual(generations, ["installer.log"] + [f"installer.log.{n}" for n in range(1, LOG_GENERATIONS + 1)])
        self.assertIn("entry 11", log.read_text())
        self.assertTrue(all((self.root / name).stat().st_mode & 0o777 == 0o600 for name in generations))

    def test_private_credentials_are_not_inherited_by_commands(self):
        secrets = {"PTERO_GITHUB_TOKEN": "ghp-secret", "AWS_SECRET_ACCESS_KEY": "aws-secret", "RESTIC_PASSWORD": "restic"}
        with patch.dict(os.environ, {**secrets, "PATH": "/usr/bin"}):
            environment = child_environment({"RESTIC_PASSWORD_FILE": "/explicit"})
        self.assertFalse(set(secrets) & set(environment))
        self.assertEqual(environment["RESTIC_PASSWORD_FILE"], "/explicit")
        self.assertEqual(environment["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(environment["PATH"], "/usr/bin")

    def test_download_enforces_its_size_limit(self):
        target = self.root / "binary"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"a" * 2048)):
            with self.assertRaisesRegex(app.InstallerError, "size limit"):
                download("https://example.test/binary", target, 1024)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"abc")):
            value = download("https://example.test/binary", target, 1024)
        self.assertEqual(value, file_sha256(target))
        self.assertEqual(write_checksum(target), value)
        self.assertEqual(Path(str(target) + ".sha256").read_text(), value + "\n")


if __name__ == "__main__":
    unittest.main()
