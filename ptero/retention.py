"""Local retention for Panel archives and Wings configuration copies.

Every update, removal and manual backup leaves a copy in the private backup
directory. Retention keeps the newest copies of each kind and never removes:
the active update rollback point, the rollback safety copy, copies owned by the
encrypted schedule (it applies its own retention), symlinks or anything whose
name or contents the installer did not create.
"""
import json
import re
import shutil

from .common import PROJECT, SCHEMA, InstallerError, write_json

DEFAULT_KEEP = 5
MAXIMUM_KEEP = 100
PANEL_ARCHIVE = re.compile(r"panel-(?:standalone-)?\d{8}T\d{6}Z-[0-9a-f]{6}\.tar\.gz")
WINGS_COPY = re.compile(r"wings-\d{8}T\d{6}Z-[0-9a-f]{6}")
WINGS_FILES = frozenset(("wings.json", "config.yml", "wings.service", "wings-compose.json", "scheduled-backup.json"))


def settings_path(installer):
    return installer.p.base / "settings.json"


def keep_count(installer):
    path = settings_path(installer)
    if not path.is_file():
        return DEFAULT_KEEP
    try:
        value = installer.load(path).get("keep_local_backups", DEFAULT_KEEP)
    except InstallerError:
        return DEFAULT_KEEP
    return value if type(value) is int and 0 <= value <= MAXIMUM_KEEP else DEFAULT_KEEP


def protected(installer):
    """Resolved paths that retention must never delete."""
    keep = set()
    if installer.p.state.is_file():
        try:
            state = json.loads(installer.p.state.read_text())
            for key in ("recovery_backup", "rollback_safety_backup"):
                if isinstance(state.get(key), str):
                    keep.add(state[key])
        except (OSError, ValueError):
            pass
    history = installer.p.base / "backup-history.json"
    if history.is_file():
        try:
            for run in json.loads(history.read_text()).get("runs", []):
                keep.update(str(path) for path in run)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return keep


def candidates(installer):
    directory = installer.p.backups
    if not directory.is_dir():
        return [], []
    panel, wings = [], []
    for path in directory.iterdir():
        if path.is_symlink():
            continue
        if path.is_file() and PANEL_ARCHIVE.fullmatch(path.name):
            panel.append(path)
        elif path.is_dir() and WINGS_COPY.fullmatch(path.name):
            entries = list(path.iterdir())
            if all(item.name in WINGS_FILES and item.is_file() and not item.is_symlink() for item in entries):
                wings.append(path)
    newest = lambda item: item.stat().st_mtime
    return sorted(panel, key=newest, reverse=True), sorted(wings, key=newest, reverse=True)


def prune(installer, keep=None):
    """Remove the oldest unprotected copies beyond ``keep``; return how many were removed."""
    keep = keep_count(installer) if keep is None else keep
    if keep == 0:
        return 0
    safe = protected(installer)
    removed = 0
    for copies in candidates(installer):
        for path in [item for item in copies if str(item) not in safe][keep:]:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
                (path.parent / (path.name + ".sha256")).unlink(missing_ok=True)
            removed += 1
    if removed:
        installer.ui.say(installer.ui.tr("Removed {count} older local backups; keeping the newest {keep} of each kind.")
                         .format(count=removed, keep=keep), "muted")
    return removed


def configure(installer):
    ui = installer.ui
    current = keep_count(installer)
    panel, wings = candidates(installer)
    ui.card("Local backup retention", [
        ui.tr("Panel archives on this server: {value}").format(value=len(panel)),
        ui.tr("Wings configuration copies on this server: {value}").format(value=len(wings)),
        ui.tr("Currently kept: {value}").format(value=current or ui.tr("all")),
        "The active rollback point, its safety copy and scheduled copies are never removed.",
        "Copies are pruned after each new backup. 0 keeps every copy."])
    value = ui.ask("Copies of each kind to keep (0-100)", str(current),
                   lambda text: text.isdigit() and 0 <= int(text) <= MAXIMUM_KEEP)
    write_json(settings_path(installer), {"schema": SCHEMA, "owner": PROJECT, "keep_local_backups": int(value)})
    prune(installer, int(value))
    ui.say("Local backup retention saved.", "ok")
