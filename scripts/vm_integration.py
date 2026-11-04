#!/usr/bin/env python3
"""Run a real deployment inside a verified disposable cloud VM on a CI host."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request

IMAGES = {
    'ubuntu-22.04': ('https://cloud-images.ubuntu.com/jammy/current/', 'jammy-server-cloudimg-amd64.img'),
    'ubuntu-24.04': ('https://cloud-images.ubuntu.com/noble/current/', 'noble-server-cloudimg-amd64.img'),
    'ubuntu-26.04': ('https://cloud-images.ubuntu.com/resolute/current/', 'resolute-server-cloudimg-amd64.img'),
    'debian-12': ('https://cloud.debian.org/images/cloud/bookworm/latest/', 'debian-12-genericcloud-amd64.qcow2'),
    'debian-13': ('https://cloud.debian.org/images/cloud/trixie/latest/', 'debian-13-genericcloud-amd64.qcow2'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os', choices=IMAGES, required=True)
    parser.add_argument('--method', choices=('standalone', 'docker'), default='standalone')
    parser.add_argument('--engine', choices=('mariadb', 'mysql'), default='mariadb')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    results = root / 'test-results/vm'
    results.mkdir(parents=True, exist_ok=True)
    report_path = results / f'{args.os}-{args.method}-{args.engine}.json'
    report = {'os': args.os, 'method': args.method, 'engine': args.engine, 'status': 'failed', 'stage': 'image'}

    def save_report():
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    save_report()
    with tempfile.TemporaryDirectory(prefix='ptero-ci-vm-') as temporary:
        work = Path(temporary)
        base, name = IMAGES[args.os]
        algorithm = 'sha512' if args.os.startswith('debian') else 'sha256'
        with urllib.request.urlopen(base + algorithm.upper() + 'SUMS', timeout=60) as response:
            sums = response.read().decode()
        expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip('*') == name)
        digest = hashlib.new(algorithm)
        with urllib.request.urlopen(base + name, timeout=120) as source, (work / 'base.img').open('wb') as target:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError('Official cloud-image checksum mismatch')
        report['image_checksum'] = digest.hexdigest()
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(work / 'key')], check=True)
        key = (work / 'key.pub').read_text().strip()
        (work / 'user-data').write_text(
            '#cloud-config\ndisable_root: false\nssh_pwauth: false\nusers:\n  - name: root\n    ssh_authorized_keys:\n      - ' + key + '\n'
        )
        (work / 'meta-data').write_text('instance-id: ptero-integration\nlocal-hostname: ptero-integration\n')
        subprocess.run(
            [
                'genisoimage',
                '-quiet',
                '-output',
                str(work / 'seed.iso'),
                '-volid',
                'cidata',
                '-joliet',
                '-rock',
                str(work / 'user-data'),
                str(work / 'meta-data'),
            ],
            check=True,
        )
        subprocess.run(
            ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(work / 'base.img'), str(work / 'disk.qcow2'), '24G'], check=True
        )
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 22022))
        report['stage'] = 'boot'
        save_report()
        # Some cloud kernels omit IDE drivers. Present the NoCloud seed as a
        # virtio block device so cloud-init can discover it on every image.
        # Keep an emulated VGA device even with no graphical window: Debian 13's
        # GRUB gfxterm/serial configuration can loop without it (Debian #1111240).
        machine = subprocess.Popen(
            [
                'qemu-system-x86_64',
                '-enable-kvm',
                '-cpu',
                'host',
                '-m',
                '2560',
                '-smp',
                '2',
                '-vga',
                'std',
                '-drive',
                f'file={work}/disk.qcow2,if=virtio,format=qcow2',
                '-drive',
                f'file={work}/seed.iso,if=virtio,format=raw,readonly=on',
                '-netdev',
                'user,id=net0,hostfwd=tcp:127.0.0.1:22022-:22',
                '-device',
                'virtio-net-pci,netdev=net0,romfile=',
                '-display',
                'none',
                '-serial',
                f'file:{work}/console.log',
            ]
        )
        ssh = [
            'ssh',
            '-i',
            str(work / 'key'),
            '-p',
            '22022',
            '-o',
            'BatchMode=yes',
            '-o',
            'StrictHostKeyChecking=accept-new',
            '-o',
            'UserKnownHostsFile=' + str(work / 'known_hosts'),
            '-o',
            'ConnectTimeout=5',
            'root@127.0.0.1',
        ]
        ready = False
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                try:
                    ready = subprocess.run([*ssh, 'true'], capture_output=True, timeout=8).returncode == 0
                except subprocess.TimeoutExpired:
                    ready = False
                if ready:
                    break
                if machine.poll() is not None:
                    raise RuntimeError('VM exited before SSH became ready')
                time.sleep(2)
            if not ready:
                raise RuntimeError('VM SSH readiness timed out')
            # Trust only this loopback VM's first key, then require it exactly.
            ssh[ssh.index('StrictHostKeyChecking=accept-new')] = 'StrictHostKeyChecking=yes'
            report['stage'] = 'deployment'
            save_report()
            with tarfile.open(work / 'runtime.tar.gz', 'w:gz') as archive:
                for path in ('ptero', 'installer.py', 'tests/integration'):
                    archive.add(root / path, arcname=path, filter=lambda member: None if '__pycache__' in member.name else member)
            with (work / 'runtime.tar.gz').open('rb') as stream:
                subprocess.run([*ssh, 'mkdir -p /root/ptero-ci && tar -xz -C /root/ptero-ci'], stdin=stream, check=True)
            if os.environ.get('PTERO_GITHUB_TOKEN'):
                subprocess.run(
                    [*ssh, 'umask 077; cat > /root/.ptero-github-token'], input=os.environ['PTERO_GITHUB_TOKEN'].encode(), check=True
                )
            result = subprocess.run(
                [
                    *ssh,
                    'cd /root/ptero-ci && python3 tests/integration/deploy.py --disposable '
                    f'--method {args.method} --engine {args.engine} --cycle --full',
                ],
                timeout=3000,
            )
            fetched = subprocess.run([*ssh, 'cat /root/ptero-ci/test-results/integration-result.json'], capture_output=True, text=True)
            if fetched.returncode == 0:
                report.update(json.loads(fetched.stdout))
            report['status'] = 'failed' if result.returncode else 'passed'
            report['stage'] = 'deployment' if result.returncode else 'complete'
            save_report()
            if result.returncode:
                subprocess.run([*ssh, 'tail -160 /var/log/pterodactyl-installer/installer.log'], check=False)
                subprocess.run([*ssh, 'journalctl -u wings -u ptero-game-sql-native -u ptero-game-databases --no-pager -n 60'], check=False)
                subprocess.run(
                    [
                        *ssh,
                        (
                            'docker compose --project-name ptero-game-databases --file '
                            '/opt/pterodactyl-installer/game-databases/compose.json logs --tail 35 database'
                        ),
                    ],
                    check=False,
                )
                subprocess.run([*ssh, 'journalctl -k --no-pager -n 100'], check=False)
                raise RuntimeError('Real VM deployment failed')
        except Exception:
            if not ready and (work / 'console.log').exists():
                # Before SSH is available, no application secrets have been sent.
                print('VM boot console (last 120 lines):', flush=True)
                print('\n'.join((work / 'console.log').read_text(errors='replace').splitlines()[-120:]), flush=True)
            raise
        finally:
            machine.terminate()
            try:
                machine.wait(timeout=20)
            except subprocess.TimeoutExpired:
                machine.kill()
                machine.wait()


if __name__ == '__main__':
    main()
