"""Wings lifecycle: verified download, linking, TLS, backup, update and removal."""
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .common import (PROJECT, SCHEMA, WINGS_VERSION, InstallerError, NoRedirect, atomic_write, download,
                     node_configuration, valid_domain, valid_email, write_json)


class WingsMixin:
    def download_wings(self, release, target):
        arch = "amd64" if platform.machine() == "x86_64" else "arm64"
        name = "wings_linux_" + arch
        asset = next((a for a in release["assets"] if a["name"] == name), None)
        if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
            raise InstallerError("GitHub does not publish a SHA-256 for this binary. Download cancelled.")
        url = asset["browser_download_url"]
        expected = f"https://github.com/pterodactyl/wings/releases/download/{release['tag_name']}/{name}"
        if url != expected:
            raise InstallerError("The Wings download does not match the official repository.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            with self.ui.busy("Downloading and verifying Wings"):
                value = download(url, target, 256 * 1024**2)
            if "sha256:" + value != asset["digest"]:
                raise InstallerError("Wings SHA-256 mismatch. The file will not be executed.")
            target.chmod(0o755)
            self.run.run([target, "version"], label="Checking the Wings executable")
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def service_text(self):
        return ("[Unit]\nDescription=Pterodactyl Wings\nAfter=docker.service network-online.target\n"
                "Wants=network-online.target\nRequires=docker.service\nPartOf=docker.service\n"
                "StartLimitIntervalSec=180\nStartLimitBurst=30\n\n[Service]\nUser=root\n"
                f"WorkingDirectory={self.p.config_dir}\nLimitNOFILE=65536\n"
                f"ExecStart={self.p.wings} --config {self.p.config}\nRestart=on-failure\nRestartSec=5s\n"
                "\n[Install]\nWantedBy=multi-user.target\n")

    def install_wings(self):
        self.ui.step(1, 3, "Wings installation")
        self.check_host(wings=True)
        if self.p.wings_state.exists():
            state = self.load(self.p.wings_state)
            if state.get("method") == "docker":
                from .wings import DockerWings
                DockerWings(self, state).save()
                self.ui.say("Wings container already installed. Use configure or update.")
                return
            if self.p.wings.exists() and self.p.service.exists():
                self.ui.say(f"Wings is already managed ({state['status']}). Use configure or update.")
                return
            self.ui.say("Interrupted Wings installation: repairing its recorded version.", "warn")
            release = self.release("wings", state["version"])
        else:
            if any(path.exists() for path in (self.p.wings, self.p.config, self.p.service)):
                raise InstallerError("An unmanaged Wings installation exists. It will not be overwritten.")
            release = self.choose_release("wings", WINGS_VERSION)
        method = state.get("method", "standalone") if self.p.wings_state.exists() else self.ui.choice("Wings deployment", [
            ("standalone", "Standalone daemon (Recommended)", "A systemd service managing Docker game containers"),
            ("docker", "Docker container", "A Wings container with host networking and Docker socket access")])
        self.ui.confirm("INSTALL WINGS")
        self.ui.step(2, 3, "Deploying Wings")
        self.docker()
        if method == "docker":
            from .wings import DockerWings
            state = {"schema": SCHEMA, "owner": PROJECT, "method": "docker", "version": release["tag_name"],
                     "status": "pending", "image": self.pin_image("ghcr.io/pterodactyl/wings:" + release["tag_name"])}
            DockerWings(self, state).save()
            if self.ui.yes("Link Wings to the Panel now?", True):
                self.configure_wings()
            else:
                self.ui.say("Wings remains stopped until you complete Configure Wings.", "warn")
            return
        staged = self.p.wings.with_name(".wings-installer-new")
        self.download_wings(release, staged)
        self.p.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Record state before publishing so a later failure can be resumed or configured.
        write_json(self.p.wings_state, {"schema": SCHEMA, "owner": PROJECT, "version": release["tag_name"],
                                       "status": "pending"})
        os.replace(staged, self.p.wings)
        atomic_write(self.p.service, self.service_text(), 0o644)
        self.run.run(["systemctl", "daemon-reload"])
        self.ui.say("Wings binary installed and verified. Link the node to continue.", "ok")
        if self.ui.yes("Link Wings to the Panel now?", True):
            self.configure_wings()
        else:
            self.ui.say("Wings remains stopped until you complete Configure Wings.", "warn")

    def configure_wings(self):
        state = self.load(self.p.wings_state)
        if self.p.state.exists() and self.panel_state().get("status") == "ready":
            method = self.ui.choice("Node connection method", [("automatic", "Create node automatically (Recommended)", "Create location, node and allocations, then configure Wings"), ("manual", "Use an existing node token", "Link a node already created in the Panel")])
            if method == "automatic":
                return self.create_node()
        self.ui.step(3, 3, "Node connection")
        from .node_plan import connection
        known = self.panel_state() if self.p.state.exists() else None
        url, node, token = connection(self, known["url"] if known else None)
        if self.p.config.exists():
            self.ui.say("A fresh configuration will be generated. Reapply manual host settings from the backup if needed.", "warn")
            self.backup_wings()
        self.docker()
        # Same endpoint as `wings configure`; verified HTTPS and no token in argv.
        endpoint = url + f"/api/application/nodes/{node}/configuration"
        request = urllib.request.Request(endpoint, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.pterodactyl.v1+json",
            "User-Agent": "ptero-installer/" + __version__})
        try:
            with self.ui.busy("Fetching the node configuration"), urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise InstallerError("Node configuration is too large.")
            config = node_configuration(json.loads(body), url)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise InstallerError("Unable to fetch configuration. Check the Panel URL, node ID and Auto-Deploy token.") from exc
        self.run.secrets.update((config["token"], config["token_id"]))
        # JSON is a subset of YAML and Wings fills in the defaults.
        from .networking import wings_network
        wings_network(self, config)
        write_json(self.p.config, config)
        if state.get("method", "standalone") == "standalone":
            atomic_write(self.p.service, self.service_text(), 0o644)
            self.run.run(["systemctl", "daemon-reload"])
        state.update(status="configured", panel_url=url, node=node)
        write_json(self.p.wings_state, state)
        self.ui.say("If the Panel uses HTTPS, enable SSL on the node and issue its certificate before starting.")
        if self.ui.yes("Issue an HTTPS certificate for Wings now?"):
            self.wings_tls()
        self.start_wings()

    def start_wings(self):
        state = self.load(self.p.wings_state)
        if state.get("method") == "docker":
            from .wings import DockerWings
            return DockerWings(self, state).start()
        self.run.run(["systemctl", "enable", "wings"], label="Enabling Wings")
        self.run.run(["systemctl", "restart", "wings"], label="Starting Wings with its current configuration")
        # systemctl can report success before Wings has finished starting.
        with self.ui.busy("Checking initial Wings stability"):
            for _ in range(10):
                time.sleep(1)
                self.run.run(["systemctl", "is-active", "--quiet", "wings"])
        state = self.load(self.p.wings_state)
        state["status"] = "ready"
        write_json(self.p.wings_state, state)
        self.ui.say("Wings is active. Check that the node is connected in the Panel.", "ok")

    def wings_tls(self):
        self.load(self.p.wings_state)
        if self.p.state.exists() and self.panel_state().get("method") == "standalone":
            return self.native().wings_tls()
        domain = self.ui.ask("Wings domain, e.g. node.example.com", validator=valid_domain)
        email = self.ui.ask("Let's Encrypt email", validator=valid_email)
        self.ui.say("Enable SSL in the node's Configuration and use these paths:")
        self.ui.say(f"/etc/letsencrypt/live/{domain}/fullchain.pem\n/etc/letsencrypt/live/{domain}/privkey.pem")
        self.ui.say("The domain must point here and allow HTTP challenges on port 80.")
        panel = self.panel_state() if self.p.state.exists() else None
        if panel and panel["http_port"] == 80:
            self.ui.say("The Panel will pause briefly during certificate issuance/renewal to release port 80.", "warn")
        self.ui.confirm("ISSUE WINGS SSL")
        self.run.run(["apt-get", "update"], label="Updating APT")
        self.run.run(["apt-get", "install", "-y", "certbot"], label="Installing Certbot")
        pre = self.p.root / "usr/local/sbin/ptero-acme-pre"
        post = self.p.root / "usr/local/sbin/ptero-acme-post"
        flag = self.p.root / "run/ptero-acme-panel-running"
        command = f"docker compose --project-name {PROJECT} --file {shlex.quote(str(self.p.compose))}"
        atomic_write(pre, "#!/bin/sh\nset -eu\n" +
                     f"if [ -f {shlex.quote(str(self.p.compose))} ] && {command} ps --services --status running | grep -qx panel; then\n" +
                     f"  touch {shlex.quote(str(flag))}\n  {command} stop panel\nfi\n", 0o700)
        atomic_write(post, "#!/bin/sh\nset -eu\n" +
                     f"if [ -f {shlex.quote(str(flag))} ]; then\n  {command} up -d panel\n" +
                     f"  rm -f {shlex.quote(str(flag))}\nfi\n", 0o700)
        atomic_write(self.p.hook, self.wings_renew_hook(), 0o700)
        was_running = panel and panel["http_port"] == 80 and "panel" in self.running_services()
        try:
            if was_running:
                self.dc("stop", "panel", label="Releasing port 80 for the node certificate")
            self.port_free("0.0.0.0", 80)
            self.run.run(["certbot", "certonly", "--standalone", "--non-interactive", "--agree-tos",
                          "--email", email, "-d", domain, "--pre-hook", pre, "--post-hook", post],
                         label="Issuing the Wings certificate")
        finally:
            if was_running:
                self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel")
        self.run.run(["systemctl", "enable", "--now", "certbot.timer"])
        self.ui.say("Certificate issued. Synchronize Wings again if you changed SSL in the Panel.", "ok")

    def wings_renew_hook(self):
        from .wings import renewal_hook
        return renewal_hook(self)

    def backup_wings(self):
        self.load(self.p.wings_state)
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.p.backups / ("wings-" + self.stamp())
        target.mkdir(mode=0o700)
        for path in (self.p.wings_state, self.p.config, self.p.service, self.p.base / "wings-compose.json"):
            if path.exists():
                shutil.copy2(path, target / path.name)
                (target / path.name).chmod(0o600)
        self.ui.say(f"Wings configuration saved: {target}", "ok")
        return target

    def update_wings(self):
        state = self.load(self.p.wings_state)
        release = self.choose_release("wings", "auto")
        if release["tag_name"] == state["version"]:
            self.ui.say("This version is already installed.", "ok")
            return
        if state.get("method") == "docker":
            from .wings import DockerWings
            return DockerWings(self, state).update(release)
        self.ui.confirm("UPDATE WINGS")
        staged = self.p.wings.with_name(".wings-installer-new")
        old = self.p.wings.with_name(".wings-installer-old")
        self.download_wings(release, staged)
        backup = self.backup_wings()
        active = self.run.run(["systemctl", "is-active", "--quiet", "wings"], check=False)[0] == 0
        shutil.copy2(self.p.wings, old)
        try:
            if active:
                self.run.run(["systemctl", "stop", "wings"])
            os.replace(staged, self.p.wings)
            if active:
                self.start_wings()
            state["version"] = release["tag_name"]
            write_json(self.p.wings_state, state)
        except BaseException:
            os.replace(old, self.p.wings)
            if (backup / "config.yml").exists():
                shutil.copy2(backup / "config.yml", self.p.config)
            write_json(self.p.wings_state, state)
            if active:
                self.run.run(["systemctl", "restart", "wings"], check=False)
            raise
        finally:
            old.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
        self.ui.say("Wings updated. Game containers are preserved.", "ok")

    def uninstall_wings(self, confirmed=False):
        state = self.load(self.p.wings_state)
        if state.get("method") == "docker":
            from .wings import DockerWings
            return DockerWings(self, state).uninstall(confirmed)
        if not confirmed:
            self.ui.confirm("UNINSTALL WINGS")
        self.ui.say("Game files and containers will remain. Stop them from the Panel if needed.", "warn")
        self.backup_wings()
        self.run.run(["systemctl", "disable", "--now", "wings"], label="Stopping and disabling Wings")
        # ACME hooks are still needed to renew the retained certificates.
        for path in (self.p.wings, self.p.service, self.p.config, self.p.wings_state):
            path.unlink(missing_ok=True)
        self.run.run(["systemctl", "daemon-reload"])
        self.ui.say("Wings removed. Game files, Docker and certificates are preserved.", "ok")
