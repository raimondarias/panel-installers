"""Real node, game SQL provisioning and Panel/Wings-only backup smoke in disposable VMs."""

from pathlib import Path
import secrets
import socket
import tempfile
import time
from ptero.core import InstallerError
from ptero.nodes import create
from ptero.panel_bridge import execute
from ptero.game_database import GameDatabase
from ptero.native_game_database import NativeGameDatabase
from ptero.backups import verified


def run(i):
    address = socket.gethostbyname(socket.gethostname())
    if address.startswith('127.'):
        _, routes = i.run.run(['hostname', '-I'])
        address = routes.split()[0]
    # Full-smoke CI uses guest HTTP. Public TLS is exercised by the domain lab.
    i.install_wings()
    data = {
        'location': 'integration',
        'name': 'Integration node',
        'fqdn': address,
        'scheme': 'http',
        'behind_proxy': False,
        'memory': 256,
        'disk': 2048,
        'memory_overallocate': 0,
        'disk_overallocate': 0,
        'public': True,
        'upload_size': 100,
        'daemon_base': '/var/lib/pterodactyl/volumes',
        'daemon_listen': 8080,
        'daemon_sftp': 2022,
        'allocation_ip': address,
        'allocation_ports': ['25565-25575'],
    }
    result = create(i, data, link=True)
    panel = i.panel_state()
    method = 'docker' if panel.get('method') == 'docker' else 'standalone'
    settings = {
        'mode': 'local',
        'method': method,
        'engine': panel['database_engine'],
        'name': 'Integration SQL',
        'port': 3307,
        'username': 'ptero_games',
        'password': secrets.token_hex(32),
        'node_id': result['node_id'],
        'registered': False,
    }
    panel['game_database'] = settings
    i.save_panel(panel)
    sql = GameDatabase(i)
    sql.setup(panel)
    state = sql.load()
    backend = NativeGameDatabase(sql) if method == 'standalone' else None

    def query(statement):
        if backend is not None:
            return backend.client(state, statement)
        mysql = state['engine'] == 'mysql'
        variable = 'MYSQL_ROOT_PASSWORD' if mysql else 'MARIADB_ROOT_PASSWORD'
        client = 'mysql' if mysql else 'mariadb'
        command = 'MYSQL_PWD="$' + variable + '" exec ' + client + ' --user=root --batch --skip-column-names'
        with tempfile.TemporaryFile() as stream:
            stream.write(statement.encode())
            stream.seek(0)
            return sql.dc('exec', '-T', 'database', 'sh', '-c', command, input_file=stream)

    query(
        (
            "CREATE DATABASE installer_smoke; CREATE TABLE installer_smoke.world(id INT PRIMARY KEY); INSERT "
            "INTO installer_smoke.world VALUES(42);"
        )
    )
    _, rows = query('SELECT id FROM installer_smoke.world;')
    if rows.strip() != '42':
        raise InstallerError('Game SQL provisioning did not preserve inserted data')
    source = Path(__file__).resolve().parent
    i.run.run(['docker', 'build', '-t', 'ptero-integration-minetest:1', '-f', source / 'game.Dockerfile', source], timeout=900)
    code = r'''
$egg=$app->make(Pterodactyl\Contracts\Eggs\CreatesEggs::class)->create([
'name'=>'Integration Minetest','docker_images'=>'ptero-integration-minetest:1',
'startup'=>'/usr/lib/minetest/minetestserver --world /home/container/world --gameid minetest --port {{SERVER_PORT}}',
'config_stop'=>'^C','config_startup'=>json_encode(['done'=>'Server for gameid=']),'config_logs'=>'{}','config_files'=>'{}']);
$allocation=Pterodactyl\Models\Allocation::query()->where('node_id',$data['node'])->whereNull('server_id')->firstOrFail();
$server=$app->make(Pterodactyl\Contracts\Servers\CreatesServers::class)->create([
'name'=>'Integration game','owner_id'=>Pterodactyl\Models\User::query()->where('root_admin',true)->firstOrFail()->id,
'egg_id'=>$egg->id,'image'=>'ptero-integration-minetest:1','startup'=>$egg->startup,'environment'=>[],
'skip_scripts'=>true,'memory'=>128,'swap'=>128,'disk'=>1024,'io'=>500,'cpu'=>50,'oom_disabled'=>false,
'database_limit'=>1,'allocation_limit'=>0,'backup_limit'=>1,'allocation_id'=>$allocation->id,'start_on_completion'=>true]);
fwrite(STDOUT,json_encode(['uuid'=>$server->uuid]));
'''
    server = execute(i, code, {'node': result['node_id']})
    for attempt in range(120):
        status, logs = i.run.run(['docker', 'logs', server['uuid']], check=False)
        if status == 0 and 'listening on' in logs:
            break
        time.sleep(1)
    else:
        raise InstallerError('The integration game did not start')
    # Installer backups concern only Panel and Wings configuration. A running
    # hosted game and its SQL instance must not be stopped, paused or archived.
    verified(i.backup_panel())
    wings_backup = i.backup_wings()
    if not (wings_backup / 'config.yml').is_file():
        raise InstallerError('Wings configuration backup is missing')
    _, status = i.run.run(['docker', 'inspect', '--format', '{{.State.Running}} {{.State.Paused}}', server['uuid']])
    if status.strip() != 'true false':
        raise InstallerError('Panel/Wings backup changed the hosted game state')
    _, rows = query('SELECT id FROM installer_smoke.world;')
    if rows.strip() != '42':
        raise InstallerError('Panel/Wings backup changed game SQL')
    i.ui.say('INTEGRATION_PANEL_WINGS_BACKUP_GAME_PRESERVATION_PASSED', 'ok')
