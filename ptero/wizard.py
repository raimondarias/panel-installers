"""Simple defaults and explicit advanced choices, collected before any changes."""

import ipaddress
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def valid_public_ipv4(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and ip.is_global
    except ValueError:
        return False


def administrator(installer):
    from .core import valid_email

    ui = installer.ui
    ui.card("First administrator", ["Your password is hidden and is never stored in the installer's state."])
    data = {
        "email": ui.ask("Administrator email", validator=valid_email),
        "username": ui.ask("Administrator username", "admin", lambda s: bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", s))),
        "name_first": "Server",
        "name_last": "Administrator",
        "root_admin": True,
    }
    password = ui.ask(
        "Administrator password (12+ characters; empty generates one)", secret=True, validator=lambda s: not s or len(s) >= 12
    )
    generated = not password
    if password:
        ui.ask("Confirm administrator password", secret=True, validator=lambda value: value == password)
    installer.admin_password = password or secrets.token_urlsafe(24)
    installer.run.secrets.add(installer.admin_password)
    installer.generated_admin_password = generated
    return data


def panel_plan(installer):
    from .core import PANEL_VERSION, SCHEMA, PROJECT, Cancelled, InstallerError, valid_domain, valid_version

    ui = installer.ui
    ui.step(1, 4, "Choose your installation")
    advanced = ui.choice(
        "Setup experience",
        [
            (False, "Simple (Recommended)", "Essential questions, sensible defaults"),
            (True, "Advanced", "Versions, ports, PHP, email and network settings"),
        ],
    )
    method = ui.choice(
        "Panel deployment",
        [
            ("standalone", "Standalone (Recommended)", "Native PHP, NGINX, database and Redis services"),
            ("docker", "Docker", "Isolated Panel, database and Redis"),
        ],
    )
    if method == "standalone":
        ui.say("Wings still uses Docker to run game servers.", "muted")
    database = ui.choice(
        "Database engine",
        [
            ("mariadb", "MariaDB (Recommended)", "MariaDB 11.4 in Docker; compatible distribution packages natively"),
            ("mysql", "MySQL", "MySQL 8.4 LTS in Docker; MySQL 8.x natively"),
        ],
    )
    version = PANEL_VERSION
    if advanced:
        version = ui.ask(
            "Panel version (auto selects the latest compatible release)", PANEL_VERSION, lambda s: s == "auto" or valid_version(s, "panel")
        )
    release = installer.release("panel", version)
    if release["prerelease"] and not ui.yes("Panel v2 is a beta release. Continue?"):
        raise Cancelled("Beta installation cancelled.")
    ui.step(2, 4, "Address and connection")
    access = ui.choice(
        "How will you access the Panel?",
        [
            ("domain", "Domain / subdomain (Recommended)", "HTTPS with a free Let's Encrypt certificate"),
            ("ip", "Public machine IP", "HTTP only, no SSL certificate"),
        ],
    )
    if access == "domain":
        host = ui.ask("Domain / subdomain (without http://)", validator=valid_domain).lower()
        tls = ui.yes("Enable HTTPS (Recommended)?", True)
    else:
        detected = None
        with ui.busy("Detecting the machine's public IPv4 address"):
            for endpoint in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
                try:
                    candidate = installer.fetch(endpoint, limit=256).decode().strip()
                    if valid_public_ipv4(candidate):
                        detected = candidate
                        break
                except (OSError, ValueError, InstallerError):
                    # No network dependency for manual input; never use a private interface as public IP.
                    continue
        host = ui.ask("Public IPv4 address of this machine", detected, valid_public_ipv4)
        tls = False
    if tls and not ui.yes("Does DNS point here, with public TCP ports 80 and 443 reachable?"):
        raise Cancelled("Prepare DNS and open the required ports, then run the wizard again.")
    port = 80
    state = {
        "schema": SCHEMA,
        "owner": PROJECT,
        "status": "installing",
        "method": method,
        "database_engine": database,
        "version": release["tag_name"],
        "access": access,
        "tls": tls,
        "http_port": port,
        "https_port": 443,
        "bind": "0.0.0.0",
        "timezone": "UTC",
        "db_password": secrets.token_hex(32),
        "admin_created": False,
        "redis_password": secrets.token_hex(32),
        "php": "8.3",
        "language": ui.language,
    }
    if advanced:
        if method == "standalone":
            state["php"] = ui.choice("PHP version", [("8.3", "PHP 8.3 (Recommended)", ""), ("8.4", "PHP 8.4", "")])
        if not tls:
            state["http_port"] = int(ui.ask("HTTP port", "80", installer.valid_port))
        state["bind"] = ui.ask("Listen IPv4 address", "0.0.0.0", installer.is_ipv4)

        def timezone(value):
            try:
                ZoneInfo(value)
                return True
            except (ZoneInfoNotFoundError, ValueError):
                return False

        state["timezone"] = ui.ask("Timezone", "UTC", timezone)
    suffix = f":{state['http_port']}" if not tls and state["http_port"] != 80 else ""
    state["url"] = ("https://" if tls else "http://") + host + suffix
    ui.step(3, 4, "Administrator and email")
    state["admin"] = administrator(installer)
    state["email"] = state["admin"]["email"]
    if ui.yes("Set up SMTP email now?", False):
        installer.smtp_settings(state)
    else:
        ui.say("Email delivery is disabled until SMTP is configured from the menu.", "muted")
    from .game_database import plan as database_host_plan

    state["game_database"] = database_host_plan(installer, state, advanced)
    ui.step(4, 4, "Review your installation")
    ui.card(
        "Ready to install",
        [
            ui.tr("Deployment: {value}").format(value=method.title()),
            ui.tr("Database: {value}").format(value="MariaDB" if database == "mariadb" else "MySQL"),
            ui.tr("Panel URL: {value}").format(value=state["url"]),
            ui.tr("Release: {value}").format(value=state["version"]),
            ui.tr("Administrator: {value}").format(value=state["admin"]["email"]),
            ui.tr("Timezone: {value}").format(value=state["timezone"]),
            ui.tr("Game database host: {value}").format(value=ui.tr(state["game_database"]["mode"])),
        ],
    )
    installer.check_plan(state)
    ui.confirm("INSTALL PANEL")
    if installer.generated_admin_password:
        from .ui import Literal

        ui.card(
            "Save your generated password",
            [Literal(installer.admin_password), "Keep it in your password manager. It will not be written to a file."],
        )
    return state
