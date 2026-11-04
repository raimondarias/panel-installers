"""Separate native SQL instance; never creates privileged users in Panel SQL."""
import hashlib
import ipaddress
import json
import secrets
import re
import shlex
import tempfile
import time
from .core import InstallerError, atomic_write, write_json


class NativeGameDatabase:
    unit = 'ptero-game-sql-native.service'
    user = 'ptero-game-sql'

    def __init__(self, manager):
        self.manager, self.i, self.p = manager, manager.i, manager.p
        self.run, self.ui = manager.run, manager.ui
        self.data = self.p.root / 'var/lib/pterodactyl-installer/game-sql'
        self.config = self.p.root / 'etc/pterodactyl-installer/game-sql.cnf'
        self.socket = self.p.root / 'run/ptero-game-sql/mysql.sock'

    def client(self, state, sql, output=None):
        name = 'mysql' if state['engine'] == 'mysql' else 'mariadb'
        with tempfile.TemporaryFile() as stream:
            stream.write(sql.encode()); stream.seek(0)
            return self.run.run([name, '--no-defaults', '--socket=' + str(self.socket), '--user=root', '--batch', '--skip-column-names'],
                                input_file=stream, output=output, label='Configuring isolated native game SQL')

    def dependencies(self, state):
        release = self.i.check_host()
        mysql = state['engine'] == 'mysql'
        selected = 'mysql-community-server' if mysql and release['ID'] == 'debian' else 'mysql-server' if mysql else 'mariadb-server'
        _, installed = self.run.run(['dpkg-query', '-W', '-f=${Status}', selected], check=False)
        had_default_instance = 'install ok installed' in installed
        opposites = ('mariadb-server',) if mysql else ('mysql-server', 'mysql-community-server')
        for package in opposites:
            code, text = self.run.run(['dpkg-query', '-W', '-f=${Status}', package], check=False)
            if code == 0 and 'install ok installed' in text:
                raise InstallerError('A different database engine is already installed. Choose the matching engine or Docker.')
        self.run.run(['apt-get', 'update'])
        if mysql and release['ID'] == 'debian':
            import platform
            if platform.machine() != 'x86_64':
                raise InstallerError('Native MySQL on Debian requires amd64. Choose MariaDB or Docker on ARM64.')
            from .native import NativeBackend
            NativeBackend(self.i, {'database_engine': 'mysql'}).repository('mysql', 'https://repo.mysql.com/RPM-GPG-KEY-mysql-2025',
                f"https://repo.mysql.com/apt/debian/ {release['VERSION_CODENAME']} mysql-8.4-lts")
            self.run.run(['apt-get', 'update'])
            packages = ['mysql-community-server', 'mysql-community-client']
        else:
            packages = ['mysql-server', 'mysql-client'] if mysql else ['mariadb-server', 'mariadb-client']
        self.run.run(['apt-get', 'install', '-y', 'iptables', *packages], label='Installing native game SQL packages')
        if not had_default_instance:
            self.run.run(['systemctl', 'disable', '--now', 'mysql' if mysql else 'mariadb'])

    def firewall(self, state):
        host = str(ipaddress.IPv4Address(state['host']))
        port = int(state['port'])
        identity = json.dumps([host, port, sorted(state.get('allowed_sources', []))])
        chain = 'PTERO-SQL-' + hashlib.sha256(identity.encode()).hexdigest()[:10]
        script = f'#!/bin/sh\nset -eu\niptables -w -N {chain} 2>/dev/null || iptables -w -S {chain} >/dev/null\n'
        for address in state.get('allowed_sources', []):
            address = str(ipaddress.IPv4Address(address))
            script += f'iptables -w -C PTERO-NATIVE-SQL -s {address} -j RETURN 2>/dev/null || iptables -w -A PTERO-NATIVE-SQL -s {address} -j RETURN\n'
        if not state.get('allowed_sources'):
            for interface in ('lo', 'docker0', 'br-+', 'pterodactyl0'):
                script += f'iptables -w -C PTERO-NATIVE-SQL -i {shlex.quote(interface)} -j RETURN 2>/dev/null || iptables -w -A PTERO-NATIVE-SQL -i {shlex.quote(interface)} -j RETURN\n'
        script += 'iptables -w -C PTERO-NATIVE-SQL -j DROP 2>/dev/null || iptables -w -A PTERO-NATIVE-SQL -j DROP\n'
        script += f'iptables -w -C INPUT -d {host} -p tcp --dport {port} -j PTERO-NATIVE-SQL 2>/dev/null || iptables -w -I INPUT 1 -d {host} -p tcp --dport {port} -j PTERO-NATIVE-SQL\n'
        script = script.replace('PTERO-NATIVE-SQL', chain)
        path = self.manager.base / 'native-firewall.sh'
        atomic_write(path, script, 0o700)
        return path

    def runtime(self, state):
        if state.get('os_user', 'mysql') != self.user:
            self.run.run(['systemctl', 'stop', self.unit], check=False)
        _, account = self.run.run(['getent', 'passwd', self.user], check=False)
        if not account.strip():
            self.run.run(['useradd', '--system', '--user-group', '--home-dir', str(self.data), '--shell', '/usr/sbin/nologin', self.user])
        elif account.split(':')[2] == '0':
            raise InstallerError('The reserved game SQL service account cannot be root.')
        self.data.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self.data.parent.chmod(0o755)
        self.data.mkdir(exist_ok=True, mode=0o750)
        self.run.run(['chown', '-R', self.user + ':' + self.user, self.data])
        binary = '/usr/sbin/mysqld' if state['engine'] == 'mysql' else '/usr/sbin/mariadbd'
        self.config.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self.config.parent.chmod(0o755)
        atomic_write(self.config, '[mysqld]\n' + f"datadir={self.data}\nsocket={self.socket}\npid-file={self.socket.parent}/mysql.pid\nbind-address={state['host']}\nport={int(state['port'])}\n" + 'skip-name-resolve\n' + ('mysqlx=OFF\n' if state['engine'] == 'mysql' else ''), 0o640)
        self.run.run(['chown', 'root:' + self.user, self.config])
        self.apparmor()
        if not (self.data / 'mysql').exists():
            if any(self.data.iterdir()):
                raise InstallerError('Native game SQL data is incomplete; recover this SQL instance outside the installer before continuing.')
            args = [binary, '--no-defaults', '--initialize-insecure', '--user=' + self.user, '--datadir=' + str(self.data)] if state['engine'] == 'mysql' else [
                'mariadb-install-db', '--no-defaults', '--user=' + self.user, '--datadir=' + str(self.data), '--auth-root-authentication-method=socket', '--skip-test-db']
            # Initialize under the same account as the service. Modern MariaDB
            # AppArmor profiles intentionally deny root's DAC/setuid bypasses.
            self.run.run(['runuser', '-u', self.user, '--', *args], cwd=str(self.data),
                         label='Initializing isolated native game SQL')
        firewall = self.firewall(state)
        atomic_write(self.p.root / ('etc/systemd/system/' + self.unit),
            '[Unit]\nDescription=Pterodactyl Installer: isolated native game SQL\nAfter=network-online.target\nWants=network-online.target\n'
            + ('Requires=docker.service\nAfter=docker.service\n' if not state.get('allowed_sources') else '') +
            f'[Service]\nUser={self.user}\nGroup={self.user}\nRuntimeDirectory=ptero-game-sql\nRuntimeDirectoryMode=0750\n'
            f'ExecStartPre=+/bin/sh {shlex.quote(str(firewall))}\nExecStart={binary} --defaults-file={self.config}\n'
            'Restart=on-failure\nTimeoutStartSec=180\nTimeoutStopSec=180\nLimitNOFILE=65536\n[Install]\nWantedBy=multi-user.target\n', 0o644)
        self.run.run(['systemctl', 'daemon-reload'])
        self.run.run(['systemctl', 'enable', '--now', self.unit])
        with self.ui.busy('Checking isolated native SQL readiness'):
            for attempt in range(60):
                try:
                    self.client(state, 'SELECT 1;')
                    break
                except InstallerError:
                    if attempt == 59:
                        raise
                    time.sleep(1)
        state['os_user'] = self.user

    def apparmor(self):
        directory = self.p.root / 'etc/apparmor.d'
        for name in ('usr.sbin.mysqld', 'usr.sbin.mariadbd', 'mysqld', 'mariadbd'):
            profile = directory / name
            if not profile.is_file() or ('local/' + name) not in profile.read_text():
                continue
            local = directory / 'local' / name
            previous = local.read_text() if local.exists() else ''
            rules = ('# BEGIN PTERO GAME SQL\n'
                     f'{self.data.parent}/ r,\n{self.data}/ r,\n{self.data}/** rwk,\n'
                     f'{self.config} r,\n{self.socket.parent}/ r,\n{self.socket.parent}/** rwk,\n'
                     '# END PTERO GAME SQL\n')
            previous = re.sub(r'# BEGIN PTERO GAME SQL\n.*?# END PTERO GAME SQL\n?', '', previous, flags=re.S)
            atomic_write(local, previous.rstrip() + '\n' + rules, 0o644)
            # A newly created optional include may be absent from a cached policy.
            self.run.run(['apparmor_parser', '-r', '-T', profile], label='Loading the isolated SQL access rules')

    def install(self, settings):
        if self.manager.state_file.exists():
            state = self.manager.load()
            if (state.get('method') != 'standalone' or any(state[k] != settings[k] for k in ('engine', 'port', 'username', 'password'))
                    or settings.get('host', state['host']) != state['host']
                    or settings.get('allowed_sources', []) != state.get('allowed_sources', [])):
                raise InstallerError('An existing game SQL instance has different settings.')
        else:
            if self.data.exists() and any(self.data.iterdir()):
                raise InstallerError('Unmanaged native game SQL files exist; nothing was overwritten.')
            if (self.p.root / ('etc/systemd/system/' + self.unit)).exists():
                raise InstallerError('An unmanaged native SQL service already exists.')
            host = settings.get('host')
            if not host:
                self.i.docker()
                host = self.manager.network(create=True)
            self.i.port_free(host, settings['port'])
            state = {**settings, 'schema': 1, 'owner': 'ptero-installer', 'status': 'installing', 'method': 'standalone',
                     'host': host, 'root_password': secrets.token_hex(32)}
            if not settings.get('host'):
                state['network_config'] = self.manager.network_config()
            self.manager.base.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(self.manager.state_file, state)
        self.run.secrets.update((state['password'], state['root_password']))
        self.dependencies(state)
        self.runtime(state)
        # Idempotent per-source provisioning; always separate from the Panel DB.
        quote = lambda v: "'" + v.replace("'", "''") + "'"
        sql = "SET SESSION sql_mode='NO_BACKSLASH_ESCAPES';\n"
        for source in state.get('allowed_sources') or ['%']:
            account = quote(state['username']) + '@' + quote(source)
            sql += f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY {quote(state['password'])};\nALTER USER {account} IDENTIFIED BY {quote(state['password'])};\nGRANT ALL PRIVILEGES ON *.* TO {account} WITH GRANT OPTION;\n"
        self.client(state, sql)
        _, version = self.run.run(['mysqld' if state['engine'] == 'mysql' else 'mariadbd', '--version'])
        state.update(status='ready', server_version=version.strip())
        write_json(self.manager.state_file, state)
        settings.update(host=state['host'], method='standalone')
