"""Optional Wings container backend. Game containers always remain separate."""

import shlex

from .core import InstallerError, write_json


class DockerWings:
    project = "ptero-wings"

    def __init__(self, installer, state):
        self.i, self.p, self.s = installer, installer.p, state
        self.compose = self.p.base / "wings-compose.json"

    def save(self):
        write_json(self.p.wings_state, self.s)
        paths = [
            (self.p.config_dir, "/etc/pterodactyl", True),
            (self.p.root / "var/run/docker.sock", "/var/run/docker.sock", False),
            (self.p.root / "var/lib/pterodactyl", "/var/lib/pterodactyl", False),
            (self.p.root / "var/log/pterodactyl", "/var/log/pterodactyl", False),
            (self.p.root / "tmp/pterodactyl", "/tmp/pterodactyl", False),
            (self.p.root / "run/wings", "/run/wings", False),
            (self.p.root / "etc/letsencrypt", "/etc/letsencrypt", True),
        ]
        volumes = []
        for source, target, readonly in paths:
            if target != "/var/run/docker.sock":
                source.mkdir(parents=True, exist_ok=True, mode=0o755)
            volumes.append({"type": "bind", "source": str(source), "target": target, "read_only": readonly})
        # Wings normalizes/writes config.yml during startup, so its own config is writable.
        volumes[0]["read_only"] = False
        write_json(
            self.compose,
            {
                "name": self.project,
                "services": {
                    "wings": {
                        "image": self.s["image"],
                        "restart": "unless-stopped",
                        "network_mode": "host",
                        "volumes": volumes,
                        "stop_grace_period": "30s",
                        "healthcheck": {"test": ["CMD", "/usr/bin/wings", "version"], "interval": "10s", "timeout": "5s", "retries": 5},
                        "ulimits": {"nofile": {"soft": 65536, "hard": 65536}},
                    }
                },
            },
        )

    def dc(self, *args, **kwargs):
        return self.i.run.run(["docker", "compose", "--project-name", self.project, "--file", self.compose, *args], **kwargs)

    def start(self):
        if not self.p.config.is_file():
            raise InstallerError("Configure the Wings node before starting its container.")
        self.save()
        self.dc("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120", "wings", label="Starting the Wings container")
        _, text = self.dc("ps", "-q", "wings")
        container = text.strip()
        if not container:
            raise InstallerError("The Wings container was not created.")
        import time

        with self.i.ui.busy("Checking Wings startup stability"):
            for _ in range(10):
                time.sleep(1)
                _, status = self.i.run.run(["docker", "inspect", "--format", "{{.State.Running}} {{.RestartCount}}", container])
                if status.strip() != "true 0":
                    raise InstallerError("Wings failed to start cleanly. Check the container logs.")
        self.s["status"] = "ready"
        self.save()

    def health(self):
        _, text = self.dc("ps", "-q", "wings")
        if not text.strip():
            raise InstallerError("The Wings container is stopped or missing.")
        _, status = self.i.run.run(["docker", "inspect", "--format", "{{.State.Running}} {{.State.Health.Status}}", text.strip()])
        if status.strip() != "true healthy":
            raise InstallerError("The Wings container is stopped or unhealthy.")

    def update(self, release):
        self.i.ui.confirm("UPDATE WINGS")
        image = self.i.pin_image("ghcr.io/pterodactyl/wings:" + release["tag_name"])
        old = dict(self.s)
        backup = self.i.backup_wings()
        active = self.s["status"] == "ready"
        self.s.update(image=image, version=release["tag_name"])
        self.save()
        try:
            if active:
                self.start()
        except BaseException:
            import shutil

            if (backup / "config.yml").exists():
                shutil.copy2(backup / "config.yml", self.p.config)
            self.s = old
            self.save()
            if active:
                self.dc("up", "-d", "wings", check=False)
            raise

    def uninstall(self, confirmed=False):
        if not confirmed:
            self.i.ui.confirm("UNINSTALL WINGS")
        self.i.backup_wings()
        self.dc("down", label="Removing only the Wings service container")
        self.compose.unlink(missing_ok=True)
        self.p.config.unlink(missing_ok=True)
        self.p.wings_state.unlink(missing_ok=True)
        self.i.ui.say("Wings removed. Game containers, game files and Docker are preserved.", "ok")


def renewal_hook(installer):
    compose = installer.p.base / "wings-compose.json"
    return (
        "#!/bin/sh\nset -eu\n"
        f"if [ -f {shlex.quote(str(compose))} ]; then\n"
        f"  docker compose --project-name ptero-wings --file {shlex.quote(str(compose))} restart wings\n"
        "elif systemctl cat wings.service >/dev/null 2>&1; then\n  systemctl try-restart wings.service\nfi\n"
    )
