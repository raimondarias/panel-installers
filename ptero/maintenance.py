"""Cross-component maintenance, diagnostics and the action table."""
import json
import platform
import shutil

from .common import InstallerError


class MaintenanceMixin:
    def install_both(self):
        self.install_panel()
        self.install_wings()

    def uninstall_both(self):
        self.ui.confirm("UNINSTALL PANEL AND WINGS")
        # Keep the Panel reachable if the Wings backup or removal fails.
        if self.p.wings_state.exists():
            self.uninstall_wings(confirmed=True)
        if self.p.state.exists():
            self.uninstall_panel(confirmed=True)

    def health_report(self):
        from .health import inspect
        return inspect(self)

    def create_node(self):
        from .nodes import setup
        return setup(self)

    def backup_schedule(self):
        from .backups import ScheduledBackups
        return ScheduledBackups(self).configure()

    def backup_run(self):
        from .backups import ScheduledBackups
        return ScheduledBackups(self).run_now()

    def backup_download(self):
        from .backups import ScheduledBackups
        return ScheduledBackups(self).download()

    def remote_node(self):
        from .remote import setup_node
        return setup_node(self)

    def remote_database(self):
        from .remote import setup_database
        return setup_database(self)

    def resume(self):
        if self.p.state.exists() and self.panel_state()["status"] != "ready":
            state = self.panel_state()
            from .rollback import available
            if state["status"] == "updating" and available(state):
                previous = state.get("previous_version") or self.ui.tr("the previous version")
                choice = self.ui.choice("The last Panel update did not finish", [
                    ("retry", self.ui.tr("Continue the update to {version}").format(version=state["version"]),
                     "Run the remaining update phases again"),
                    ("rollback", self.ui.tr("Roll back to {version}").format(version=previous),
                     "Restore the verified pre-update backup and previous release")])
                if choice == "rollback":
                    return self.rollback_panel(after_failure=True)
            return self.install_panel()
        from .game_database import GameDatabase
        database = GameDatabase(self)
        if database.state_file.exists():
            saved = database.load()
            if saved.get("status") != "ready":
                self.ui.confirm("RESUME PANEL")
                database.local(saved)
                return
        if self.p.wings_state.exists() and self.load(self.p.wings_state)["status"] != "ready":
            return self.configure_wings()
        self.ui.say("No interrupted managed installation needs resuming.")

    def diagnostics(self):
        from .navigation import inventory, _state_problem
        installed = inventory(self)
        if installed['panel'] == installed['wings'] == 'absent':
            self.ui.card("Ready for your first installation", [
                "No managed Panel or Wings is installed on this machine yet.",
                "Next: choose Install Panel and Wings, or install the Panel first."])
        self.ui.card("Machine overview", [
            self.ui.tr("Architecture: {value}").format(value=platform.machine()),
            self.ui.tr("Free disk space: {value} GiB").format(value=round(shutil.disk_usage(self.p.root).free / 1024 ** 3, 1)),
            self.ui.tr("System manager: {value}").format(value="systemd" if (self.p.root / "run/systemd/system").is_dir() else self.ui.tr("Not detected")),
            "Diagnostics do not install software or change firewall rules."])
        for component in ('Panel', 'Wings'):
            status = installed[component.lower()]
            if status == 'absent':
                continue
            problem = _state_problem(component, status)
            if problem:
                self.ui.say(problem, 'error')
                continue
            try:
                if component == 'Panel':
                    if self.panel_state().get('method') == 'standalone':
                        self.native().health()
                        self.ui.say("Standalone services and HTTP endpoint passed their checks.", 'ok')
                    else:
                        self.run.run(['docker', 'info', '--format', '{{.ServerVersion}}'])
                        _, text = self.dc('ps', '--format', 'json')
                        found = set()
                        for line in text.splitlines():
                            data = json.loads(line)
                            for service in (data if isinstance(data, list) else [data]):
                                found.add(service.get('Service'))
                                if service.get('State') != 'running' or service.get('Health') != 'healthy':
                                    raise InstallerError('A Panel container is stopped or unhealthy.')
                        if not {'database', 'cache', 'panel'}.issubset(found):
                            raise InstallerError('Panel containers are missing.')
                        self.ui.say('All Panel containers are healthy.', 'ok')
                else:
                    state = self.load(self.p.wings_state)
                    if state.get('method') == 'docker':
                        from .wings import DockerWings
                        backend = DockerWings(self, state)
                        backend.health()
                        _, text = backend.dc('ps', '--all')
                        self.ui.say(text)
                    else:
                        self.run.run(['systemctl', 'is-active', '--quiet', 'wings'])
                        self.ui.say('Wings is active.', 'ok')
            except (InstallerError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.ui.say(self.ui.tr('Needs attention: {component} — {detail}').format(
                    component=component, detail=self.run.redact(str(exc))), 'error')
        if installed['panel'] != 'absent' or installed['wings'] != 'absent':
            self.ui.say(f"Private log: {self.p.log}")

    def logs(self):
        if self.p.state.exists():
            state = self.panel_state()
            if state.get("method") == "standalone":
                _, text = self.run.run(["journalctl", "-u", "ptero-panel-queue", "-u", "ptero-panel-scheduler", "-n", "80", "--no-pager"])
            else:
                _, text = self.dc("logs", "--no-color", "--tail", "80")
            self.ui.say(self.run.redact(text))
        if self.p.wings_state.exists():
            state = self.load(self.p.wings_state)
            if state.get("method") == "docker":
                from .wings import DockerWings
                _, text = DockerWings(self, state).dc("logs", "--no-color", "--tail", "80")
            else:
                _, text = self.run.run(["journalctl", "-u", "wings", "-n", "80", "--no-pager"])
            self.ui.say(self.run.redact(text))

    def restart(self):
        self.ui.confirm("RESTART SERVICES")
        if self.p.state.exists():
            state = self.panel_state()
            if state["status"] != "ready":
                raise InstallerError("The Panel is not ready. Resume its installation/update first.")
            if state.get("method") == "standalone":
                backend = self.native(state)
                self.run.run(["systemctl", "restart", backend.queue, backend.scheduler])
                backend.health()
            else:
                self.dc("restart", "panel", label="Restarting the Panel")
                self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel")
        if self.p.wings_state.exists():
            self.start_wings()

    def mail(self):
        state = self.panel_state()
        self.smtp_settings(state)
        self.ui.confirm("CHANGE SMTP")
        self.save_panel(state)
        if state.get("method") == "standalone":
            backend = self.native(state)
            backend.application_environment()
            backend.artisan("config:clear")
            self.run.run(["systemctl", "restart", backend.queue])
            backend.health()
        else:
            self.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Applying SMTP settings")

    def admin(self):
        state = self.panel_state()
        from .wizard import administrator
        state["admin"] = administrator(self)
        self.ui.confirm("CREATE ADMINISTRATOR")
        self.create_admin(state)
        if self.generated_admin_password:
            self.ui.card("Save your generated password", [self.admin_password])

    def firewall(self):
        self.ui.say("Allow these ports in your provider and host firewalls:")
        self.ui.say("Panel: TCP 80/443. Wings: TCP 8080 (or your node's API port) and TCP 2022 for SFTP.")
        self.ui.say("Games: their TCP/UDP allocations. Keep your SSH administration port open.")
        self.ui.say("Docker publishes ports using its own rules; restrict them with DOCKER-USER/nftables.")
        self.ui.say("Firewalls and their rules are not changed automatically.")

    def actions(self):
        return {"install-all": self.install_both, "install-panel": self.install_panel,
                "install-wings": self.install_wings, "uninstall-all": self.uninstall_both,
                "uninstall-panel": self.uninstall_panel, "uninstall-wings": self.uninstall_wings,
                "update-panel": self.update_panel, "update-wings": self.update_wings,
                "rollback-panel": self.rollback_panel,
                "configure-wings": self.configure_wings, "tls-wings": self.wings_tls,
                "backup-panel": self.backup_panel, "backup-wings": self.backup_wings,
                "restore-panel": self.restore_panel, "diagnose": self.diagnostics,
                "restart": self.restart, "logs": self.logs, "smtp": self.mail,
                "admin": self.admin, "firewall": self.firewall,
                "configure-database": self.configure_game_database,
                "database-credentials": self.game_database_credentials,
                "create-node": self.create_node,
                "backup-schedule": self.backup_schedule, "backup-run": self.backup_run,
                "backup-download": self.backup_download, "health": self.health_report,
                "resume": self.resume, "remote-node": self.remote_node, "remote-database": self.remote_database}

    def menu(self):
        from .cli import menu
        return menu(self)
