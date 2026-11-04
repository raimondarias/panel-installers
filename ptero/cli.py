"""Language selection, grouped maintenance menu and process locking."""

import argparse
import fcntl
import os
import time

from . import __version__
from .i18n import LANGUAGES
from .ui import UI


def select_language(ui):
    if not ui.screen:
        ui.say("Language")
    language = ui.choice(
        "Which language would you like to use?",
        [("en", "English", ""), ("es", "Español", ""), ("pt-BR", "Português (Brasil)", "")],
        default=list(LANGUAGES).index(ui.language),
    )
    ui.set_language(language)
    return language


ENTRIES = [
    ("Install Panel and Wings", "install-all"),
    ("Install Panel", "install-panel"),
    ("Install Wings", "install-wings"),
    ("Uninstall Panel and Wings", "uninstall-all"),
    ("Uninstall Panel", "uninstall-panel"),
    ("Uninstall Wings", "uninstall-wings"),
    ("Update Panel v2", "update-panel"),
    ("Update Wings", "update-wings"),
    ("Roll back Panel update", "rollback-panel"),
    ("Configure / link Wings", "configure-wings"),
    ("Wings HTTPS certificate", "tls-wings"),
    ("Back up Panel", "backup-panel"),
    ("Back up Wings configuration", "backup-wings"),
    ("Restore Panel from backup", "restore-panel"),
    ("Status and diagnostics", "diagnose"),
    ("Restart services", "restart"),
    ("View logs", "logs"),
    ("Configure SMTP", "smtp"),
    ("Create administrator", "admin"),
    ("Ports and firewall", "firewall"),
    ("Configure game database host", "configure-database"),
    ("Show database host credentials", "database-credentials"),
    ("Create node and allocations", "create-node"),
    ("Schedule encrypted backups", "backup-schedule"),
    ("Run scheduled backup now", "backup-run"),
    ("Download remote backup", "backup-download"),
    ("Local backup retention", "backup-retention"),
    ("Detailed health report", "health"),
    ("Resume installation", "resume"),
    ("Install remote Wings node", "remote-node"),
    ("Prepare remote game SQL", "remote-database"),
]


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


def menu(installer):
    from .core import InstallerError, Cancelled
    from .navigation import attention, inventory, menu_items, prepare_action, status_label
    from .ui import Literal

    ui = installer.ui
    while True:
        state = inventory(installer)
        items = menu_items(ENTRIES, state)
        if ui.screen:
            ui.screen.wizard = None
            ui.screen.dashboard = state
            try:
                action = ui.screen.select("Overview", items, grouped=True)
            except Cancelled:
                return
        else:
            print()
            ui.banner()
            rows = []
            for key, label in (("panel", "Panel"), ("wings", "Wings"), ("database", "Game SQL")):
                value = ui.tr(status_label(key, state))
                if state.get(key + "_version") and state[key] != "absent":
                    value += " · " + state[key + "_version"]
                rows.append(Literal(ui.tr(label) + ": " + value))
            hint = attention(state)
            rows.append(
                hint
                or ("New machine? Start with Install Panel and Wings." if state["panel"] == "absent" else Literal(state.get("url", "")))
            )
            ui.card("Installation status", rows)
            last_group = None
            for item in items:
                if item.group != last_group:
                    print("\n  " + ui.paint(ui.tr(item.group), "35"))
                    last_group = item.group
                suffix = " · " + ui.tr("Setup required") if item.reason else ""
                print(f"  {item.shortcut:>2}  {ui.tr(item.label)}{suffix}")
            print()
            selected = ui.ask("Choose an option", validator=lambda s: any(item.shortcut == s for item in items))
            item = next(item for item in items if item.shortcut == selected)
            action = item.value
        if action == "exit":
            return
        if action == "refresh":
            continue
        if action == "language":
            try:
                select_language(ui)
            except Cancelled:
                pass
            except (InstallerError, OSError, ValueError) as exc:
                ui.say(installer.run.redact(str(exc)), "error")
                if ui.screen:
                    ui.screen.result("Language could not be changed")
            continue
        if ui.screen:
            ui.screen.begin()
        outcome = "Operation complete"
        started = time.monotonic()
        try:
            if prepare_action(ui, action, state, lambda setup: installer.actions()[setup](), lambda: inventory(installer)):
                installer.actions()[action]()
            else:
                outcome = "Before you continue"
        except Cancelled:
            outcome = "Operation cancelled."
            ui.say(outcome, "muted")
        except (InstallerError, OSError, ValueError, KeyError) as exc:
            outcome = "Operation needs attention"
            ui.say(installer.run.redact(str(exc)), "error")
            ui.say("The operation did not complete. Review the private installer log.", "warn")
        if outcome != "Before you continue":
            ui.say(ui.tr("Duration: {value}").format(value=duration(time.monotonic() - started)), "muted")
        if ui.screen:
            ui.screen.result(outcome)


def main(argv=None):
    from .core import Installer, InstallerError

    parser = argparse.ArgumentParser(description="Pterodactyl Installer — multilingual Panel v2 and Wings setup for Ubuntu / Debian.")
    parser.add_argument("--language", choices=LANGUAGES, help="Skip the startup language selector")
    parser.add_argument("--no-animation", action="store_true", help="Disable terminal animations")
    parser.add_argument("--plain", action="store_true", help="Use numbered prompts instead of the full-screen TUI")
    parser.add_argument("--preview", action="store_true", help="Preview the interface without root or system changes")
    parser.add_argument("--version", action="version", version="Pterodactyl Installer " + __version__)
    parser.add_argument("action", nargs="?", help="Direct action, e.g. install-all, install-panel, diagnose, backup-panel")
    args = parser.parse_args(argv)
    ui = UI(animate=not args.no_animation, language=args.language or "en", plain=args.plain)
    installer = Installer(ui=ui)
    if args.action and args.action not in installer.actions():
        parser.error("Unknown action. Choices: " + ", ".join(installer.actions()))
    try:
        with ui.session():
            if args.language is None:
                select_language(ui)
            if args.preview:
                from .preview import show

                show(ui, args.action)
                return 0
            if os.geteuid() != 0:
                raise InstallerError("Run with sudo or as root.")
            os.umask(0o077)
            installer.p.base.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (installer.p.base / ".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise InstallerError("Another installer is already running.") from exc
                # Upgrade an owned saved schedule before any interactive or mutating
                # operation can leave its former game-data backup runtime active.
                # Read-only diagnostics never change profiles or installed runtimes.
                readonly = {"diagnose", "health", "firewall", "logs", "database-credentials", "backup-download"}
                if args.action not in readonly and (installer.p.base / "backup-profile.json").exists():
                    from .navigation import inventory

                    if inventory(installer)["backup_profile"] == "ready":
                        from .backups import ScheduledBackups

                        ScheduledBackups(installer).migrate()
                if args.action:
                    from .navigation import inventory, prepare_action

                    ui.banner()
                    ready = prepare_action(
                        ui, args.action, inventory(installer), lambda setup: installer.actions()[setup](), lambda: inventory(installer)
                    )
                    if ready:
                        installer.actions()[args.action]()
                    if ui.screen:
                        ui.screen.result("Operation complete" if ready else "Before you continue")
                    if not ready:
                        return 1
                else:
                    menu(installer)
    except (KeyboardInterrupt, EOFError):
        ui.say("\nInterrupted. Keep the log and resume from the menu.", "warn")
        return 130
    except (InstallerError, OSError, ValueError, KeyError) as exc:
        ui.say(installer.run.redact(str(exc)), "error")
        return 1
    return 0
