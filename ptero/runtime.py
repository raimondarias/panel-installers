"""Command execution with secret redaction, and managed filesystem locations."""

import contextlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .common import InstallerError

# Credentials that callers pass explicitly (``env=``) must never leak by
# inheritance into unrelated commands such as apt-get, docker or ssh.
PRIVATE_ENVIRONMENT = frozenset(
    (
        "PTERO_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "RESTIC_PASSWORD",
        "RESTIC_PASSWORD_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    )
)
LOG_LIMIT = 5 * 1024 * 1024
LOG_GENERATIONS = 3


def child_environment(extra=None):
    inherited = {key: value for key, value in os.environ.items() if key not in PRIVATE_ENVIRONMENT}
    return {**inherited, "DEBIAN_FRONTEND": "noninteractive", **(extra or {})}


class Runner:
    def __init__(self, ui, log):
        self.ui, self.log = ui, Path(log)
        self.secrets = set()

    def redact(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)((?:password|token(?:_id)?|app_key|hashids_salt)[\"']?\s*[:=]\s*)[^\s,]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", text)
        text = re.sub(r"ptl[a-z]_[A-Za-z0-9_-]+", "[REDACTED TOKEN]", text)
        return text

    def rotate(self):
        """Keep the private log bounded: installer.log plus three numbered generations."""
        try:
            if self.log.stat().st_size < LOG_LIMIT:
                return
        except FileNotFoundError:
            return
        for number in range(LOG_GENERATIONS - 1, 0, -1):
            older = self.log.with_name(f"{self.log.name}.{number}")
            if older.exists():
                os.replace(older, self.log.with_name(f"{self.log.name}.{number + 1}"))
        os.replace(self.log, self.log.with_name(self.log.name + ".1"))

    def record(self, text):
        self.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.rotate()
        with self.log.open("a", encoding="utf-8") as stream:
            self.log.chmod(0o600)
            stream.write(self.redact(text) + "\n")

    def run(
        self, args, label=None, interactive=False, output=None, input_file=None, check=True, timeout=1800, env=None, umask=0o077, cwd=None
    ):
        args = [str(arg) for arg in args]
        self.record("$ " + " ".join(args))
        # Temporary files avoid holding SQL dumps and apt output in memory.
        with tempfile.TemporaryFile() as captured, tempfile.TemporaryFile() as errors:
            with (
                self.ui.busy(label) if label and not interactive else contextlib.nullcontext(),
                self.ui.suspend() if interactive else contextlib.nullcontext(),
            ):
                try:
                    result = subprocess.run(
                        args,
                        stdin=input_file,
                        stdout=None if interactive else (output or captured),
                        stderr=None if interactive else errors,
                        check=False,
                        timeout=None if interactive else timeout,
                        env=child_environment(env),
                        umask=umask,
                        cwd=cwd,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise InstallerError(f"Unable to execute {args[0]}: {exc}") from exc
            captured.seek(0)
            errors.seek(0)
            text = captured.read().decode("utf-8", "replace")
            error = errors.read().decode("utf-8", "replace")
            self.record(text + error)
            if result.returncode and check:
                detail = self.redact((error + "\n" + text)[-4800:])
                raise InstallerError(f"Failed: {label or args[0]} (exit code {result.returncode}).\n{detail}")
            return result.returncode, text


class Paths:
    def __init__(self, root=Path("/")):
        self.root = Path(root)
        self.base = self.root / "opt/pterodactyl-installer"
        self.panel = self.base / "panel"
        self.data = self.panel / "data"
        self.state = self.panel / "state.json"
        self.compose = self.panel / "compose.json"
        self.backups = self.root / "var/backups/pterodactyl-installer"
        self.log = self.root / "var/log/pterodactyl-installer/installer.log"
        self.wings = self.root / "usr/local/bin/wings"
        self.wings_state = self.base / "wings.json"
        self.config_dir = self.root / "etc/pterodactyl"
        self.config = self.config_dir / "config.yml"
        self.service = self.root / "etc/systemd/system/wings.service"
        self.hook = self.root / "etc/letsencrypt/renewal-hooks/deploy/ptero-wings.sh"
