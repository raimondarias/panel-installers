"""New node, recovery, backup, native SQL and SSH boundary regressions."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from unittest.mock import patch
from test_installer import BaseTest, QuietUI
from ptero.core import InstallerError, write_json
from ptero.phases import Phases
from ptero.nodes import CREATE, create, port_range
from ptero.game_database import GameDatabase
from ptero.native_game_database import NativeGameDatabase
from ptero.remote import AGENT, execute, private_ipv4, ssh
from ptero.backups import ScheduledBackups, checksum, panel_archive, backup_plan


class OperationsTests(BaseTest):
    def test_native_game_sql_refuses_mysql_community_before_package_changes(self):
        backend = NativeGameDatabase(GameDatabase(self.installer))
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            return (0, 'install ok installed') if args[-1] == 'mysql-community-server' else (1, '')

        with (
            patch.object(self.installer, 'check_host', return_value={'ID': 'debian', 'VERSION_CODENAME': 'trixie'}),
            patch.object(self.runner, 'run', side_effect=command),
        ):
            with self.assertRaisesRegex(InstallerError, 'different database engine'):
                backend.dependencies({'engine': 'mariadb'})
        self.assertFalse(any(args[0] == 'apt-get' for args in calls))

    def test_wings_subnet_avoids_panel_compose_and_host_routes(self):
        from ptero.networking import wings_network

        replies = [
            'panel\nhost\nnone\n',
            json.dumps(
                [
                    {'Name': 'host', 'IPAM': {'Config': None}},
                    {'Name': 'none', 'IPAM': None},
                    {'Name': 'panel_default', 'IPAM': {'Config': [{'Subnet': '172.18.0.0/16'}]}},
                ]
            ),
            json.dumps([{'dst': 'default'}, {'dst': '172.30.0.0/24'}]),
        ]
        with patch.object(self.runner, 'run', side_effect=[(0, value) for value in replies]):
            result = wings_network(self.installer, {})
        self.assertEqual(result['docker']['network']['interfaces']['v4']['subnet'], '172.30.1.0/24')

    def test_wings_reuses_existing_bridge_and_preserves_explicit_configuration(self):
        from ptero.networking import wings_network

        replies = [
            'wings',
            json.dumps([{'Name': 'pterodactyl_nw', 'IPAM': {'Config': [{'Subnet': '10.20.0.0/24', 'Gateway': '10.20.0.1'}]}}]),
            '[]',
        ]
        with patch.object(self.runner, 'run', side_effect=[(0, value) for value in replies]):
            config = wings_network(self.installer, {})
        self.assertEqual(config['docker']['network']['interface'], '10.20.0.1')
        with patch.object(self.runner, 'run') as run:
            self.assertIs(wings_network(self.installer, config), config)
        run.assert_not_called()

    def test_apparmor_game_sql_rules_preserve_existing_local_rules(self):
        backend = NativeGameDatabase(GameDatabase(self.installer))
        directory = self.paths.root / 'etc/apparmor.d'
        (directory / 'local').mkdir(parents=True)
        (directory / 'usr.sbin.mysqld').write_text('#include <local/usr.sbin.mysqld>\n')
        local = directory / 'local/usr.sbin.mysqld'
        local.write_text('/custom/site/** r,\n')
        backend.apparmor()
        backend.apparmor()
        text = local.read_text()
        self.assertIn('/custom/site/** r,', text)
        self.assertEqual(text.count('# BEGIN PTERO GAME SQL'), 1)
        self.assertIn(str(backend.data) + '/** rwk,', text)
        self.assertNotIn('/var/lib/mysql/**', text)

    def test_modern_mariadb_apparmor_profile_gets_scoped_local_rules(self):
        backend = NativeGameDatabase(GameDatabase(self.installer))
        directory = self.paths.root / 'etc/apparmor.d'
        directory.mkdir(parents=True)
        (directory / 'mariadbd').write_text('profile mariadbd /usr/sbin/mariadbd {\n include if exists <local/mariadbd>\n}\n')
        backend.apparmor()
        self.assertIn(str(backend.data) + '/** rwk,', (directory / 'local/mariadbd').read_text())
        self.assertTrue(any(args[:2] == ['apparmor_parser', '-r'] for args, _ in self.runner.calls))

    def test_wings_has_one_start_to_avoid_concurrent_bridge_creation(self):
        self.wings()
        with patch('ptero.core.time.sleep'):
            self.installer.start_wings()
        commands = [args for args, _ in self.runner.calls if args[0] == 'systemctl']
        self.assertIn(['systemctl', 'enable', 'wings'], commands)
        self.assertEqual(sum(args[1] == 'restart' or '--now' in args for args in commands), 1)

    def test_failed_phase_persists_failure_without_exception_secrets_and_can_resume(self):
        state = {'version': 'v2'}
        saves = []
        phases = Phases(self.installer, state, lambda: saves.append(json.loads(json.dumps(state))))
        with self.assertRaises(ValueError):
            phases.run('migration', lambda: (_ for _ in ()).throw(ValueError('secret-password')))
        self.assertEqual(state['phases']['migration']['status'], 'failed')
        self.assertNotIn('secret-password', json.dumps(saves))
        phases.run('migration', lambda: None)
        self.assertEqual(state['phases']['migration']['status'], 'done')
        self.assertEqual(saves[0]['phases']['migration']['status'], 'running')

    def test_completed_phase_requires_live_validation_and_matching_version(self):
        state = {'version': 'v2'}
        phases = Phases(self.installer, state, lambda: None)
        calls = []
        phases.run('source', lambda: calls.append(1))
        phases.run('source', lambda: calls.append(2), lambda: True)
        phases.run('source', lambda: calls.append(3), lambda: False)
        state['version'] = 'v3'
        phases.run('source', lambda: calls.append(4), lambda: True)
        self.assertEqual(calls, [1, 3, 4])

    def test_node_port_ranges_reject_system_ports_reverse_and_excessive_ranges(self):
        for value in ('25565', '25565-25575', '1025-2024'):
            self.assertTrue(port_range(value))
        for value in ('22', '1024', '8080-8000', '1025-2025', '1; id', '65536'):
            self.assertFalse(port_range(value))

    def test_node_provisioning_uses_contracts_and_saves_tokens_privately(self):
        self.panel()
        from test_installer import node_payload

        payload = {'node_id': 3, 'configuration': node_payload()}
        with patch('ptero.nodes.execute', return_value=payload) as bridge:
            result = create(self.installer, {'name': 'Node 1'})
        self.assertEqual(result['node_id'], 3)
        self.assertIn('CreatesNodes', bridge.call_args.args[1])
        self.assertIn('CreatesAllocations', bridge.call_args.args[1])
        report = self.paths.base / 'node-provisioning.json'
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertIn('valid-node-token', self.runner.secrets)

    def test_node_php_has_valid_real_parser_syntax(self):
        if not shutil.which('php'):
            self.skipTest('PHP CLI unavailable')
        path = Path(self.temporary.name) / 'node.php'
        path.write_text('<?php\n' + CREATE)
        result = subprocess.run(['php', '-l', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ssh_provisioning_keeps_credentials_in_stdin_and_host_keys_enforced(self):
        target = {'host': 'node.example.com', 'port': 2222, 'identity': '/root/key', 'known_hosts': '/root/known_hosts'}
        original = self.runner.run
        captured = []

        def run(args, **kwargs):
            captured.append(kwargs['input_file'].read())
            return 0, 'PTERO_REMOTE_SUCCESS'

        self.runner.run = run
        execute(self.installer, target, {'action': 'database', 'settings': {'password': 'private-secret'}})
        self.assertIn(b'private-secret', captured[0])
        self.assertIn('StrictHostKeyChecking=yes', ssh(target))
        self.assertNotIn('private-secret', ' '.join(ssh(target)))
        compile(AGENT, 'agent', 'exec')
        self.runner.run = original

    def test_remote_database_requires_private_routed_ipv4(self):
        for value in ('10.2.3.4', '172.20.1.3', '192.168.3.4'):
            self.assertTrue(private_ipv4(value))
        for value in ('8.8.8.8', '127.0.0.1', '169.254.1.2', '0.0.0.0', '::1', 'invalid'):
            self.assertFalse(private_ipv4(value))

    def test_native_sql_uses_own_datadir_socket_and_service(self):
        manager = GameDatabase(self.installer)
        backend = NativeGameDatabase(manager)
        state = {'engine': 'mariadb', 'host': '172.25.0.1', 'port': 3307}
        with patch.object(backend, 'client', return_value=(0, '1')):
            backend.runtime(state)
        config = backend.config.read_text()
        self.assertIn('port=3307', config)
        self.assertIn('game-sql', config)
        self.assertEqual(backend.config.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(backend.config.stat().st_mode & 0o777, 0o640)
        initialization = [
            (args, kwargs) for args, kwargs in self.runner.calls if kwargs.get('label') == 'Initializing isolated native game SQL'
        ]
        self.assertEqual(initialization[0][0][:4], ['runuser', '-u', 'ptero-game-sql', '--'])
        self.assertEqual(initialization[0][1]['cwd'], str(backend.data))
        self.assertNotIn('ptero_panel', config)
        unit = self.paths.root / ('etc/systemd/system/' + backend.unit)
        self.assertIn('ExecStartPre=+', unit.read_text())
        self.assertIn('RuntimeDirectory=ptero-game-sql', unit.read_text())

    def test_native_firewall_remote_allowlist_has_no_public_wildcard_rule(self):
        backend = NativeGameDatabase(GameDatabase(self.installer))
        script = backend.firewall({'host': '10.1.2.3', 'port': 3307, 'allowed_sources': ['10.1.2.4']}).read_text()
        self.assertIn('-s 10.1.2.4', script)
        self.assertIn('-j DROP', script)
        self.assertNotIn('0.0.0.0/0', script)
        self.assertNotIn('-F ', script)

    def configure_backup_profile(self, **changes):
        self.paths.base.mkdir(parents=True, exist_ok=True)
        profile = {
            'schema': 1,
            'owner': 'ptero-installer',
            'repository': '/private-repository',
            'backup_id': 'test-instance',
            'environment': {},
            'keep_daily': 7,
            'keep_weekly': 4,
            **changes,
        }
        write_json(self.paths.base / 'backup-profile.json', profile)
        return ScheduledBackups(self.installer)

    def test_backup_plan_never_asks_to_include_hosted_server_data(self):
        ui = QuietUI(['/private-repository', 'strong-backup-password', 'strong-backup-password'])
        with (
            patch.object(ui, 'choice', return_value='daily'),
            patch.object(ui, 'yes', side_effect=AssertionError('No game inclusion prompt')),
        ):
            profile, password = backup_plan(ui)
        self.assertEqual(profile['scope'], 'panel-wings-config')
        self.assertNotIn('games', profile)
        self.assertEqual(password, 'strong-backup-password')

    def test_profile_migration_replaces_obsolete_scheduled_runtime_modules(self):
        scheduler = self.configure_backup_profile(games=True)
        runtime = self.paths.base / 'runtime/ptero'
        runtime.mkdir(parents=True)
        (runtime / 'obsolete_game_backups.py').write_text('do_not_run = True')
        scheduler.migrate()
        self.assertFalse((runtime / 'obsolete_game_backups.py').exists())
        self.assertIn('panel-wings-config', (runtime / 'backups.py').read_text())
        self.assertEqual(self.runner.calls, [])

    def test_native_panel_backup_excludes_unknown_data_directories(self):
        from ptero.native import NativeBackend

        state = {**self.state, 'method': 'standalone', 'database_engine': 'mariadb', 'php': '8.3', 'redis_password': 'c' * 64}
        self.installer.save_panel(state)
        backend = NativeBackend(self.installer, state)
        backend.app.mkdir(parents=True)
        (backend.app / '.env').write_text('APP_KEY=original-key')
        (self.paths.data / 'volumes').mkdir(parents=True)
        (self.paths.data / 'volumes/world.dat').write_text('PRIVATE WORLD')
        archive = backend.backup()
        panel_archive(archive)
        with tarfile.open(archive) as tar:
            self.assertFalse(any('volumes' in member.name for member in tar))
        self.assertEqual((self.paths.data / 'volumes/world.dat').read_text(), 'PRIVATE WORLD')

    def test_removed_hosted_server_backup_apis_are_not_executable(self):
        actions = self.installer.actions()
        for action in ('backup-game-database', 'restore-game-database', 'backup-games', 'restore-games'):
            self.assertNotIn(action, actions)
        for manager in (GameDatabase(self.installer), NativeGameDatabase(GameDatabase(self.installer))):
            self.assertFalse(hasattr(manager, 'backup'))

    def test_legacy_game_profile_migrates_without_touching_game_files_or_sql(self):
        self.wings()
        scheduler = self.configure_backup_profile(games=True, include_games=True)
        with patch.object(GameDatabase, '__init__', side_effect=AssertionError('SQL must not be opened')):
            scheduler.run_now()
        profile = self.installer.load(scheduler.profile)
        self.assertEqual(profile['scope'], 'panel-wings-config')
        self.assertNotIn('games', profile)
        self.assertNotIn('include_games', profile)
        self.assertTrue(any('Legacy game backup options' in str(message) for message, _ in self.ui.messages))
        commands = [args for args, _ in self.runner.calls]
        self.assertFalse(any(args[0] == 'docker' for args in commands))
        self.assertTrue(any(args[:2] == ['restic', 'backup'] for args in commands))

    def test_legacy_game_backup_history_and_snapshots_are_never_pruned(self):
        self.wings()
        scheduler = self.configure_backup_profile(keep_local=1)
        self.paths.backups.mkdir(parents=True)
        legacy = []
        for name in ('game-files-old.tar.gz', 'game-databases-old.tar.gz'):
            path = self.paths.backups / name
            path.write_bytes(b'original hosted-server backup')
            Path(str(path) + '.sha256').write_text('original checksum')
            legacy.append(str(path))
        write_json(self.paths.base / 'backup-history.json', {'schema': 1, 'owner': 'ptero-installer', 'runs': [legacy]})
        scheduler.run_now()
        for value in legacy:
            self.assertEqual(Path(value).read_bytes(), b'original hosted-server backup')
            self.assertEqual(Path(value + '.sha256').read_text(), 'original checksum')
        forget = next(args for args, _ in self.runner.calls if args[:2] == ['restic', 'forget'])
        self.assertEqual(forget[forget.index('--tag') + 1], 'ptero-installer,panel-wings-config')

    def test_wings_only_bundle_contains_configuration_not_game_volumes(self):
        self.wings()
        source = self.paths.root / 'var/lib/pterodactyl/volumes'
        source.mkdir(parents=True)
        (source / 'world.dat').write_text('PRIVATE WORLD')
        scheduler = self.configure_backup_profile()
        observed = {}
        original = self.runner.run

        def run(args, **kwargs):
            if args[:2] == ['restic', 'backup']:
                bundle = Path(kwargs['cwd'])
                observed.update({p.name: p.read_bytes() for p in bundle.iterdir()})
            return original(args, **kwargs)

        with (
            patch.object(self.runner, 'run', side_effect=run),
            patch.object(self.installer, 'backup_panel', side_effect=AssertionError('No Panel')),
        ):
            scheduler.run_now()
        self.assertIn('wings-state.json', observed)
        self.assertIn('wings-config.yml', observed)
        self.assertIn('bundle-manifest.json', observed)
        self.assertNotIn(b'PRIVATE WORLD', b''.join(observed.values()))
        self.assertEqual((source / 'world.dat').read_text(), 'PRIVATE WORLD')

    def test_wings_only_schedule_retains_three_runs_and_preserves_manual_copies(self):
        self.wings()
        scheduler = self.configure_backup_profile()
        manual = self.installer.backup_wings()
        # Even a manual directory present in legacy history is not scheduler-owned.
        write_json(self.paths.base / 'backup-history.json', {'schema': 1, 'owner': 'ptero-installer', 'runs': [[str(manual)]]})
        for _ in range(5):
            scheduler.run_now()
        scheduled = [path for path in self.paths.backups.glob('wings-*') if (path / 'scheduled-backup.json').exists()]
        self.assertEqual(len(scheduled), 3)
        self.assertTrue((manual / 'wings.json').is_file())
        self.assertTrue((manual / 'config.yml').is_file())
        history = self.installer.load(self.paths.base / 'backup-history.json')
        self.assertEqual(len(history['runs']), 3)
        self.assertEqual({value for run in history['runs'] for value in run}, {str(path) for path in scheduled})

    def test_wings_schedule_retention_refuses_unknown_files_and_links(self):
        self.wings()
        scheduler = self.configure_backup_profile(keep_local=1)
        scheduler.run_now()
        owned = next(self.paths.backups.glob('wings-*'))
        (owned / 'extra-world.dat').write_text('must remain')
        scheduler.run_now()
        self.assertEqual((owned / 'extra-world.dat').read_text(), 'must remain')
        self.assertTrue((owned / 'wings.json').is_file())
        history = self.installer.load(self.paths.base / 'backup-history.json')
        current = Path(history['runs'][-1][0])
        external = Path(self.temporary.name) / 'preserve-external-file'
        external.write_text('external content')
        (current / 'config.yml').unlink()
        (current / 'config.yml').symlink_to(external)
        scheduler.run_now()
        self.assertTrue((current / 'config.yml').is_symlink())
        self.assertTrue((current / 'wings.json').is_file())
        self.assertEqual(external.read_text(), 'external content')

    def test_download_rejects_legacy_hosted_content_before_restic_restore(self):
        scheduler = self.configure_backup_profile()
        for name in ('/game-files-old.tar.gz', '/game-databases-old.tar.gz', '/volumes/world.dat'):
            calls = []

            def run(args, **kwargs):
                calls.append(args)
                if args[1] == 'snapshots':
                    return 0, '[{"id":"snapshot"}]'
                if args[1] == 'ls':
                    return 0, json.dumps({'struct_type': 'node', 'type': 'file', 'path': name})
                raise AssertionError('Restore must not run')

            with (
                self.subTest(name=name),
                patch.object(self.runner, 'run', side_effect=run),
                self.assertRaisesRegex(InstallerError, 'Nothing was downloaded'),
            ):
                scheduler.download()
            self.assertFalse(any(args[1] == 'restore' for args in calls))
        self.assertFalse(self.paths.backups.exists())

    def test_panel_restore_rejects_hosted_content_before_any_extraction(self):
        for name in ('volumes/world.dat', 'data/ibdata1', 'game-files-old.tar.gz'):
            archive = Path(self.temporary.name) / 'legacy.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for path, value in [('manifest.json', b'{"type":"panel-backup","owner":"ptero-installer"}'), (name, b'PRIVATE GAME DATA')]:
                    member = tarfile.TarInfo(path)
                    member.size = len(value)
                    tar.addfile(member, io.BytesIO(value))
            Path(str(archive) + '.sha256').write_text(checksum(archive))
            self.ui.answers = [str(archive)]
            with (
                self.subTest(name=name),
                patch('ptero.core.secure_extract') as extract,
                self.assertRaisesRegex(InstallerError, 'hosted-server content'),
            ):
                self.installer.restore_panel()
            extract.assert_not_called()
        self.assertEqual(self.runner.calls, [])

    def test_panel_backup_excludes_unknown_data_directories(self):
        self.panel()
        (self.paths.data / 'volumes').mkdir()
        (self.paths.data / 'volumes/world.dat').write_text('PRIVATE GAME DATA')
        archive = self.installer.backup_panel()
        panel_archive(archive)
        with tarfile.open(archive) as tar:
            self.assertFalse(any('volumes' in item.name for item in tar))
        self.assertEqual((self.paths.data / 'volumes/world.dat').read_text(), 'PRIVATE GAME DATA')

    def test_diagnostics_invalid_panel_keeps_wings_and_machine_checks_read_only(self):
        self.wings()
        self.paths.state.parent.mkdir(parents=True, exist_ok=True)
        self.paths.state.write_text('invalid JSON')
        before = self.paths.state.read_bytes()
        self.installer.diagnostics()
        messages = ' '.join(str(message) for message, _ in self.ui.messages)
        self.assertIn('Panel state is invalid', messages)
        self.assertIn('Wings is active', messages)
        self.assertIn('Machine overview', messages)
        self.assertEqual(self.paths.state.read_bytes(), before)
        self.assertEqual([args for args, _ in self.runner.calls], [['systemctl', 'is-active', '--quiet', 'wings']])

    def test_diagnostics_failed_panel_backend_does_not_hide_healthy_wings(self):
        self.panel()
        self.wings()
        with patch.object(self.installer, 'dc', side_effect=InstallerError('Panel failed')):
            self.installer.diagnostics()
        messages = ' '.join(str(message) for message, _ in self.ui.messages)
        self.assertIn('Panel failed', messages)
        self.assertIn('Wings is active', messages)

    def test_diagnostics_foreign_panel_is_not_operated_or_adopted(self):
        self.paths.state.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.paths.state, {'schema': 1, 'owner': 'another-installer'})
        self.installer.diagnostics()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(json.loads(self.paths.state.read_text())['owner'], 'another-installer')
        self.assertTrue(any('belongs to another installer' in str(message) for message, _ in self.ui.messages))

    def test_health_report_collects_multiple_failures_instead_of_hiding_them(self):
        self.panel()
        from ptero.health import inspect

        with patch('ptero.health.urllib.request.urlopen', side_effect=OSError('offline')):
            rows = inspect(self.installer)
        self.assertTrue(any('Panel containers' in row for row in rows))
        self.assertTrue(any('Panel HTTP endpoint' in row for row in rows))

    def test_health_reports_pending_migrations_and_queue_failures_in_docker(self):
        self.panel()
        from ptero.health import inspect

        def command(*args, **kwargs):
            if 'migrate:status' in args:
                return 0, '2026_10_23_create_games ........ Pending\n'
            if 'queue:failed' in args:
                return 0, '17 failed jobs\n'
            return 0, '[]'

        with (
            patch.object(self.installer, 'dc', side_effect=command),
            patch('ptero.health.urllib.request.urlopen', side_effect=OSError('offline')),
        ):
            rows = inspect(self.installer)
        self.assertTrue(any('Needs attention: Panel SQL and migrations' in row for row in rows))
        self.assertTrue(any('Needs attention: Queue failures' in row for row in rows))

    def test_menu_shortcuts_remain_unique_with_language_23_reserved(self):
        from ptero.cli import ENTRIES
        from ptero.navigation import inventory, menu_items

        items = menu_items(ENTRIES, inventory(self.installer))
        values = [item.shortcut for item in items]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(next(i for i in items if i.shortcut == '23').value, 'language')

    def test_actual_encrypted_repository_roundtrip(self):
        binary = shutil.which('restic') or (
            '/root/ptero-lab/tools/usr/bin/restic' if Path('/root/ptero-lab/tools/usr/bin/restic').exists() else None
        )
        if not binary:
            self.skipTest('Restic executable unavailable')
        root = Path(self.temporary.name)
        self.panel()
        archive = self.installer.backup_panel()
        repository = root / 'encrypted-repo'
        self.paths.base.mkdir(parents=True, exist_ok=True)
        (self.paths.base / 'backup-password').write_text('test-encryption-password-123!\n')
        profile = {
            'schema': 1,
            'owner': 'ptero-installer',
            'repository': str(repository),
            'backup_id': 'test-instance',
            'environment': {},
            'keep_daily': 7,
            'keep_weekly': 4,
            'games': False,
        }
        write_json(self.paths.base / 'backup-profile.json', profile)
        scheduler = ScheduledBackups(self.installer)
        env = scheduler.environment(profile)
        subprocess.run([binary, 'init'], env={**os.environ, **env}, capture_output=True, check=True)
        self.panel()
        original = self.runner.run

        def run(args, **kwargs):
            if str(args[0]) == 'restic':
                result = subprocess.run(
                    [binary, *map(str, args[1:])],
                    cwd=kwargs.get('cwd'),
                    env={**os.environ, **kwargs.get('env', {})},
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise InstallerError(result.stderr)
                return result.returncode, result.stdout
            return original(args, **kwargs)

        self.runner.run = run
        with patch.object(self.installer, 'backup_panel', return_value=archive):
            scheduler.run_now()
        destination = scheduler.download()
        restored = list(destination.glob('panel-*.tar.gz'))
        self.assertEqual(len(restored), 1)
        self.assertEqual(checksum(restored[0]), checksum(archive))
        self.assertNotIn(b'original-key', b''.join(p.read_bytes() for p in repository.rglob('*') if p.is_file()))

    def test_native_health_waits_for_php_socket_readiness_after_reload(self):
        from unittest.mock import MagicMock
        from ptero.native import NativeBackend

        backend = NativeBackend(self.installer, {**self.state, 'method': 'standalone'})
        responses = [MagicMock(), MagicMock()]
        responses[0].getresponse.return_value.status = 502
        responses[1].getresponse.return_value.status = 200
        with patch('ptero.native.http.client.HTTPConnection', side_effect=responses) as connect, patch('ptero.native.time.sleep') as sleep:
            backend.health()
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(1)
        for response in responses:
            response.close.assert_called_once()

    def test_mysql_authentication_plugin_is_saved_verified_and_repaired(self):
        state = {
            **self.state,
            'method': 'docker',
            'database_engine': 'mysql',
            'panel_image': 'ghcr.io/pterodactyl/panel@sha256:' + 'a' * 64,
        }
        self.installer.save_panel(state)
        self.installer.prepare_mysql_client(state)
        plugin = self.paths.data / 'mysql-plugins/caching_sha2_password.so'
        self.assertEqual(state['mysql_plugin_sha256'], checksum(plugin))

        def count():
            return sum(args[:2] == ['docker', 'run'] for args, _ in self.runner.calls)

        self.assertEqual(count(), 1)
        self.installer.prepare_mysql_client(state)
        self.assertEqual(count(), 1)
        plugin.write_bytes(b'corrupt plugin')
        self.installer.prepare_mysql_client(state)
        self.assertEqual(count(), 2)
        self.assertEqual(state['mysql_plugin_sha256'], checksum(plugin))
        volumes = self.installer.compose_config(state)['services']['panel']['volumes']
        self.assertTrue(any(v.get('target') == '/usr/lib/mariadb/plugin' and v['read_only'] for v in volumes))
