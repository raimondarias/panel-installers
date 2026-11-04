#!/usr/bin/env python3
"""Opt-in REAL deployment on a disposable systemd VM. Never run on a shared server."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ptero.core import Installer, PANEL_VERSION, InstallerError, write_json
from ptero.ui import UI


class LabUI(UI):
    def __init__(self):
        super().__init__(False, 'en', plain=True)
    def confirm(self, phrase):
        self.say('Disposable-VM scenario approved: ' + self.tr(phrase))
    def card(self, title, rows):
        if title == 'Game database host is ready':
            self.say('Game SQL credentials are stored privately in the disposable VM.')
            return
        return super().card(title, rows)
    def ask(self, label, default=None, validator=None, secret=False):
        if label == "Absolute path to the .tar.gz backup" and getattr(self, 'restore_archive', None):
            return str(self.restore_archive)
        if default is not None and (not validator or validator(str(default))):
            return str(default)
        return super().ask(label,default,validator,secret)
    def choice(self, label, options, default=0):
        return options[default][0]
    def yes(self, label, default=False):
        return getattr(self, 'purge', False) and (label.startswith('Also delete the Panel') or label.startswith('¿Borrar también'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable', action='store_true', required=True)
    parser.add_argument('--method', choices=['standalone', 'docker'], default='standalone')
    parser.add_argument('--engine', choices=['mariadb', 'mysql'], default='mariadb')
    parser.add_argument('--url', default='http://127.0.0.1')
    parser.add_argument('--external-tls', action='store_true')
    parser.add_argument('--full', action='store_true', help='Also run real Wings, game SQL and game server smoke tests')
    parser.add_argument('--cycle', action='store_true', help='Also exercise a real service restart, update redeployment, purge and restore')
    args = parser.parse_args()
    token_file=Path('/root/.ptero-github-token')
    if token_file.exists():
        os.environ['PTERO_GITHUB_TOKEN']=token_file.read_text().strip()
    if os.geteuid() != 0:
        parser.error('Run as root inside the disposable VM.')
    i = Installer(ui=LabUI())
    # Detect common evidence of a shared machine before any package changes.
    if not i.p.state.exists():
        for directory in ('/etc/nginx/sites-enabled', '/var/www'):
            path = Path(directory)
            if path.exists() and any(p.name not in ('default', 'html') for p in path.iterdir()):
                raise InstallerError('This machine has existing sites; use an empty disposable VM.')
        state = {'schema': 1, 'owner': 'ptero-installer', 'status': 'installing', 'method': args.method,
                 'database_engine': args.engine, 'version': PANEL_VERSION, 'access': 'domain',
                 'tls': args.url.startswith('https://') and not args.external_tls, 'http_port': 80,
                 'https_port': 443, 'bind': '0.0.0.0', 'timezone': 'UTC', 'db_password': secrets.token_hex(32),
                 'redis_password': secrets.token_hex(32), 'admin_created': False, 'php': '8.3', 'language': 'en',
                 'url': args.url, 'email': 'admin@installer.xerosrv.com',
                 'admin': {'email': 'admin@installer.xerosrv.com', 'username': 'admin', 'name_first': 'Installer', 'name_last': 'Test', 'root_admin': True},
                 'game_database': {'mode': 'later'}}
        i.check_plan(state)
        i.new_panel = lambda: state
        password = secrets.token_urlsafe(32)
        write_json(Path('/root/ptero-integration-secrets.json'), {'administrator_email': state['email'], 'administrator_password': password})
    else:
        password = json.loads(Path('/root/ptero-integration-secrets.json').read_text())['administrator_password']
    i.admin_password = password
    i.run.secrets.add(password)
    i.install_panel()
    i.diagnostics()
    i.health_report()
    archive = i.backup_panel()
    print('INTEGRATION_PANEL_BACKUP=' + str(archive), flush=True)
    if args.cycle:
        state=i.panel_state()
        key_path=i.native(state).app/'.env' if state.get('method')=='standalone' else i.p.data/'var/.env'
        original_keys=[line for line in key_path.read_text().splitlines() if line.startswith(('APP_KEY=','HASHIDS_SALT='))]
        i.restart()
        if state.get('method')=='standalone':
            i.native(state).update(i.release('panel',state['version']))
        else:
            i.dc('pull',label='Checking the pinned update images')
            i.dc('run','--rm','-T','panel','php','artisan','migrate','--seed','--force','--no-interaction')
            i.dc('up','-d','--wait','--wait-timeout','360','panel')
        archive=i.backup_panel()
        i.ui.purge=True
        i.uninstall_panel(confirmed=True)
        i.ui.restore_archive=archive
        i.restore_panel()
        restored_keys=[line for line in key_path.read_text().splitlines() if line.startswith(('APP_KEY=','HASHIDS_SALT='))]
        if original_keys!=restored_keys:raise InstallerError('Restoration changed original application keys')
        i.diagnostics()
        print('INTEGRATION_UPDATE_PURGE_RESTORE_PASSED',flush=True)
    if args.full:
        from full_smoke import run as full_smoke
        full_smoke(i)
    results=Path('test-results');results.mkdir(exist_ok=True)
    (results/'integration-result.json').write_text(json.dumps({'panel': 'passed', 'method': args.method, 'engine': args.engine,
        'url': args.url, 'backup': str(archive), 'update_purge_restore': 'passed' if args.cycle else 'not_run',
        'node_wings_sql_provisioning_panel_wings_backup_preserves_game': 'passed' if args.full else 'not_run',
        'backup_scope': 'panel-wings-config',
        'game_sql_method': ('docker' if args.method == 'docker' else 'standalone') if args.full else 'not_run',
        'wings_method': 'standalone' if args.full else 'not_run',
        'secrets': 'stored privately in guest only'},indent=2)+'\n')
    print('INTEGRATION_PANEL_READY', flush=True)


if __name__ == '__main__':
    main()
