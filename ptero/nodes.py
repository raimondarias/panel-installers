"""Idempotent first-node provisioning through Panel v2 contracts."""

import ipaddress
import re
from .core import InstallerError, valid_host, node_configuration, write_json
from .panel_bridge import execute


def port_range(value):
    try:
        first, last = map(int, value.split('-')) if '-' in value else (int(value), int(value))
        return 1025 <= first <= last <= 65535 and last - first < 1000
    except (ValueError, TypeError):
        return False


def plan(installer):
    ui = installer.ui
    ui.step(1, 3, 'Node resources')
    short = ui.ask('Location code', 'local', lambda v: bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,60}', v)))
    data = {
        'location': short,
        'name': ui.ask('Node name', 'Node 1', lambda v: bool(re.fullmatch(r'[A-Za-z0-9_. -]{1,100}', v))),
        'fqdn': ui.ask('Node hostname / IPv4', validator=valid_host),
        'memory': int(ui.ask('Node memory (MiB)', '1024', lambda v: v.isdigit() and 128 <= int(v) <= 2147483647)),
        'disk': int(ui.ask('Node disk (MiB)', '10240', lambda v: v.isdigit() and 1024 <= int(v) <= 2147483647)),
        'memory_overallocate': 0,
        'disk_overallocate': 0,
        'public': True,
        'upload_size': 100,
        'daemon_base': '/var/lib/pterodactyl/volumes',
    }
    tls = ui.yes('Use HTTPS for the node?', True)
    data['scheme'] = 'https' if tls else 'http'
    data['behind_proxy'] = ui.yes('Is an existing HTTPS reverse proxy terminating node TLS?', False) if tls else False
    if installer.panel_state()['url'].startswith('https://') and not tls:
        raise InstallerError('An HTTPS Panel requires an HTTPS node for browser connections.')
    data['daemon_listen'] = int(ui.ask('Node API port', '8080', installer.valid_port))
    data['daemon_sftp'] = int(ui.ask('Node SFTP port', '2022', installer.valid_port))
    if data['daemon_listen'] == data['daemon_sftp']:
        raise InstallerError('Node API and SFTP ports must be different.')
    ui.step(2, 3, 'Game allocations')

    def ipv4(v):
        try:
            ip = ipaddress.IPv4Address(v)
            return not (ip.is_unspecified or ip.is_multicast)
        except ValueError:
            return False

    data['allocation_ip'] = ui.ask('Allocation IPv4 address', validator=ipv4)
    data['allocation_ports'] = [ui.ask('Allocation port or range', '25565-25575', port_range)]
    first, last = (
        map(int, data['allocation_ports'][0].split('-')) if '-' in data['allocation_ports'][0] else [int(data['allocation_ports'][0])] * 2
    )
    if any(first <= data[k] <= last for k in ('daemon_listen', 'daemon_sftp')):
        raise InstallerError('Game allocations cannot overlap node API or SFTP ports.')
    ui.step(3, 3, 'Review node')
    ui.card(
        'Ready to install',
        [
            f"{data['name']} · {data['scheme']}://{data['fqdn']}:{data['daemon_listen']}",
            ui.tr('Node resources: {memory} MiB RAM / {disk} MiB disk').format(**data),
            f"{data['allocation_ip']}:{data['allocation_ports'][0]}",
        ],
    )
    return data


CREATE = r'''
$location=Pterodactyl\Models\Location::query()->where('short',$data['location'])->first();
if(!$location){$location=$app->make(Pterodactyl\Contracts\Locations\CreatesLocations::class)->create(['short'=>$data['location'],'long'=>'Managed by Pterodactyl Installer']);}
$node=Pterodactyl\Models\Node::query()->where('fqdn',$data['fqdn'])->where('daemonListen',$data['daemon_listen'])->first();
$input=array_diff_key($data,array_flip(['location','allocation_ip','allocation_ports']));$input['location_id']=$location->id;
if($node){foreach(['name','memory','disk','scheme','behind_proxy','location_id'] as $k){if($node->$k!=$input[$k]){throw new RuntimeException('An existing node has different settings.');}}
if($node->daemonSFTP!=$input['daemon_sftp']){throw new RuntimeException('An existing node has a different SFTP port.');}}
else{$node=$app->make(Pterodactyl\Contracts\Nodes\CreatesNodes::class)->create($input);}
$app->make(Pterodactyl\Contracts\Allocations\CreatesAllocations::class)->create($node,['allocation_ip'=>$data['allocation_ip'],'allocation_ports'=>$data['allocation_ports']]);
fwrite(STDOUT,json_encode(['node_id'=>$node->id,'configuration'=>$node->getConfiguration()],JSON_THROW_ON_ERROR));
'''


def create(installer, data, link=False):
    result = execute(installer, CREATE, data)
    if type(result.get('node_id')) is not int or result['node_id'] < 1:
        raise InstallerError('The Panel returned an invalid node ID.')
    config = node_configuration(result['configuration'], installer.panel_state()['url'])
    installer.run.secrets.update((config['token'], config['token_id']))
    report = {'schema': 1, 'owner': 'ptero-installer', **data, **result}
    write_json(installer.p.base / 'node-provisioning.json', report)
    if link:
        if not installer.p.wings_state.exists():
            raise InstallerError('Install Wings before applying the node configuration.')
        if installer.p.config.exists():
            installer.backup_wings()
        from .networking import wings_network

        wings_network(installer, config)
        write_json(installer.p.config, config)
        state = installer.load(installer.p.wings_state)
        state.update(status='configured', panel_url=installer.panel_state()['url'], node=str(result['node_id']))
        write_json(installer.p.wings_state, state)
        if config['api']['ssl']['enabled']:
            installer.wings_tls()
        installer.start_wings()
    return result


def setup(installer):
    data = plan(installer)
    link = installer.p.wings_state.exists() and installer.ui.yes('Apply this node configuration to local Wings?', True)
    installer.ui.confirm('CREATE NODE')
    result = create(installer, data, link)
    installer.ui.say(installer.ui.tr('Node created: {value}').format(value=result['node_id']), 'ok')
