#!/usr/bin/env python3
import fnmatch
import glob
import os
import shutil
import sys
from datetime import datetime

from .discover_tailscale import ManualProvider, TailscaleProvider
from .models import read_topology_data
from .toml_io import write_toml


def get_topology_path() -> str:
    topologies_home = os.environ.get('TOPOLOGIES_HOME', os.path.expanduser('~/.agents/skills'))
    return os.path.join(topologies_home, 'topology.toml')


def list_sidecars(topologies_home: str) -> list[str]:
    """Return sorted topology-*.toml sidecar files, excluding backup files."""
    candidates = glob.glob(os.path.join(topologies_home, 'topology-*.toml'))
    return sorted(p for p in candidates if not fnmatch.fnmatch(os.path.basename(p), 'topology-backup*.toml'))


def backup(topology_path: str) -> None:
    backup_path = os.path.join(os.path.dirname(topology_path), 'topology-backup.toml')
    shutil.copy2(topology_path, backup_path)


def read_network(data: dict) -> str:
    """Where the machine list comes from: tailscale (the default) or manual."""
    return data.get('network', 'tailscale').lower()


def merge(existing_machines: list[dict], discovered: list[dict], ip_key: str = 'tailscale_ip') -> tuple[list[dict], list[str]]:
    existing_by_hostname = {m['hostname']: m for m in existing_machines}
    discovered_hostnames = {m['hostname'] for m in discovered}
    changes = []
    merged = []

    for machine in discovered:
        hostname = machine['hostname']
        if hostname in existing_by_hostname:
            row = dict(existing_by_hostname[hostname])
            old_ip = row.get(ip_key, '—')
            row[ip_key] = machine['tailscale_ip']
            row['os'] = machine['os']
            if old_ip != machine['tailscale_ip']:
                changes.append(f"  {hostname}: {ip_key} {old_ip} → {machine['tailscale_ip']}")
        else:
            row = {'name': hostname, 'hostname': hostname, ip_key: machine['tailscale_ip'], 'os': machine['os']}
            changes.append(f'  {hostname}: new machine added')
        merged.append(row)

    for row in existing_machines:
        if row['hostname'] not in discovered_hostnames:
            updated = dict(row)
            updated[ip_key] = '(offline)'
            merged.append(updated)
            changes.append(f"  {row['hostname']}: marked offline")

    return merged, changes


def main():
    topology_path = get_topology_path()

    if not os.path.exists(topology_path):
        print(f'Topology file not found: {topology_path}', file=sys.stderr)
        sys.exit(1)

    backup(topology_path)

    data = read_topology_data(topology_path)
    existing_machines = data.get('machines', [])

    provider_name = read_network(data)
    if provider_name == 'manual':
        machines = [
            {'hostname': r['hostname'], 'ip': r.get('local_ip', ''), 'os': r.get('os', '')}
            for r in existing_machines
            if r.get('local_ip') not in (None, '', '(offline)')
        ]
        provider = ManualProvider(machines)
        ip_key = 'local_ip'
    else:
        provider = TailscaleProvider()
        ip_key = 'tailscale_ip'

    try:
        discovered = provider.discover()
    except RuntimeError as e:
        print(f'Discovery error: {e}', file=sys.stderr)
        sys.exit(1)

    merged_machines, changes = merge(existing_machines, discovered, ip_key)

    data['machines'] = merged_machines
    data['last_refreshed'] = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')

    write_toml(data, topology_path)

    print(f'Topology updated: {topology_path}')
    if changes:
        print('Changes:')
        for change in changes:
            print(change)
    else:
        print('No changes.')
    print()
    print('Tip: run /topology discover for deeper per-node data — GPU model, VRAM,')
    print('     local IP, installed GGUFs, running models, and agent process status.')


if __name__ == '__main__':
    main()
