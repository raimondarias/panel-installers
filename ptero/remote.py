"""Root SSH provisioning with verified host keys and a source-pinned runtime payload."""

import base64
import io
import ipaddress
import json
from pathlib import Path
import secrets
import shlex
import tempfile
import zipfile
from .core import InstallerError, valid_host


def private_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value)
        return ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)
    except ValueError:
        return False


def target_plan(i, preview=False):
    ui = i.ui
    result = {
        'host': ui.ask('Remote SSH hostname / IPv4', validator=lambda v: valid_host(v) and ':' not in v),
        'port': int(ui.ask('Remote SSH port', '22', i.valid_port)),
        'identity': ui.ask(
            'Absolute SSH private key path',
            '/preview/id_ed25519' if preview else None,
            lambda v: Path(v).is_absolute() and (preview or Path(v).is_file()),
        ),
        'known_hosts': ui.ask(
            'Verified SSH known_hosts path',
            '/preview/known_hosts' if preview else str(Path.home() / '.ssh/known_hosts'),
            lambda v: Path(v).is_absolute() and (preview or Path(v).is_file()),
        ),
    }
    ui.say('Remote provisioning uses root SSH with strict host-key verification. No password is sent.')
    return result


def ssh(target):
    return [
        'ssh',
        '-p',
        str(target['port']),
        '-i',
        target['identity'],
        '-o',
        'BatchMode=yes',
        '-o',
        'StrictHostKeyChecking=yes',
        '-o',
        'UserKnownHostsFile=' + target['known_hosts'],
        '-o',
        'ConnectTimeout=15',
        '-o',
        'ServerAliveInterval=15',
        '-o',
        'ServerAliveCountMax=3',
        'root@' + target['host'],
    ]


def payload():
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        source = Path(__file__).resolve().parent
        for path in sorted(source.rglob('*')):
            if path.is_file() and path.suffix in ('.py', '.json') and '__pycache__' not in path.parts:
                archive.writestr(str(Path('ptero') / path.relative_to(source)), path.read_bytes())
    return base64.b64encode(memory.getvalue()).decode()


AGENT = r'''
import base64,hashlib,io,json,os,pathlib,sys,zipfile
os.umask(0o077)
request=json.load(sys.stdin)
if request['action'] not in ('node','database'):raise ValueError('Unknown remote action')
raw=base64.b64decode(request['runtime'],validate=True)
if hashlib.sha256(raw).hexdigest()!=request['sha256']:raise ValueError('Runtime checksum mismatch')
root=pathlib.Path('/opt/pterodactyl-installer/remote-runtime');root.mkdir(parents=True,exist_ok=True,mode=0o700)
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
 for member in archive.infolist():
  name=pathlib.PurePosixPath(member.filename)
  if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0]!='ptero' or member.file_size>4*1024*1024:raise ValueError('Unsafe runtime member')
  target=root/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700);target.write_bytes(archive.read(member))
sys.path.insert(0,str(root))
from ptero.ui import UI
from ptero.core import Installer,write_json
class RemoteUI(UI):
 def yes(self,label,default=False):return False
 def choice(self,label,options,default=0):return options[default][0]
 def ask(self,label,default=None,validator=None,secret=False):
  values={'Wings domain, e.g. node.example.com':request.get('fqdn'),'Let\'s Encrypt email':request.get('email')}
  value=values.get(label,default)
  if value is None or (validator and not validator(value)):raise ValueError('Missing remote answer: '+label)
  return value
 def confirm(self,phrase):pass
installer=Installer(ui=RemoteUI(False,request.get('language','en'),plain=True))
installer.check_host(wings=request['action']=='node')
if request['action']=='node':
 installer.install_wings()
 if installer.p.config.exists():installer.backup_wings()
 from ptero.networking import wings_network
 write_json(installer.p.config,wings_network(installer,request['configuration']))
 state=installer.load(installer.p.wings_state);state.update(status='configured',node=str(request['node_id']),panel_url=request['panel_url']);write_json(installer.p.wings_state,state)
 if request['configuration']['api']['ssl']['enabled']:installer.wings_tls()
 installer.start_wings()
else:
 from ptero.game_database import GameDatabase
 from ptero.native_game_database import NativeGameDatabase
 NativeGameDatabase(GameDatabase(installer)).install(request['settings'])
print('PTERO_REMOTE_SUCCESS',flush=True)
'''


def execute(i, target, request):
    import hashlib

    source = payload()
    request = {**request, 'runtime': source, 'sha256': hashlib.sha256(base64.b64decode(source)).hexdigest(), 'language': i.ui.language}
    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(request).encode())
        stream.seek(0)
        _, output = i.run.run(
            [*ssh(target), 'python3 -c ' + shlex.quote(AGENT)],
            input_file=stream,
            timeout=14400,
            label='Provisioning the remote machine over verified SSH',
        )
    if 'PTERO_REMOTE_SUCCESS' not in output:
        raise InstallerError('The remote machine did not confirm successful provisioning.')


def setup_node(i):
    from .nodes import plan, create

    target = target_plan(i)
    data = plan(i)
    email = i.ui.ask("Let's Encrypt email", i.panel_state()['email']) if data['scheme'] == 'https' and not data['behind_proxy'] else None
    i.ui.confirm('INSTALL REMOTE NODE')
    i.run.run([*ssh(target), 'python3 --version'], label='Checking remote SSH and Python')
    result = create(i, data, link=False)
    execute(
        i,
        target,
        {
            'action': 'node',
            'configuration': result['configuration'],
            'node_id': result['node_id'],
            'panel_url': i.panel_state()['url'],
            'fqdn': data['fqdn'],
            'email': email,
        },
    )


def database_plan(i):
    panel = i.panel_state()
    settings = {
        'mode': 'existing',
        'method': 'standalone',
        'engine': panel.get('database_engine', 'mariadb'),
        'name': 'Remote game databases',
        'username': 'ptero_games',
        'password': secrets.token_hex(32),
        'node_id': None,
        'host': i.ui.ask('Remote SQL private IPv4', validator=private_ipv4),
        'port': int(i.ui.ask('Game database port', '3307', i.valid_port)),
    }
    sources = i.ui.ask(
        'Allowed private source IPv4 addresses (comma separated)',
        validator=lambda v: bool(v) and all(private_ipv4(x.strip()) for x in v.split(',')),
    )
    settings['allowed_sources'] = sorted(set(x.strip() for x in sources.split(',')))
    return settings


def setup_database(i):
    from .game_database import GameDatabase

    panel = i.panel_state()
    target = target_plan(i)
    settings = database_plan(i)
    i.run.secrets.add(settings['password'])
    i.ui.confirm('INSTALL REMOTE DATABASE')
    i.run.run([*ssh(target), 'python3 --version'], label='Checking remote SSH and Python')
    execute(i, target, {'action': 'database', 'settings': settings})
    GameDatabase(i).register(panel, settings)
    panel['game_database'] = settings
    i.save_panel(panel)
    GameDatabase(i).credentials(panel)
