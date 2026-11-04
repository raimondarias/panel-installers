"""In-memory simulator: no Runner, paths, subprocesses or service backends."""
import copy
import sys
import time
from types import SimpleNamespace

from .core import Cancelled, Installer, InstallerError, PANEL_VERSION, WINGS_VERSION
from .navigation import menu_items, prerequisite, prepare_action
from .ui import Literal
from .wizard import administrator, panel_plan

NOTICE = "Preview only. No packages, files or services will be installed."


class Simulation:
    # These production helpers only collect/validate answers.
    valid_port = staticmethod(Installer.valid_port)
    is_ipv4 = staticmethod(Installer.is_ipv4)
    smtp_settings = Installer.smtp_settings
    choose_release = Installer.choose_release

    def __init__(self, ui):
        self.ui = ui
        self.run = SimpleNamespace(secrets=set())  # Cannot execute commands.
        self.panel = self.wings = self.database = None
        self.backups = {}
        self.generated_admin_password = False
        self.backup_profile = None
        self.created_node = None

    def panel_state(self):
        return self.panel

    def release(self, component, version):
        if version == "auto":
            version = PANEL_VERSION if component == "panel" else WINGS_VERSION
        return {"tag_name": version, "prerelease": any(s in version for s in ("beta", "alpha", "rc"))}

    def fetch(self, _url, limit=256):
        self.ui.say("Simulation uses a sample public IP; no address is detected.", "muted")
        return b"93.184.216.34"

    def existing_game_database(self):
        return self.database

    def check_plan(self, _state):
        self.stages("Checking your installation plan")

    def stages(self, *labels):
        for number, label in enumerate(labels, 1):
            text = self.ui.tr("Simulation: {value}").format(value=self.ui.tr(label))
            self.ui.say(f"[{number}/{len(labels)}] {text}")
            with self.ui.busy(text):
                if self.ui.animate:
                    time.sleep(0.7)

    def inventory(self):
        return {"panel": "ready" if self.panel else "absent",
                "wings": self.wings["status"] if self.wings else "absent",
                "database": "ready" if self.database else "absent",
                "credentials": bool(self.panel and self.panel.get("game_database", {}).get("registered")),
                "backup_profile": "ready" if self.backup_profile else "absent",
                "interrupted": bool(self.wings and self.wings.get("status") != "ready"),
                "url": self.panel["url"] if self.panel else "",
                "rollback": bool(self.panel and self.panel.get("previous_version")),
                **({"panel_version": self.panel["version"]} if self.panel else {}),
                **({"wings_version": self.wings["version"]} if self.wings and self.wings.get("version") else {})}

    def install_panel(self):
        if self.panel:
            self.ui.say("The simulated Panel already exists. Use maintenance or reset the preview.")
            return
        state = panel_plan(self)
        self.stages("Preparing dependencies", "Preparing the database and cache", "Deploying Panel files",
                    "Running migrations and creating the administrator", "Configuring the web server and scheduler",
                    "Checking simulated services")
        state["status"] = "ready"
        self.panel = state
        self.register_database(state["game_database"])

    def register_database(self, settings):
        if settings["mode"] == "later":
            return
        self.stages("Preparing the game database host", "Registering the database host in the Panel")
        if settings["mode"] == "local":
            settings["host"] = "172.30.0.1"
            self.database = copy.deepcopy(settings)
        settings["registered"] = True
        self.credentials()

    def credentials(self):
        settings = self.panel["game_database"]
        self.ui.card("Game database host is ready", [NOTICE, Literal(f"{settings['host']}:{settings['port']}"),
            Literal(settings["username"]), Literal(settings["password"]), "Simulation credentials disappear when you exit."])

    def install_wings(self):
        if self.wings:
            self.ui.say("The simulated Wings already exists. Use maintenance or reset the preview.")
            return
        self.ui.step(1, 3, "Wings installation")
        release = self.choose_release("wings", WINGS_VERSION)
        method = self.ui.choice("Wings deployment", [
            ("standalone", "Standalone daemon (Recommended)", "A systemd service managing Docker game containers"),
            ("docker", "Docker container", "A Wings container with host networking and Docker socket access")])
        self.ui.confirm("INSTALL WINGS")
        self.ui.step(2, 3, "Deploying Wings")
        self.stages("Preparing dependencies", "Deploying Wings", "Checking simulated services")
        self.wings = {"method": method, "version": release["tag_name"], "status": "pending"}
        if self.ui.yes("Link Wings to the Panel now?", True):
            self.link_wings()

    def link_wings(self):
        from .node_plan import connection
        self.ui.step(3, 3, "Node connection")
        if self.panel:
            method = self.ui.choice("Node connection method", [
                ("automatic", "Create node automatically (Recommended)", "Create location, node and allocations, then configure Wings"),
                ("manual", "Use an existing node token", "Link a node already created in the Panel")])
            if method == "automatic":
                from .nodes import plan
                self.created_node = plan(self)
                self.ui.confirm("CREATE NODE")
                self.stages("Creating node and allocations", "Checking simulated services")
                self.wings.update(status="ready", panel_url=self.panel["url"], node="1")
                return
        url, node, _token = connection(self, self.panel["url"] if self.panel else None)
        self.stages("Fetching simulated node configuration")
        if self.ui.yes("Issue an HTTPS certificate for Wings now?"):
            self.tls_wings()
        self.stages("Checking simulated services")
        self.wings.update(status="ready", panel_url=url, node=node)

    def tls_wings(self):
        from .core import valid_domain, valid_email
        self.ui.ask("Wings domain, e.g. node.example.com", validator=valid_domain)
        self.ui.ask("Let's Encrypt email", validator=valid_email)
        self.ui.confirm("ISSUE WINGS SSL")
        self.stages("Simulating certificate issuance")

    def dispatch(self, action):
        reason = prerequisite(action, self.inventory())
        if reason:
            raise InstallerError(reason)
        if action == "create-node":
            from .nodes import plan
            data = plan(self)
            self.ui.confirm("CREATE NODE")
            self.stages("Creating node and allocations")
            self.created_node = data
        elif action in ("remote-node", "remote-database"):
            from .remote import target_plan, database_plan
            target_plan(self, preview=True)
            if action == "remote-node":
                from .nodes import plan
                self.created_node = plan(self)
            else:
                database_plan(self)
            self.ui.confirm("INSTALL REMOTE NODE" if action == "remote-node" else "INSTALL REMOTE DATABASE")
            self.stages("Checking simulated SSH connection", "Provisioning the simulated remote machine")
        elif action == "health":
            self.ui.card("Health report", [NOTICE, "All statuses and checks in this session are simulated."])
        elif action == "resume":
            self.stages("Checking completed installation phases")
            if self.wings and self.wings.get("status") != "ready":
                self.link_wings()
        elif action == "backup-schedule":
            from .backups import backup_plan
            profile, password = backup_plan(self.ui)
            self.run.secrets.add(password)
            self.ui.confirm("CONFIGURE BACKUPS")
            self.backup_profile = profile
            self.stages("Saving the simulated backup schedule")
        elif action in ("backup-run", "backup-download"):
            if not self.backup_profile:
                raise InstallerError("Configure scheduled backups first.")
            self.stages("Creating a simulated backup", "Verifying the simulated backup")
        elif action == "install-all":
            self.install_panel()
            self.install_wings()
        elif action == "install-panel":
            self.install_panel()
        elif action == "install-wings":
            self.install_wings()
        elif action == "configure-wings":
            self.link_wings()
        elif action == "tls-wings":
            self.tls_wings()
        elif action == "configure-database":
            from .game_database import plan
            settings = plan(self, self.panel, advanced=True)
            if settings["mode"] != "later":
                self.ui.confirm("INSTALL PANEL")
                self.panel["game_database"] = settings
                self.register_database(settings)
        elif action == "database-credentials":
            self.credentials()
        elif action == "smtp":
            self.smtp_settings(self.panel)
            self.ui.confirm("INSTALL PANEL")
            self.stages("Saving simulated SMTP settings")
        elif action == "admin":
            administrator(self)
            self.ui.confirm("INSTALL PANEL")
            self.stages("Running migrations and creating the administrator")
            if self.generated_admin_password:
                self.ui.card("Save your generated password", [Literal(self.admin_password),
                    "Simulation credentials disappear when you exit."])
        elif action in ("backup-panel", "backup-wings"):
            key = action.removeprefix("backup-")
            self.stages("Creating a simulated backup", "Verifying the simulated backup")
            self.backups[key] = copy.deepcopy(self.panel if key == "panel" else self.wings)
            self.ui.say("Backup stored in preview memory only.")
        elif action == "restore-panel":
            if "panel" not in self.backups:
                raise InstallerError("Create a simulated Panel backup first.")
            if self.panel:
                raise InstallerError("Remove the simulated Panel before restoring into an empty destination.")
            self.ui.confirm("RESTORE PREVIEW")
            self.stages("Verifying the simulated backup", "Restoring the simulated Panel")
            self.panel = copy.deepcopy(self.backups["panel"])
        elif action.startswith("uninstall-"):
            self.ui.confirm("REMOVE PREVIEW")
            self.stages("Removing simulated components")
            if action in ("uninstall-all", "uninstall-panel"):
                self.panel = None
            if action in ("uninstall-all", "uninstall-wings"):
                self.wings = None
        elif action in ("update-panel", "update-wings"):
            component = action.removeprefix("update-")
            state = self.panel if component == "panel" else self.wings
            release = self.choose_release(component, state["version"])
            if component == "panel":
                self.ui.card("Panel update plan", [
                    self.ui.tr("Update: {current} → {target}").format(current=state["version"], target=release["tag_name"]),
                    "A verified backup is created before any code or database change.",
                    self.ui.tr("If anything fails, you can roll back to {version}.").format(version=state["version"])])
            self.ui.confirm("UPDATE PREVIEW")
            self.stages("Creating a simulated backup", "Deploying the simulated update", "Checking simulated services")
            if component == "panel":
                state["previous_version"] = state["version"]
            state["version"] = release["tag_name"]
        elif action == "rollback-panel":
            previous = self.panel.pop("previous_version")
            self.ui.card("Panel rollback plan", [
                self.ui.tr("Installed: {value}").format(value=self.panel["version"]),
                self.ui.tr("Roll back to: {value}").format(value=previous),
                "Panel changes made after this backup will be lost.",
                "A safety copy of the current Panel is created first."])
            self.ui.confirm("ROLL BACK PANEL")
            self.stages("Creating a simulated backup", "Restoring the pre-update database", "Starting the previous Panel version")
            self.panel["version"] = previous
        elif action == "restart":
            self.ui.confirm("RESTART PREVIEW")
            self.stages("Restarting simulated services", "Checking simulated services")
        elif action in ("diagnose", "logs", "firewall"):
            self.ui.card("Interface preview", [NOTICE, "All statuses and checks in this session are simulated.",
                self.ui.tr("Panel URL: {value}").format(value=self.panel["url"] if self.panel else "—"),
                "Panel: TCP 80/443 · Wings: TCP 8080/2022 · Games: per allocation"])
        else:
            raise InstallerError("Unknown preview action: " + action)


class TourUI:
    """Exercise the real form for redirected output, without input/getpass."""
    def __init__(self, ui):
        self.base = ui

    def __getattr__(self, name):
        return getattr(self.base, name)

    def choice(self, title, options, default=0):
        preferred = {"Setup experience": True, "Where are the game databases?": "local", "Node connection method": "manual"}
        value = preferred.get(title, options[default][0])
        self.base.card(title, [self.tr(next(item[1] for item in options if item[0] == value))])
        return value

    def ask(self, label, default=None, validator=None, secret=False):
        values = {"Domain / subdomain (without http://)": "panel.example.com",
                  "Administrator email": "admin@example.com", "Administrator username": "admin",
                  "Administrator password (12+ characters; empty generates one)": "Preview-only-password-123!",
                  "Confirm administrator password": "Preview-only-password-123!",
                  "SMTP host": "smtp.example.com", "SMTP username": "admin@example.com",
                  "SMTP password": "preview-smtp-password", "Panel URL": "https://panel.example.com",
                  "Numeric node ID": "1", "Auto-Deploy token": "preview-token",
                  "Wings domain, e.g. node.example.com": "node.example.com",
                  "Let's Encrypt email": "admin@example.com",
                  "Database host password (16+ characters; empty generates one)": "",
                  "Node hostname / IPv4": "node.example.com", "Allocation IPv4 address": "93.184.216.34",
                  "Backup encryption password (16+ characters)": "preview-backup-password-123!",
                  "Confirm backup encryption password": "preview-backup-password-123!",
                  "Restic repository (local path, sftp: or s3:)": "s3:https://storage.example.com/ptero",
                  "S3 access key": "preview-access-key", "S3 secret key": "preview-secret-key",
                  "Remote SSH hostname / IPv4": "node.example.com", "Remote SQL private IPv4": "10.20.0.2",
                  "Allowed private source IPv4 addresses (comma separated)": "10.20.0.1"}
        value = values.get(label, default)
        if value is None or (validator and not validator(value)):
            raise InstallerError("No valid tour answer: " + label)
        self.base.say(self.tr(label) + ": " + ("••••••••" if secret else str(value)))
        return value

    def yes(self, label, default=False):
        self.base.say(self.tr(label) + " · " + self.tr("Yes"))
        return True

    def confirm(self, phrase):
        self.base.say(self.tr(phrase))


def show(ui, action=None):
    """Interactive on a terminal; automatic full tour for redirected input."""
    ui.preview = True
    simulation = Simulation(ui)
    if action:
        simulation.dispatch(action)
        if ui.screen:
            ui.screen.result("Simulation complete")
        return
    if not ui.screen and not sys.stdin.isatty():
        ui.banner()
        ui.card("Interface preview", [NOTICE, "All statuses and checks in this session are simulated."])
        simulation.ui = TourUI(ui)
        simulation.dispatch("install-all")
        for item in ("backup-panel", "backup-wings", "diagnose"):
            simulation.dispatch(item)
        ui.card("Simulation complete", [NOTICE, "Simulation credentials disappear when you exit."])
        return
    from .cli import ENTRIES, select_language
    from .navigation import Item
    while True:
        items = menu_items(ENTRIES, simulation.inventory())
        items.insert(-2, Item("reset", "Reset preview", "Clear simulated components and backups.", shortcut="99", group="Preferences"))
        if ui.screen:
            ui.screen.wizard = None
            ui.screen.dashboard = simulation.inventory()
        in_operation = False
        try:
            if ui.screen:
                selected = ui.screen.select("Interface preview", items, grouped=True)
            else:
                ui.card("Interface preview", [NOTICE])
                selected = ui.choice("Choose an option", [(item.value, item.label, item.reason or item.note) for item in items])
            if selected == "exit":
                return
            if selected == "refresh":
                continue
            in_operation = True
            if selected == "language":
                select_language(ui)
                continue
            if selected == "reset":
                ui.confirm("RESET PREVIEW")
                simulation = Simulation(ui)
                continue
            if ui.screen:
                ui.screen.begin()
            ui.say(NOTICE, "muted")
            if prepare_action(ui, selected, simulation.inventory(), simulation.dispatch, simulation.inventory):
                simulation.dispatch(selected)
                if ui.screen:
                    ui.screen.result("Simulation complete")
            elif ui.screen:
                ui.screen.result("Before you continue")
        except Cancelled:
            if not in_operation:
                return
        except (InstallerError, ValueError) as exc:
            message = str(exc)
            for secret in sorted(simulation.run.secrets, key=len, reverse=True):
                if secret:
                    message = message.replace(secret, "***")
            ui.say(message, "error")
            if ui.screen:
                ui.screen.result("Operation needs attention")
