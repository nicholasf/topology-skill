#!/usr/bin/env python3
"""First-run setup for topology-skill.

Interactive mode (no args): prompts for provider choice and, for manual
provider, machine entries.

Non-interactive mode (for agents and scripts):
  --provider tailscale
  --provider manual --machines "pond 192.168.86.118,gollum 192.168.86.50"

After writing topology.toml the script calls sync.py to validate and
populate it, so the file is ready to use immediately.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime

from .models import SCHEMA_VERSION
from .sync import get_topology_path
from .toml_io import write_toml


def machines_to_rows(machines: list[dict]) -> list[dict]:
    return [{'name': m['hostname'], 'hostname': m['hostname'], 'local_ip': m['ip']} for m in machines]


def write_topology(provider: str, machines: list[dict], topology_path: str) -> None:
    os.makedirs(os.path.dirname(topology_path), exist_ok=True)
    data = {
        'schema_version': SCHEMA_VERSION,
        'network': provider,
        'last_refreshed': datetime.now().strftime('%Y-%m-%dT%H-%M-%S'),
        'machines': machines,
    }
    write_toml(data, topology_path)
    print(f'Created {topology_path}')


def run_sync() -> bool:
    result = subprocess.run([sys.executable, '-m', 'topology.sync'])
    return result.returncode == 0


def parse_machines_arg(value: str) -> list[dict]:
    machines = []
    for entry in value.split(','):
        parts = entry.strip().split()
        if len(parts) != 2:
            raise argparse.ArgumentTypeError(
                f"Invalid machine entry: {entry!r}. Expected 'hostname ip'."
            )
        machines.append({'hostname': parts[0], 'ip': parts[1]})
    return machines


def prompt_provider() -> str:
    print('No topology found. How do you want to discover your network?')
    print('  1. tailscale — query Tailscale for hostnames and IPs automatically')
    print('  2. manual    — enter IP addresses yourself')
    while True:
        choice = input('Choice [1/2]: ').strip()
        if choice in ('1', 'tailscale'):
            return 'tailscale'
        if choice in ('2', 'manual'):
            return 'manual'
        print('  Enter 1 or 2.')


def prompt_machines() -> list[dict]:
    print("Enter machines as 'hostname ip' pairs, one per line. Blank line to finish.")
    machines = []
    while True:
        line = input('  > ').strip()
        if not line:
            break
        parts = line.split()
        if len(parts) != 2:
            print("  Expected 'hostname ip', e.g. 'pond 192.168.86.118'")
            continue
        machines.append({'hostname': parts[0], 'ip': parts[1]})
    return machines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='First-run setup for topology-skill.')
    parser.add_argument('--provider', choices=['tailscale', 'manual'], help='Network provider')
    parser.add_argument(
        '--machines',
        type=str,
        help='Comma-separated hostname/ip pairs for manual mode: "pond 192.168.86.118,gollum 192.168.86.50"',
    )
    parser.add_argument('--force', action='store_true', help='Overwrite an existing topology file')
    args = parser.parse_args(argv)

    topology_path = get_topology_path()

    if os.path.exists(topology_path) and not args.force:
        print(f'Topology already exists: {topology_path}')
        print('Use --force to reinitialize.')
        sys.exit(0)

    provider = args.provider or prompt_provider()

    if provider == 'tailscale':
        write_topology('tailscale', [], topology_path)
        print('Running sync to populate from Tailscale...')
        if not run_sync():
            print('Sync failed. Check that Tailscale is installed and running.', file=sys.stderr)
            sys.exit(1)
    else:
        if args.machines:
            try:
                machines = parse_machines_arg(args.machines)
            except argparse.ArgumentTypeError as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
        else:
            machines = prompt_machines()

        if not machines:
            print('No machines entered. Exiting.', file=sys.stderr)
            sys.exit(1)

        rows = machines_to_rows(machines)
        write_topology('manual', rows, topology_path)
        print('Running sync to validate and timestamp...')
        if not run_sync():
            print('Sync failed.', file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
