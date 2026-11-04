"""Optional game SQL host, independent of Panel data and its lifecycle.

Local SQL uses a dedicated native service or Docker database: the privileged
provisioning account must never share the Panel SQL server.
Its published port binds only to its own private Docker bridge IPv4 gateway.
"""

import ipaddress
import json
import shlex
import re
import secrets
import tempfile

from .core import PROJECT, SCHEMA, InstallerError, atomic_write, write_json, valid_host
from .ui import Literal

NETWORK = "ptero-game-databases"
COMPOSE_PROJECT = "ptero-game-databases"
OWNER_LABEL = "io.ptero-installer.owner"


def valid_password(value):
    return 16 <= len(value) <= 128 and not any(ord(char) < 32 or ord(char) == 127 for char in value)


def valid_endpoint(value):
    if not valid_host(value) or ":" in value or value.rstrip(".").lower() == "localhost":
        return False
    try:
        address = ipaddress.ip_address(value)
        return not (address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast)
    except ValueError:
        return True


def plan(installer, panel, advanced=False):
    ui = installer.ui
    ui.card(
        "Game database host",
        [
            "One engine selection for Panel and games.",
            "Game databases use separate credentials. Each game gets its own database from the Panel.",
        ],
    )
    mode = ui.choice(
        "Where are the game databases?",
        [
            ("local", "Local node (isolated game SQL)", "Same machine only; private bridge, no public SQL port"),
            ("existing", "Existing database host", "Register a database server already prepared on another machine"),
            ("later", "Configure later", "Skip game databases; configure them later from the menu"),
        ],
        default=2,
    )
    if mode == "later":
        return {"mode": mode}
    method = "docker"
    if mode == "local":
        method = ui.choice(
            "Game SQL deployment",
            [
                ("standalone", "Isolated native instance (Recommended)", "Separate SQL data, socket and system service"),
                ("docker", "Docker", "Isolated SQL container on a private bridge"),
            ],
        )
    settings = {
        "mode": mode,
        "engine": panel.get("database_engine", "mariadb"),
        "name": "Game databases",
        "port": 3307 if mode == "local" else 3306,
        "username": "ptero_games",
        "password": secrets.token_hex(32),
        "node_id": None,
        "registered": False,
    }
    settings["method"] = method
    saved = installer.existing_game_database() if mode == "local" else None
    reuse_local = saved is not None
    if reuse_local:
        settings["method"] = saved.get("method", "docker")
        if saved["engine"] != settings["engine"]:
            raise InstallerError(
                (
                    "A local game database already exists. Reuse its saved credentials; changing its engine or password "
                    "requires a separate migration."
                )
            )
        for key in ("port", "username", "password"):
            settings[key] = saved[key]
        ui.say("Reusing the local database instance and its saved credentials.")
    if mode == "existing":
        ui.say("The address must be reachable from both the Panel and game containers. Never use localhost for a remote node.")
        settings["host"] = ui.ask("Game database hostname / IPv4", validator=valid_endpoint)
        settings["port"] = int(ui.ask("Game database port", "3306", installer.valid_port))
        settings["username"] = ui.ask(
            "Database host provisioning username", validator=lambda s: bool(re.fullmatch(r"[A-Za-z0-9_]{1,32}", s))
        )
        settings["password"] = ui.ask(
            "Database host provisioning password",
            secret=True,
            validator=lambda s: bool(s) and len(s) <= 128 and not any(ord(c) < 32 for c in s),
        )
        ui.say(
            "Prepare the remote SQL grants, private routing and firewall first; the installer will test the connection before registration."
        )
    if advanced:
        settings["name"] = ui.ask(
            "Database host display name", settings["name"], lambda s: 1 <= len(s) <= 191 and not any(ord(c) < 32 for c in s)
        )
        if mode == "local" and not reuse_local:
            settings["port"] = int(ui.ask("Game database port", "3307", installer.valid_port))
            settings["username"] = ui.ask(
                "Database host provisioning username",
                "ptero_games",
                lambda s: bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", s))
                and s not in ("root", "mysql", "mariadb", "pterodactyl", "ptero_panel"),
            )
            password = ui.ask(
                "Database host password (16+ characters; empty generates one)", secret=True, validator=lambda s: not s or valid_password(s)
            )
            if password:
                ui.ask("Confirm database host password", secret=True, validator=lambda value: value == password)
                settings["password"] = password
        node = ui.ask("Existing node ID (0 = global / node not created yet)", "0", lambda s: s.isdigit() and 0 <= int(s) <= 2147483647)
        settings["node_id"] = int(node) or None
    installer.run.secrets.add(settings["password"])
    return settings


class GameDatabase:
    def __init__(self, installer):
        self.i, self.p = installer, installer.p
        self.run, self.ui = installer.run, installer.ui
        self.base = self.p.base / "game-databases"
        self.state_file = self.base / "state.json"
        self.compose = self.base / "compose.json"
        self.data = self.base / "data"
        self.init = self.base / "init.sql"
        self.report = self.p.base / "game-database-credentials.json"
        self.firewall_script = self.base / "firewall.sh"
        self.firewall_unit = self.p.root / "etc/systemd/system/ptero-game-db-firewall.service"
        self.service = self.p.root / "etc/systemd/system/ptero-game-databases.service"

    def load(self):
        state = self.i.load(self.state_file)
        self.run.secrets.update((state["password"], state["root_password"]))
        return state

    def dc(self, *args, **kwargs):
        return self.run.run(["docker", "compose", "--project-name", COMPOSE_PROJECT, "--file", self.compose, *args], **kwargs)

    def network(self, create=False, config=None):
        code, output = self.run.run(["docker", "network", "inspect", NETWORK], check=False)
        if code:
            if not create:
                raise InstallerError("The managed game database network is missing.")
            addressing = []
            if config:
                subnet = ipaddress.IPv4Network(config["Subnet"])
                gateway = ipaddress.IPv4Address(config["Gateway"])
                if not subnet.is_private or not gateway.is_private or gateway not in subnet or gateway.is_loopback:
                    raise InstallerError("The SQL configuration contains an invalid private network.")
                addressing = ["--subnet", str(subnet), "--gateway", str(gateway)]
            self.run.run(
                [
                    "docker",
                    "network",
                    "create",
                    *addressing,
                    "--driver",
                    "bridge",
                    "--label",
                    OWNER_LABEL + "=" + PROJECT,
                    "--opt",
                    "com.docker.network.bridge.name=ptero-db0",
                    "--opt",
                    "com.docker.network.bridge.gateway_mode_ipv4=nat",
                    NETWORK,
                ],
                label="Creating the private game database network",
            )
            _, output = self.run.run(["docker", "network", "inspect", NETWORK])
        try:
            network = json.loads(output)[0]
            if network["Driver"] != "bridge" or network.get("Labels", {}).get(OWNER_LABEL) != PROJECT:
                raise ValueError
            if network.get("Options", {}).get("com.docker.network.bridge.name") != "ptero-db0":
                raise ValueError
            if network.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4", "nat") != "nat":
                raise ValueError
            for config in network["IPAM"]["Config"]:
                gateway = ipaddress.ip_address(config["Gateway"])
                subnet = ipaddress.ip_network(config["Subnet"])
                if gateway.version == 4 and gateway.is_private and not gateway.is_loopback and gateway in subnet:
                    return str(gateway)
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        raise InstallerError("The game database network is not an owned private IPv4 bridge.")

    def network_config(self):
        _, output = self.run.run(["docker", "network", "inspect", NETWORK])
        for config in json.loads(output)[0]["IPAM"]["Config"]:
            if ipaddress.ip_address(config["Gateway"]).version == 4:
                return {"Subnet": config["Subnet"], "Gateway": config["Gateway"]}
        raise InstallerError("No IPv4 game database network was found.")

    def firewall(self, state):
        # Publishing on a private IP alone does not block a remote host that has
        # a static route to it. Filter forwarded AND docker-proxy input traffic.
        # No global flush, policy change, UFW reset or SSH rule changes.
        code, _ = self.run.run(["iptables", "-w", "-S", "DOCKER-USER"], check=False)
        if code:
            raise InstallerError(
                (
                    "Local game databases require Docker's iptables DOCKER-USER chain. Use an existing private SQL host "
                    "with this Docker firewall backend."
                )
            )
        gateway = str(ipaddress.IPv4Address(state["host"]))
        port = int(state["port"])
        script = (
            "#!/bin/sh\nset -eu\n"
            "iptables -w -N PTERO-GAME-DB 2>/dev/null || iptables -w -S PTERO-GAME-DB >/dev/null\n"
            "for interface in lo docker0 'br-+' pterodactyl0; do\n"
            "  iptables -w -C PTERO-GAME-DB -i \"$interface\" -j RETURN 2>/dev/null || iptables -w -A PTERO-GAME-DB "
            "-i \"$interface\" -j RETURN\n"
            "done\n"
            "iptables -w -C PTERO-GAME-DB -j DROP 2>/dev/null || iptables -w -A PTERO-GAME-DB -j DROP\n"
            "iptables -w -C DOCKER-USER -o ptero-db0 -p tcp --dport 3306 -j PTERO-GAME-DB 2>/dev/null || "
            "iptables -w -I DOCKER-USER 1 -o ptero-db0 -p tcp --dport 3306 -j PTERO-GAME-DB\n"
            f"iptables -w -C INPUT -d {gateway} -p tcp --dport {port} -j PTERO-GAME-DB 2>/dev/null || iptables "
            f"-w -I INPUT 1 -d {gateway} -p tcp --dport {port} -j PTERO-GAME-DB\n"
        )
        atomic_write(self.firewall_script, script, 0o700)
        atomic_write(
            self.firewall_unit,
            "[Unit]\nDescription=Restrict local Pterodactyl game SQL to host and game bridges\n"
            "Requires=docker.service\nAfter=docker.service\nPartOf=docker.service\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/sh {shlex.quote(str(self.firewall_script))}\n"
            "[Install]\nWantedBy=multi-user.target\n",
            0o644,
        )
        self.run.run(["systemctl", "daemon-reload"])
        self.run.run(["systemctl", "enable", "ptero-game-db-firewall.service"])
        self.run.run(["systemctl", "restart", "ptero-game-db-firewall.service"], label="Restricting game SQL to local container bridges")

    def config(self, state):
        mysql = state["engine"] == "mysql"
        prefix = "MYSQL" if mysql else "MARIADB"
        environment = {prefix + "_ROOT_PASSWORD": state["root_password"].replace("$", "$$"), prefix + "_ROOT_HOST": "localhost"}
        # TCP ping excludes the temporary initialization server. The SQL query
        # uses root's local socket, which works with skip-name-resolve enabled.
        check = (
            [
                "CMD-SHELL",
                'mysqladmin --host=127.0.0.1 --user=root --silent ping >/dev/null 2>&1 && '
                'MYSQL_PWD="$$MYSQL_ROOT_PASSWORD" mysql --protocol=socket --user=root --execute="SELECT 1" --silent',
            ]
            if mysql
            else ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
        )
        return {
            "name": COMPOSE_PROJECT,
            "services": {
                "database": {
                    "image": state["image"],
                    "restart": "no",
                    "stop_grace_period": "120s",
                    "environment": environment,
                    "ports": [f"{state['host']}:{state['port']}:3306"],
                    "volumes": [{"type": "bind", "source": str(self.data), "target": "/var/lib/mysql"}],
                    "networks": ["games"],
                    "healthcheck": {"test": check, "interval": "10s", "timeout": "5s", "retries": 30, "start_period": "30s"},
                }
            },
            "networks": {"games": {"external": True, "name": NETWORK}},
        }

    def service_config(self):
        command = "/usr/bin/docker compose --project-name " + COMPOSE_PROJECT + " --file " + shlex.quote(str(self.compose))
        atomic_write(
            self.service,
            "[Unit]\nDescription=Pterodactyl Installer: isolated game SQL host\n"
            "Requires=docker.service ptero-game-db-firewall.service\n"
            "After=docker.service ptero-game-db-firewall.service\nPartOf=docker.service\n"
            f"[Service]\nType=simple\nExecStart={command} up --abort-on-container-exit --exit-code-from database\n"
            f"ExecStop={command} stop --timeout 120 database\n"
            "Restart=always\nRestartSec=5\nTimeoutStartSec=0\nTimeoutStopSec=150\n"
            "[Install]\nWantedBy=docker.service\n",
            0o644,
        )
        self.run.run(["systemctl", "daemon-reload"])
        self.run.run(["systemctl", "enable", "ptero-game-databases.service"])

    def start(self, state=None):
        # One Compose client creates/starts containers. The supervisor attaches
        # afterward, avoiding concurrent startup during install and recovery.
        self.run.run(["systemctl", "stop", "ptero-game-databases.service"], check=False)
        self.dc("up", "-d", "--wait", "--wait-timeout", "420", "database", label="Starting the isolated game database")
        if state is not None:
            self.provision(state)
        self.run.run(["systemctl", "start", "ptero-game-databases.service"])

    def provision(self, state):
        self.run.secrets.update((state["password"], state["root_password"]))
        mysql = state["engine"] == "mysql"
        variable = "MYSQL_ROOT_PASSWORD" if mysql else "MARIADB_ROOT_PASSWORD"
        client = "mysql" if mysql else "mariadb"
        command = 'MYSQL_PWD="$' + variable + '" exec ' + client + ' --user=root --batch'
        with tempfile.TemporaryFile() as stream:
            stream.write(self.init_sql(state).encode())
            stream.seek(0)
            self.dc("exec", "-T", "database", "sh", "-c", command, input_file=stream, label="Preparing the game SQL provisioning account")

    def init_sql(self, state):
        # A dedicated instance contains no Panel database. Its manager can create
        # per-game accounts and grant their privileges, as required by upstream.
        def quote(value):
            return "'" + value.replace("'", "''") + "'"

        account = quote(state["username"]) + "@'%'"
        return (
            "SET SESSION sql_mode='NO_BACKSLASH_ESCAPES';\n"
            f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY {quote(state['password'])};\n"
            f"ALTER USER {account} IDENTIFIED BY {quote(state['password'])};\n"
            f"GRANT ALL PRIVILEGES ON *.* TO {account} WITH GRANT OPTION;\n"
        )

    def local(self, settings):
        if settings.get("method") == "standalone":
            from .native_game_database import NativeGameDatabase

            return NativeGameDatabase(self).install(settings)
        self.i.docker()
        if self.state_file.exists():
            state = self.load()
            if any(state[key] != settings[key] for key in ("engine", "port", "username", "password")):
                raise InstallerError(
                    (
                        "A local game database already exists. Reuse its saved credentials; changing its engine or password "
                        "requires a separate migration."
                    )
                )
            if self.network() != state["host"]:
                raise InstallerError("The private database gateway changed. Restore its original network before proceeding.")
        else:
            if self.base.exists() and any(self.base.iterdir()):
                raise InstallerError("Unmanaged game database files exist; nothing was overwritten.")
            if self.firewall_unit.exists() or self.service.exists():
                raise InstallerError("Unmanaged game database files exist; nothing was overwritten.")
            state = {
                **settings,
                "schema": SCHEMA,
                "owner": PROJECT,
                "status": "installing",
                "host": self.network(create=True),
                "root_password": secrets.token_hex(32),
            }
            state["network_config"] = self.network_config()
            self.run.secrets.add(state["root_password"])
            self.i.port_free(state["host"], state["port"])
            state["image"] = self.i.pin_image("mysql:8.4" if state["engine"] == "mysql" else "mariadb:11.4")
            self.base.mkdir(parents=True, mode=0o700, exist_ok=True)
            write_json(self.state_file, state)
        self.data.mkdir(parents=True, mode=0o700, exist_ok=True)
        atomic_write(self.init, self.init_sql(state))
        write_json(self.compose, self.config(state))
        self.dc("config", "--quiet")
        try:
            self.firewall(state)
        except (InstallerError, OSError):
            self.run.run(["systemctl", "stop", "ptero-game-databases.service"], check=False)
            self.dc("stop", "database", check=False)
            raise
        try:
            self.service_config()
            self.start(state)
        except (InstallerError, OSError):
            self.run.run(["systemctl", "stop", "ptero-game-databases.service"], check=False)
            self.dc("stop", "database", check=False)
            raise
        state["status"] = "ready"
        write_json(self.state_file, state)
        settings["host"] = state["host"]

    def register(self, panel, settings):
        # Use the exact v2 contract: it validates the connection, encrypts the
        # password and saves the host. No API token or hand-written table insert.
        data = {key: settings[key] for key in ("name", "host", "port", "username", "password", "node_id")}
        if panel.get("method", "docker") == "standalone":
            backend = self.i.native(panel)
            prefix, app_path = ["runuser", "-u", "www-data", "--", backend.php], str(backend.app)
        else:
            prefix = ["docker", "compose", "--project-name", PROJECT, "--file", self.p.compose, "exec", "-T", "panel", "php"]
            app_path = "/app"
        code = (
            f"require {json.dumps(app_path + '/vendor/autoload.php')};"
            f"$app=require {json.dumps(app_path + '/bootstrap/app.php')};"
            "$app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();"
            "$data=json_decode(stream_get_contents(STDIN),true,512,JSON_THROW_ON_ERROR);"
            "$host=Pterodactyl\\Models\\DatabaseHost::query()->where('host',$data['host'])->where('port',$data['port'])->first();"
            "if($host){"
            "if($host->username!==$data['username']||$host->name!==$data['name']||"
            "$host->node_id!==$data['node_id']||"
            "Illuminate\\Support\\Facades\\Crypt::decrypt($host->password)!==$data['password'])"
            "{throw new RuntimeException('An existing database host has different settings.');}"
            "$app->make(Pterodactyl\\Extensions\\DynamicDatabaseConnection::class)->set('dynamic',$host);"
            "Illuminate\\Support\\Facades\\DB::connection('dynamic')->select('SELECT 1 FROM dual');"
            "}else{$host=$app->make(Pterodactyl\\Contracts\\Databases\\CreatesDatabaseHosts::class)->create($data);}"
            "fwrite(STDOUT,json_encode(['id'=>$host->id],JSON_THROW_ON_ERROR));"
        )
        with tempfile.TemporaryFile() as stream:
            stream.write(json.dumps(data).encode())
            stream.seek(0)
            _, output = self.run.run(
                [*prefix, "-r", code],
                input_file=stream,
                label="Registering the game database host in the Panel",
                cwd=app_path if panel.get("method") == "standalone" else None,
            )
        try:
            host_id = json.loads(output)["id"]
            if not isinstance(host_id, int) or host_id < 1:
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise InstallerError("The Panel did not return a valid database host ID.") from exc
        settings.update(registered=True, host_id=host_id)

    def setup(self, panel):
        settings = panel.get("game_database", {"mode": "later"})
        if settings["mode"] == "later":
            return
        self.run.secrets.add(settings["password"])
        try:
            if settings["mode"] == "local":
                self.local(settings)
                # Save the chosen private gateway before registration, so a
                # failed registration resumes with the same credentials/address.
                self.i.save_panel(panel)
            self.register(panel, settings)
            self.i.save_panel(panel)
            self.credentials(panel)
        except (InstallerError, OSError, ValueError):
            self.ui.say(
                (
                    "The Panel is installed. Database host setup is incomplete; rerun Install Panel to retry, or "
                    "configure it from the database menu."
                ),
                "warn",
            )
            raise

    def credentials(self, panel=None):
        if panel is None and not self.report.exists():
            raise InstallerError("No registered game database host is saved.")
        settings = panel.get("game_database", {}) if panel is not None else self.i.load(self.report)
        if not settings.get("registered"):
            raise InstallerError("No registered game database host is saved.")
        report = {key: settings[key] for key in ("engine", "name", "host", "port", "username", "password", "node_id", "host_id")}
        report.update(schema=SCHEMA, owner=PROJECT, mode=settings["mode"], registered=True)
        report["scope"] = "Panel provisioning account; do not share with game users."
        self.run.secrets.add(settings["password"])
        write_json(self.report, report)
        rows = [
            Literal(self.ui.tr("Host: {value}").format(value=settings["host"])),
            Literal(self.ui.tr("Port: {value}").format(value=settings["port"])),
            Literal(self.ui.tr("Username: {value}").format(value=settings["username"])),
            Literal(self.ui.tr("Password: {value}").format(value=settings["password"])),
            Literal(self.ui.tr("Private credentials file: {value}").format(value=self.report)),
            "Provisioning credentials are for the Panel only. Game users receive separate accounts.",
            "Set each server's database limit above zero in the Panel before creating game databases.",
        ]
        if settings["mode"] == "local":
            rows.append(
                "This private database address works only for nodes on this machine. Remote nodes need a reachable private database host."
            )
        self.ui.card("Game database host is ready", rows)
