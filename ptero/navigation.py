"""Read-only dashboard inventory and prerequisites shared by both interfaces."""
from dataclasses import dataclass
import json


@dataclass
class Item:
    value: object
    label: str
    note: str = ""
    enabled: bool = True
    reason: str = ""
    shortcut: str = ""
    group: str = ""


GROUPS = ("Install", "Maintenance", "Game databases", "Backups", "Remove", "Nodes", "Preferences")
NOTES = {
    "install-all": "Set up the Panel and a Wings node with a guided review.",
    "install-panel": "Standalone is recommended. Choose your database, address and administrator.",
    "install-wings": "Install the game server daemon, then link it to your Panel.",
    "diagnose": "Check this machine and inspect managed services without changing them.",
    "configure-database": "Register a separate SQL host for game databases in the Panel.",
    "database-credentials": "View the saved private connection details for the provisioning account.",
    "update-panel": "Back up the Panel before updating its code and database schema.",
    "update-wings": "Update Wings with recovery of its previous binary or image on failure.",
    "rollback-panel": "Return to the Panel version before the last update, using its verified backup.",
    "restore-panel": "Restore a verified backup into an empty managed destination.",
    "uninstall-all": "Remove managed components. Game files and game SQL remain.",
    "uninstall-panel": "Back up first, then choose whether to retain Panel data.",
    "uninstall-wings": "Remove Wings while preserving game containers and files.",
    "configure-wings": "Use a node ID and Auto-Deploy token to link Wings.",
    "tls-wings": "Issue or renew the HTTPS certificate for the node address.",
    "backup-panel": "Save Panel SQL, original application keys and persistent configuration.",
    "backup-wings": "Save the node configuration before changing or removing Wings.",
    "restart": "Restart the managed services after a confirmation.",
    "logs": "Read recent service logs with secrets redacted.",
    "smtp": "Configure the Panel's outgoing email delivery.",
    "admin": "Create an additional administrator with a hidden password.",
    "firewall": "Review required ports without changing firewall rules.",
    "backup-schedule": "Schedule encrypted copies of the Panel and Wings configuration only.",
    "backup-run": "Run the saved Panel and Wings configuration backup schedule now.",
    "backup-download": "Download and verify a saved encrypted Panel or Wings configuration backup.",
    "health": "Inspect this machine and report service failures with next steps.",
    "resume": "Continue an interrupted managed installation from its saved phases.",
    "create-node": "Create a location, node and allocations in your managed Panel.",
    "remote-node": "Provision Wings over verified SSH and register its node in your Panel.",
    "remote-database": "Prepare a private SQL host over verified SSH and register it in your Panel.",
}


# Action numbers are public shortcuts. Retired game-data backup numbers remain unused.
SHORTCUTS = dict(zip(("install-all", "install-panel", "install-wings", "uninstall-all",
    "uninstall-panel", "uninstall-wings", "update-panel", "update-wings", "configure-wings", "tls-wings",
    "backup-panel", "backup-wings", "restore-panel", "diagnose", "restart", "logs", "smtp", "admin", "firewall",
    "configure-database", "database-credentials"), map(str, range(1, 22))))
SHORTCUTS.update({"create-node": "24", "backup-schedule": "28", "backup-run": "29", "backup-download": "30",
    "health": "31", "resume": "32", "remote-node": "33", "remote-database": "34",
    "rollback-panel": "35"})


def inventory(installer):
    """Inspect ownership before asking any backend to read saved credentials."""
    from .core import InstallerError, PROJECT, SCHEMA
    result = {"panel": "absent", "wings": "absent", "database": "absent", "backup_profile": "absent",
              "credentials": False, "interrupted": False}
    for key, path in (("panel", installer.p.state), ("wings", installer.p.wings_state),
                      ("database", installer.p.base / "game-databases/state.json"),
                      ("backup_profile", installer.p.base / "backup-profile.json")):
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                if not isinstance(raw, dict):
                    raise ValueError("Invalid saved state")
                if raw.get("owner") != PROJECT:
                    result[key] = "foreign"
                    continue
                if raw.get("schema") != SCHEMA:
                    raise ValueError("Unsupported saved state")
                state = installer.panel_state() if key == "panel" else installer.load(path)
                if key == "backup_profile":
                    if not all(isinstance(state.get(name), str) and state[name] for name in ("repository", "backup_id", "calendar")):
                        raise ValueError("Incomplete backup profile")
                    environment = state.get("environment", {})
                    if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
                        raise ValueError("Invalid backup environment")
                    result[key] = "ready"
                else:
                    status = state.get("status")
                    if not isinstance(status, str) or status not in ("ready", "pending", "installing", "failed", "updating", "restoring", "configured", "stopped"):
                        raise ValueError("Invalid component status")
                    result[key] = status
                if key in ("panel", "wings") and isinstance(state.get("version"), str):
                    result[key + "_version"] = state["version"]
                if key == "panel":
                    from .rollback import available
                    result["rollback"] = status in ("ready", "updating") and available(state)
                    result["url"] = state.get("url", "")
                    result["method"] = state.get("method", "docker")
                    result["credentials"] = bool(state.get("game_database", {}).get("registered"))
                if key != "backup_profile":
                    result["interrupted"] |= state.get("status") != "ready"
            except (OSError, ValueError, KeyError, TypeError, InstallerError):
                result[key] = "invalid"
    credential_path = installer.p.base / "game-database-credentials.json"
    if credential_path.is_file():
        try:
            installer.load(credential_path)
            result["credentials"] = True
        except (OSError, ValueError, KeyError, InstallerError):
            pass
    # Existing unmanaged files need a manual review, never an automatic adoption.
    panel_files = installer.p.root / "var/www/pterodactyl"
    if result["panel"] == "absent" and any((panel_files / name).exists() for name in ("artisan", ".env")):
        result["panel"] = "unmanaged"
    if result["wings"] == "absent" and any(path.exists() for path in (installer.p.wings, installer.p.config, installer.p.service)):
        result["wings"] = "unmanaged"
    return result


def _state_problem(component, status):
    return {
        "Panel": {
            "foreign": "Panel state belongs to another installer. Review it manually; it will not be adopted or overwritten.",
            "unmanaged": "Panel files exist without managed state. Review this installation manually; it will not be adopted or overwritten.",
            "invalid": "Panel state is invalid or unreadable. Use diagnostics and recover the saved state before changing it."},
        "Wings": {
            "foreign": "Wings state belongs to another installer. Review it manually; it will not be adopted or overwritten.",
            "unmanaged": "Wings files exist without managed state. Review this installation manually; it will not be adopted or overwritten.",
            "invalid": "Wings state is invalid or unreadable. Use diagnostics and recover the saved state before changing it."},
    }.get(component, {}).get(status, "")


STATUS_LABELS = {"absent": "Not installed", "ready": "Configured", "stopped": "Stopped", "pending": "Not linked",
                 "configured": "Linked, not started", "installing": "Installation incomplete",
                 "updating": "Update incomplete", "restoring": "Restore incomplete", "failed": "Needs attention",
                 "invalid": "Needs review", "foreign": "Needs review", "unmanaged": "Needs review"}


def status_label(key, state):
    """Friendly dashboard wording for a component status from inventory()."""
    status = state.get(key, "absent")
    if key == "database" and status == "absent":
        return "Not configured"
    return STATUS_LABELS.get(status, "Needs attention")


def attention(state):
    """One actionable dashboard hint, or an empty string when nothing is pending."""
    if state.get("panel") == "updating":
        return "Panel update incomplete. Choose Resume installation (32) to continue or roll back."
    if state.get("interrupted"):
        return "An operation was interrupted. Choose Resume installation (32) to continue."
    if any(state.get(key) in ("invalid", "foreign", "unmanaged") for key in ("panel", "wings")):
        return "A component needs manual review. Choose Status and diagnostics (14)."
    return ""


def prerequisite(action, state):
    panel, wings = state["panel"], state["wings"]
    if action == "uninstall-panel" and panel == "absent":
        return "No managed Panel installation is available to remove."
    if action == "uninstall-wings" and wings == "absent":
        return "No managed Wings installation is available to remove."
    panel_actions = ("configure-database", "smtp", "admin", "update-panel", "backup-panel", "uninstall-panel",
                     "create-node", "remote-node", "remote-database")
    wings_actions = ("configure-wings", "tls-wings", "update-wings", "backup-wings", "uninstall-wings")
    for component, status, affected in (("Panel", panel, panel_actions + ("install-panel", "install-all")),
                                         ("Wings", wings, wings_actions + ("install-wings", "install-all"))):
        if action in affected:
            problem = _state_problem(component, status)
            if problem:
                return problem
    if action in panel_actions:
        if panel == "absent":
            return "Install the Panel first. Start with Install Panel or Install Panel and Wings."
        if action != "uninstall-panel" and panel != "ready":
            return "Finish or resume the Panel installation before using this option."
    if action in wings_actions and wings == "absent":
        return "Install Wings first, then create or link a node in your Panel."
    if action in ("restart", "logs", "uninstall-all", "backup-schedule", "backup-run", "resume"):
        for component, status in (("Panel", panel), ("Wings", wings)):
            problem = _state_problem(component, status)
            if problem:
                return problem
    if action in ("restart", "logs", "uninstall-all", "backup-schedule") and panel == wings == "absent":
        return "There are no managed components yet. Start from Install."
    if action == "restart" and panel not in ("absent", "ready"):
        return "Finish or resume the Panel installation before using this option."
    if action in ("backup-schedule", "backup-run") and panel not in ("absent", "ready"):
        return "Finish or resume the Panel installation before using this option."
    if action == "rollback-panel":
        problem = _state_problem("Panel", panel)
        if problem:
            return problem
        if panel == "absent":
            return "No managed Panel is installed, so there is no update to roll back."
        if panel not in ("ready", "updating"):
            return "Finish or resume the Panel installation before using this option."
        if not state.get("rollback"):
            return "No Panel update can be rolled back. A recovery point is saved automatically each time you update the Panel."
    if action == "database-credentials" and not state["credentials"]:
        return "Configure a game database host first to save its connection details."
    if action in ("backup-run", "backup-download"):
        profile = state.get("backup_profile", "absent")
        if profile == "absent":
            return "Configure scheduled Panel and Wings configuration backups first."
        if profile != "ready":
            return "The saved backup profile is invalid or belongs to another installer. Review it before continuing."
        if action == "backup-run" and panel == wings == "absent":
            return "There are no managed components to back up. Install the Panel or Wings first."
    if action == "resume":
        if state.get("database") in ("invalid", "foreign"):
            return "The saved SQL host state needs manual review before resuming."
        if not state.get("interrupted", False):
            return "No interrupted managed installation needs resuming."
    return ""


def prepare_action(ui, action, state, install, refresh):
    """Offer an explicit guided setup before retrying an existing operation."""
    reason = prerequisite(action, state)
    if not reason:
        return True
    ui.card("Before you continue", [reason])
    setup = None
    if state["panel"] == "absent" and reason.startswith("Install the Panel first"):
        setup = "install-panel"
    elif state["wings"] == "absent" and reason.startswith("Install Wings first"):
        setup = "install-wings"
    elif state["panel"] == state["wings"] == "absent" and action in ("restart", "logs", "backup-schedule"):
        setup = "install-all"
    if setup:
        question = {"install-panel": "Install the Panel now and return to this action?",
                    "install-wings": "Install Wings now and return to this action?",
                    "install-all": "Install Panel and Wings now and return to this action?"}[setup]
        if ui.yes(question, False):
            install(setup)
            remaining = prerequisite(action, refresh())
            if not remaining:
                return True
            ui.card("Before you continue", [remaining])
    return False


def group_for(action):
    if action in ("create-node", "remote-node", "remote-database"):
        return "Nodes"
    if action.startswith("install-"):
        return "Install"
    if action.startswith("uninstall-"):
        return "Remove"
    if action in ("configure-database", "database-credentials"):
        return "Game databases"
    if action.startswith("backup-") or action == "restore-panel":
        return "Backups"
    return "Maintenance"


def menu_items(entries, state):
    result = []
    for label, action in entries:
        reason = prerequisite(action, state)
        result.append(Item(action, label, NOTES.get(action, ""), True, reason,
                           SHORTCUTS[action], group_for(action)))
    result += [Item("language", "Change language", "Choose English, Spanish or Brazilian Portuguese.", shortcut="23", group="Preferences"),
               Item("exit", "Exit", "Return to your shell.", shortcut="0", group="Preferences")]
    return result
