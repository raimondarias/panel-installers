"""Verified Panel archives and encrypted Panel/Wings configuration copies."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import tarfile
import tempfile
from .common import InstallerError, atomic_write, file_sha256, write_json


checksum = file_sha256


def verified(path):
    path = Path(path)
    sidecar = Path(str(path) + '.sha256')
    if not path.is_file() or not sidecar.is_file():
        raise InstallerError('A backup archive and its SHA-256 sidecar are required.')
    expected = sidecar.read_text().split()[0] if sidecar.read_text().split() else ''
    if not re.fullmatch('[0-9a-f]{64}', expected) or checksum(path) != expected:
        raise InstallerError('Backup checksum does not match; nothing was restored.')
    return path


# Only these installer-owned Panel paths belong in an installation backup.
PANEL_DATA = frozenset(('var', 'redis', 'nginx', 'certs', 'extensions', 'extension-assets',
                        'mysql-client.cnf', 'mysql-plugins'))
SCOPE = 'panel-wings-config'


def panel_archive(path):
    """Inspect headers and the manifest before extracting any Panel archive."""
    try:
        with tarfile.open(path, 'r:*') as archive:
            members = archive.getmembers()
            for member in members:
                parts = PurePosixPath(member.name).parts
                if (not parts or member.name.startswith('/') or '..' in parts
                        or parts[0] not in ('manifest.json', 'state.json', 'database.sql', 'app', 'redis', 'data')
                        or (parts[0] == 'data' and len(parts) > 1 and parts[1] not in PANEL_DATA)):
                    raise InstallerError('Backups may contain only the Panel and Wings configuration; hosted-server content is not supported.')
            manifests = [m for m in members if m.name == 'manifest.json']
            if len(manifests) != 1 or not manifests[0].isfile() or manifests[0].size > 16384:
                raise InstallerError('This is not a valid Panel backup.')
            manifest = json.load(archive.extractfile(manifests[0]))
            if manifest.get('type') != 'panel-backup' or manifest.get('owner') != 'ptero-installer':
                raise InstallerError('This is not a valid Panel backup.')
    except (tarfile.TarError, OSError, ValueError, AttributeError) as exc:
        raise InstallerError('This is not a valid Panel backup.') from exc
    return Path(path)


def bundle_file(path):
    """Allow only known configuration files and checked Panel archive names."""
    parts = PurePosixPath(path).parts
    if '..' in parts:
        return False
    name = str(PurePosixPath(path)).lstrip('/')
    return (name in ('bundle-manifest.json', 'wings-state.json', 'wings-config.yml',
                     'wings-service.conf', 'wings-compose.json')
            or bool(re.fullmatch(r'panel-[A-Za-z0-9_-]+\.tar\.gz(?:\.sha256)?', name)))


def backup_plan(ui):
    ui.say('Backups include only the Panel and Wings configuration. Hosted-server files and databases are excluded.')
    repository = ui.ask('Restic repository (local path, sftp: or s3:)', validator=lambda v: 1 <= len(v) <= 1024 and not any(ord(c) < 32 for c in v) and (v.startswith(('/', 's3:https://', 'sftp:'))))
    password = ui.ask('Backup encryption password (16+ characters)', secret=True, validator=lambda v: len(v) >= 16 and '\n' not in v)
    ui.ask('Confirm backup encryption password', secret=True, validator=lambda v: v == password)
    profile = {'schema': 1, 'backup_scope_version': 2, 'owner': 'ptero-installer', 'scope': SCOPE, 'repository': repository, 'environment': {}, 'keep_daily': 7,
               'keep_weekly': 4,
               'backup_id': 'ptero-' + secrets.token_hex(8), 'calendar': ui.choice('Backup schedule', [('daily', 'Daily', ''), ('weekly', 'Weekly', ''), ('hourly', 'Hourly', '')])}
    if repository.startswith('s3:'):
        profile['environment']['AWS_ACCESS_KEY_ID'] = ui.ask('S3 access key', secret=True)
        profile['environment']['AWS_SECRET_ACCESS_KEY'] = ui.ask('S3 secret key', secret=True)
    if repository.startswith('sftp:'):
        ui.say('Prepare SSH keys and a verified known_hosts entry for the backup destination first.')
    return profile, password


class ScheduledBackups:
    def __init__(self, i):
        self.i, self.p, self.ui, self.run = i, i.p, i.ui, i.run
        self.profile = self.p.base / 'backup-profile.json'

    def environment(self, profile):
        env = {'RESTIC_REPOSITORY': profile['repository'], 'RESTIC_PASSWORD_FILE': str(self.p.base / 'backup-password')}
        env.update(profile.get('environment', {}))
        self.run.secrets.update(env.values())
        password = self.p.base / 'backup-password'
        if password.is_file():
            self.run.secrets.add(password.read_text().strip())
        return env

    def configure(self):
        profile, password = backup_plan(self.ui)
        self.ui.confirm('CONFIGURE BACKUPS')
        if self.profile.exists():
            profile['backup_id'] = self.i.load(self.profile)['backup_id']
        if not shutil.which('restic'):
            self.run.run(['apt-get', 'update'])
            self.run.run(['apt-get', 'install', '-y', 'restic'])
        self.run.secrets.add(password)
        env = self.environment(profile)
        candidate = self.p.base / ('backup-password-' + secrets.token_hex(8))
        try:
            atomic_write(candidate, password + '\n')
            env['RESTIC_PASSWORD_FILE'] = str(candidate)
            if self.run.run(['restic', 'snapshots', '--json'], env=env, check=False)[0]:
                self.run.run(['restic', 'init'], env=env, label='Initializing encrypted backup repository')
            candidate.replace(self.p.base / 'backup-password')
        finally:
            candidate.unlink(missing_ok=True)
        write_json(self.profile, profile)
        runtime = self.sync_runtime()
        command = shlex.join([str(self.p.root / 'usr/bin/python3'), str(runtime / 'installer.py'), '--language', self.ui.language, 'backup-run'])
        atomic_write(self.p.root / 'etc/systemd/system/ptero-backup.service', '[Unit]\nDescription=Pterodactyl Installer encrypted backups\nAfter=network-online.target\n[Service]\nType=oneshot\nUMask=0077\nNice=10\nIOSchedulingClass=idle\n' + f'ExecStart={command}\n', 0o644)
        atomic_write(self.p.root / 'etc/systemd/system/ptero-backup.timer', '[Unit]\nDescription=Pterodactyl Installer scheduled backups\n[Timer]\n' + f"OnCalendar={profile['calendar']}\nPersistent=true\nRandomizedDelaySec=300\n[Install]\nWantedBy=timers.target\n", 0o644)
        self.run.run(['systemctl', 'daemon-reload'])
        self.run.run(['systemctl', 'enable', '--now', 'ptero-backup.timer'])

    def sync_runtime(self):
        # Replace old modules as well as changed ones: obsolete game-data backup
        # implementations must not remain reachable by existing timer jobs.
        runtime = self.p.base / 'runtime'
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = Path(__file__).resolve().parent
        destination = runtime / 'ptero'
        if source.resolve() != destination.resolve():
            with tempfile.TemporaryDirectory(prefix='runtime-', dir=self.p.base) as temporary:
                staged = Path(temporary) / 'ptero'
                shutil.copytree(source, staged, ignore=shutil.ignore_patterns('__pycache__'))
                if destination.is_symlink():
                    raise InstallerError('The scheduled backup runtime cannot be a symlink.')
                previous = runtime / 'ptero.previous'
                if previous.exists():
                    shutil.rmtree(previous)
                if destination.exists():
                    destination.replace(previous)
                try:
                    staged.replace(destination)
                except BaseException:
                    if previous.exists(): previous.replace(destination)
                    raise
                if previous.exists(): shutil.rmtree(previous)
        atomic_write(runtime / 'installer.py', 'from ptero.cli import main\nraise SystemExit(main())\n')
        return runtime

    def migrate(self):
        if not self.profile.exists():
            return
        profile = self.load_profile()
        self.sync_runtime()
        return profile

    def load_profile(self):
        profile = self.i.load(self.profile)
        if profile.get('games') or profile.get('include_games'):
            self.ui.say('Legacy game backup options were removed. This profile now backs up only the Panel and Wings configuration.', 'warn')
        if profile.get('scope') != SCOPE or 'games' in profile or 'include_games' in profile:
            profile.pop('games', None)
            profile.pop('include_games', None)
            profile.update(backup_scope_version=2, scope=SCOPE)
            write_json(self.profile, profile)
        return profile

    def run_now(self):
        profile = self.migrate()
        env = self.environment(profile)
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='bundle-', dir=self.p.backups) as temporary:
            bundle = Path(temporary)
            artifacts = []
            local_artifacts = []
            if self.p.state.exists():
                artifacts.append(self.i.backup_panel())
                local_artifacts.extend(artifacts)
            if self.p.wings_state.exists():
                source = self.i.backup_wings()
                write_json(source / 'scheduled-backup.json', {'schema': 1, 'owner': 'ptero-installer',
                           'scope': SCOPE, 'backup_id': profile['backup_id']})
                local_artifacts.append(source)
                for filename, target in (('wings.json', 'wings-state.json'), ('config.yml', 'wings-config.yml'),
                                         ('wings.service', 'wings-service.conf'), ('wings-compose.json', 'wings-compose.json')):
                    if (source / filename).is_file():
                        shutil.copy2(source / filename, bundle / target)
            if not artifacts and not any(bundle.iterdir()):
                raise InstallerError('There are no managed resources to back up.')
            for archive in artifacts:
                panel_archive(verified(archive))
                shutil.copy2(archive, bundle / archive.name)
                shutil.copy2(str(archive) + '.sha256', bundle / (archive.name + '.sha256'))
            write_json(bundle / 'bundle-manifest.json', {'schema': 1, 'backup_scope_version': 2, 'owner': 'ptero-installer', 'scope': SCOPE})
            self.run.run(['restic', 'backup', '--tag', 'ptero-installer', '--tag', SCOPE, '--host', profile['backup_id'], '.'], env=env,
                         label='Uploading encrypted backup bundle', timeout=14400, cwd=str(bundle))
        # Retention only touches this installer's tagged snapshots.
        self.run.run(['restic', 'forget', '--tag', 'ptero-installer,' + SCOPE, '--host', profile['backup_id'], '--keep-daily', str(profile['keep_daily']), '--keep-weekly', str(profile['keep_weekly']), '--prune'], env=env, timeout=14400)
        self.run.run(['restic', 'check'], env=env, label='Checking encrypted backup repository', timeout=14400)

        history_path = self.p.base / 'backup-history.json'
        history = self.i.load(history_path) if history_path.exists() else {'schema': 1, 'owner': 'ptero-installer', 'runs': []}
        history['runs'].append([str(path) for path in local_artifacts])
        while len(history['runs']) > profile.get('keep_local', 3):
            for value in history['runs'].pop(0):
                path = Path(value)
                if (re.fullmatch(r'panel-[A-Za-z0-9_-]+\.tar\.gz', path.name)
                        and path.parent.resolve() == self.p.backups.resolve() and not path.is_symlink()):
                    path.unlink(missing_ok=True)
                    Path(str(path) + '.sha256').unlink(missing_ok=True)
                elif (re.fullmatch(r'wings-[A-Za-z0-9_-]+', path.name)
                      and path.parent.resolve() == self.p.backups.resolve() and not path.is_symlink()
                      and path.is_dir()):
                    # A manual Wings copy has no scheduler ownership marker.
                    # Never recursively remove arbitrary or historical contents.
                    allowed = {'wings.json', 'config.yml', 'wings.service', 'wings-compose.json', 'scheduled-backup.json'}
                    entries = list(path.iterdir())
                    if any(item.name not in allowed or item.is_symlink() or not item.is_file() for item in entries):
                        continue
                    marker = path / 'scheduled-backup.json'
                    if not marker.is_file():
                        continue
                    try:
                        ownership = self.i.load(marker)
                    except InstallerError:
                        continue
                    if ownership.get('scope') != SCOPE or ownership.get('backup_id') != profile['backup_id']:
                        continue
                    for item in entries:
                        item.unlink()
                    path.rmdir()
        write_json(history_path, history)

    def download(self):
        profile = self.migrate()
        env = self.environment(profile)
        _, output = self.run.run(['restic', 'snapshots', '--json', '--tag', 'ptero-installer', '--host', profile['backup_id']], env=env)
        snapshots = json.loads(output)
        if not snapshots:
            raise InstallerError('No managed snapshots exist in this repository.')
        self.ui.card('Available backup snapshots', [str(item['id']) + ' ' + str(item.get('time', '')) for item in snapshots])
        snapshot = self.ui.ask('Snapshot ID', snapshots[-1]['id'], lambda v: any(s['id'] == v for s in snapshots))
        # List the encrypted snapshot before restoring even into a temporary
        # directory. Legacy snapshots with hosted-server content are rejected.
        _, listing = self.run.run(['restic', 'ls', '--json', snapshot], env=env)
        found = []
        for line in listing.splitlines():
            node = json.loads(line)
            if node.get('struct_type') != 'node':
                continue
            path = node.get('path', '')
            if node.get('type') == 'dir' and path in ('', '/', '.'):
                continue
            if node.get('type') != 'file' or not bundle_file(path):
                raise InstallerError('This snapshot contains unsupported hosted-server content. Nothing was downloaded.')
            found.append(path)
        if not found:
            raise InstallerError('The snapshot contains no supported Panel or Wings configuration files.')
        self.ui.confirm('DOWNLOAD BACKUP')
        self.p.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = Path(tempfile.mkdtemp(prefix='download-', dir=self.p.backups))
        try:
            self.run.run(['restic', 'restore', snapshot, '--target', destination], env=env, timeout=14400)
            for path in destination.iterdir():
                if path.is_symlink() or not path.is_file() or not bundle_file(path.name):
                    raise InstallerError('This snapshot contains unsupported hosted-server content. Nothing was downloaded.')
            for archive in destination.glob('*.tar.gz'):
                panel_archive(verified(archive))
        except BaseException:
            shutil.rmtree(destination)
            raise
        self.ui.say(self.ui.tr('Downloaded backup directory: {value}').format(value=destination), 'ok')
        return destination
