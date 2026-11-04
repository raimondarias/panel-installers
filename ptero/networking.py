"""Choose a Wings bridge without overlapping Docker networks or host routes."""

import ipaddress
import json
from .core import InstallerError


def wings_network(installer, config):
    if config.get('docker', {}).get('network'):
        return config
    _, output = installer.run.run(['docker', 'network', 'ls', '--format', '{{.ID}}'])
    ids = output.split()
    networks = []
    if ids:
        _, output = installer.run.run(['docker', 'network', 'inspect', *ids])
        networks = json.loads(output)
    occupied = []
    existing = None
    for network in networks:
        for item in (network.get('IPAM') or {}).get('Config') or []:
            if item.get('Subnet'):
                subnet = ipaddress.ip_network(item['Subnet'], strict=False)
                occupied.append(subnet)
                if network['Name'] == 'pterodactyl_nw' and subnet.version == 4:
                    existing = (subnet, item.get('Gateway', str(subnet.network_address + 1)))
    _, output = installer.run.run(['ip', '-j', '-4', 'route', 'show'])
    for route in json.loads(output or '[]'):
        if route.get('dst') and route['dst'] != 'default':
            occupied.append(ipaddress.ip_network(route['dst'], strict=False))
    candidates = ['172.18.0.0/16'] + [f'172.{second}.{third}.0/24' for second in (30, 31) for third in range(256)]
    choice = existing
    if choice is None:
        for value in candidates:
            subnet = ipaddress.ip_network(value)
            if not any(subnet.version == other.version and subnet.overlaps(other) for other in occupied):
                choice = (subnet, str(subnet.network_address + 1))
                break
    if choice is None:
        raise InstallerError('No free private Wings subnet was found. Configure the node network manually.')
    subnet, gateway = choice
    config.setdefault('docker', {})['network'] = {
        'name': 'pterodactyl_nw',
        'interface': gateway,
        'interfaces': {'v4': {'subnet': str(subnet), 'gateway': gateway}},
    }
    return config
