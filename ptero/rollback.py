"""Guided rollback of a Panel update to its verified pre-update recovery backup.

Every Panel update records the backup taken before its code or schema changed.
A rollback restores that backup's SQL and version-specific files together with
the matching previous release, so code and schema always stay compatible.
Application keys, credentials and current settings (SMTP, URL) are retained.
"""
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from .common import PROJECT, SCHEMA, Cancelled, InstallerError, secure_extract, valid_version, version_key

NO_POINT = "No Panel update can be rolled back. A recovery point is saved automatically each time you update the Panel."
# Version-specific Docker Panel data restored with the previous release. Keys,
# certificates, logs and Redis stay current: keys never change during an update,
# certificates may have been renewed and old queued jobs must not run twice.
DOCKER_RESTORED = ("extensions", "extension-assets")


def available(state):
    """Cheap, read-only check used by the dashboard."""
    path = state.get("recovery_backup") if isinstance(state, dict) else None
    return isinstance(path, str) and bool(path) and Path(path).is_file()


def inspect(installer, state):
    """Verify the recovery backup and return (archive, saved_state) without extracting it."""
    from .backups import panel_archive, verified
    path = state.get("recovery_backup")
    if not isinstance(path, str) or not path:
        raise InstallerError(NO_POINT)
    archive = Path(path)
    if not archive.is_file():
        raise InstallerError(installer.ui.tr("The recovery backup is no longer available: {path}").format(path=archive))
    verified(archive)
    panel_archive(archive)
    try:
        with tarfile.open(archive, "r:*") as tar:
            member = tar.getmember("state.json")
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError("state.json")
            saved = json.load(tar.extractfile(member))
    except (KeyError, OSError, ValueError, tarfile.TarError) as exc:
        raise InstallerError("The recovery backup does not contain a readable Panel state.") from exc
    if not isinstance(saved, dict) or saved.get("owner") != PROJECT or saved.get("schema") != SCHEMA:
        raise InstallerError("This installation or backup does not belong to this installer.")
    previous = saved.get("version", "")
    if not isinstance(previous, str) or not valid_version(previous, "panel"):
        raise InstallerError("The recovery backup does not record a valid Panel version.")
    if version_key(previous) >= version_key(state["version"]):
        raise InstallerError("The recovery backup is not older than the installed Panel; there is nothing to roll back.")
    for key, default in (("method", "docker"), ("database_engine", "mariadb")):
        if saved.get(key, default) != state.get(key, default):
            raise InstallerError("The recovery backup was made for a different deployment method or database engine.")
    if saved.get("db_password") != state.get("db_password"):
        raise InstallerError("The recovery backup belongs to a different Panel database.")
    return archive, saved


def application_keys(path):
    """APP_KEY and HASHIDS_SALT lines of a .env file, for an equality check."""
    try:
        return sorted(line for line in Path(path).read_text().splitlines()
                      if line.startswith(("APP_KEY=", "HASHIDS_SALT=")))
    except OSError:
        return []


def plan_rows(ui, state, saved, archive, after_failure):
    rows = [ui.tr("Installed: {value}").format(value=state["version"]),
            ui.tr("Roll back to: {value}").format(value=saved["version"]),
            ui.tr("Recovery backup: {value}").format(value=archive)]
    if after_failure:
        rows.append("The Panel stayed stopped after the backup, so no Panel data is lost.")
    else:
        rows += ["Panel changes made after this backup will be lost.",
                 "A safety copy of the current Panel is created first."]
    rows.append("Application keys, credentials, certificates and current settings are kept.")
    return rows


def rollback(installer, after_failure=False):
    i, ui = installer, installer.ui
    state = i.panel_state()
    if state["status"] not in ("ready", "updating"):
        raise InstallerError("Finish or resume the Panel installation before using this option.")
    archive, saved = inspect(i, state)
    from .compat import check
    check(i, panel=saved["version"])
    ui.card("Panel rollback plan", plan_rows(ui, state, saved, archive, after_failure))
    if not after_failure:
        ui.confirm("ROLL BACK PANEL")
    safety = None
    if not after_failure:
        try:
            safety = i.backup_panel()
        except InstallerError as exc:
            ui.say(i.run.redact(str(exc)), "warn")
            if not ui.yes("The safety copy failed. Continue the rollback without it?", False):
                raise Cancelled("Operation cancelled.") from exc
        state = i.panel_state()
    newer = state["version"]
    i.p.base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="rollback-", dir=i.p.base) as name:
        work = Path(name)
        secure_extract(archive, work)
        if not (work / "database.sql").is_file() or not (work / "database.sql").stat().st_size:
            raise InstallerError("The recovery backup does not contain the Panel database.")
        state["status"] = "updating"
        i.save_panel(state)
        if state.get("method") == "standalone":
            i.native(state).rollback(work, saved)
        else:
            docker_rollback(i, state, work, saved)
    for key in ("recovery_backup", "previous_version"):
        state.pop(key, None)
    state.update(status="ready", rolled_back_from=newer)
    if safety:
        state["rollback_safety_backup"] = str(safety)
    i.save_panel(state)
    ui.card("Panel rolled back", [ui.tr("Running version: {value}").format(value=saved["version"]),
                                  *([ui.tr("Safety copy of {version}: {path}").format(version=newer, path=safety)] if safety else []),
                                  "Update again when a fixed release is available."])
    ui.say("Panel rollback complete.", "ok")
    return saved["version"]


def database_reset(state):
    mysql = state.get("database_engine") == "mysql"
    binary, user, password = ("mysql", "MYSQL_USER", "MYSQL_PASSWORD") if mysql else ("mariadb", "MARIADB_USER", "MARIADB_PASSWORD")
    # Tables added by newer migrations must not survive: recreate the database.
    return (f'export MYSQL_PWD="${password}"; exec {binary} --user="${user}" '
            "--execute='DROP DATABASE IF EXISTS panel; CREATE DATABASE panel;'")


def docker_rollback(i, state, work, saved):
    if not (work / "data/var/.env").is_file():
        raise InstallerError("The backup is missing SQL or the Panel's original keys.")
    if application_keys(work / "data/var/.env") != application_keys(i.p.data / "var/.env"):
        raise InstallerError("The recovery backup has different application keys; use Restore Panel from backup instead.")
    i.run.secrets.add(state["db_password"])
    i.dc("stop", "panel", label="Stopping the Panel for the rollback")
    for folder in DOCKER_RESTORED:
        source, target = work / "data" / folder, i.p.data / folder
        if not source.is_dir():
            continue
        if target.is_symlink():
            raise InstallerError("Unsafe removal path.")
        # Swap by rename so an interruption never leaves the folder missing.
        aside = target.with_name("." + folder + "-replaced-" + i.stamp())
        if target.exists():
            os.replace(target, aside)
        try:
            os.replace(source, target)
        except BaseException:
            if aside.exists():
                os.replace(aside, target)
            raise
        shutil.rmtree(aside, ignore_errors=True)
    i.dc("up", "-d", "--wait", "--wait-timeout", "240", "database", "cache", label="Preparing the database and Redis")
    i.dc("exec", "-T", "database", "sh", "-c", database_reset(state), label="Clearing the updated database schema")
    with (work / "database.sql").open("rb") as stream:
        i.dc("exec", "-T", "database", "sh", "-c", i.database_command(state, dump=False),
             input_file=stream, label="Restoring the pre-update database")
    state.update(version=saved["version"],
                 panel_image=saved.get("panel_image") or "ghcr.io/pterodactyl/panel:" + saved["version"])
    i.save_panel(state)
    i.dc("pull", "panel", label="Downloading the previous Panel image")
    i.prepare_mysql_client(state)
    i.dc("up", "-d", "--wait", "--wait-timeout", "360", "panel", label="Starting the previous Panel version")
