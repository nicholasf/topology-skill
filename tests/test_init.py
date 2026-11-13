import argparse
import os
from unittest.mock import patch

import pytest

from topology.init import (
    get_topology_path,
    machines_to_rows,
    parse_machines_arg,
    write_topology,
)
from topology.toml_io import read_toml


# -- get_topology_path --

def test_get_topology_path_uses_topologies_home(monkeypatch, tmp_path):
    monkeypatch.setenv('TOPOLOGIES_HOME', str(tmp_path))
    assert get_topology_path() == str(tmp_path / 'topology.toml')


def test_get_topology_path_defaults_when_unset(monkeypatch):
    monkeypatch.delenv('TOPOLOGIES_HOME', raising=False)
    assert get_topology_path().endswith('topology.toml')


# -- machines_to_rows --

def test_machines_to_rows_sets_name_and_hostname():
    machines = [{'hostname': 'pond', 'ip': '192.168.86.118'}]
    rows = machines_to_rows(machines)
    assert rows[0]['name'] == 'pond'
    assert rows[0]['hostname'] == 'pond'
    assert rows[0]['local_ip'] == '192.168.86.118'


def test_machines_to_rows_multiple():
    machines = [
        {'hostname': 'pond', 'ip': '192.168.86.118'},
        {'hostname': 'gollum', 'ip': '192.168.86.50'},
    ]
    rows = machines_to_rows(machines)
    assert len(rows) == 2
    assert rows[1]['hostname'] == 'gollum'


# -- parse_machines_arg --

def test_parse_machines_arg_single():
    result = parse_machines_arg('pond 192.168.86.118')
    assert result == [{'hostname': 'pond', 'ip': '192.168.86.118'}]


def test_parse_machines_arg_multiple():
    result = parse_machines_arg('pond 192.168.86.118,gollum 192.168.86.50')
    assert len(result) == 2
    assert result[1] == {'hostname': 'gollum', 'ip': '192.168.86.50'}


def test_parse_machines_arg_invalid_raises():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_machines_arg('just-hostname')


# -- write_topology --

def test_write_topology_creates_file(tmp_path):
    path = str(tmp_path / 'topology.toml')
    write_topology('manual', [], path)
    assert os.path.exists(path)
    data = read_toml(path)
    assert data['network'] == 'manual'
    assert data['schema_version'] == 2
    assert 'last_refreshed' in data
    assert data.get('machines', []) == []


def test_write_topology_creates_parent_dirs(tmp_path):
    path = str(tmp_path / 'nested' / 'dir' / 'topology.toml')
    write_topology('manual', [], path)
    assert os.path.exists(path)


def test_write_topology_includes_machines(tmp_path):
    path = str(tmp_path / 'topology.toml')
    write_topology('manual', [{'name': 'pond', 'hostname': 'pond', 'local_ip': '192.168.86.118'}], path)
    data = read_toml(path)
    assert data['machines'][0]['hostname'] == 'pond'
    assert data['machines'][0]['local_ip'] == '192.168.86.118'


# -- main() integration --

def test_main_tailscale_non_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv('TOPOLOGIES_HOME', str(tmp_path))
    with patch('topology.init.run_sync', return_value=True):
        from topology.init import main
        import sys
        with patch.object(sys, 'argv', ['init.py', '--provider', 'tailscale']):
            main()
    data = read_toml(str(tmp_path / 'topology.toml'))
    assert data['network'] == 'tailscale'
    assert data.get('machines', []) == []


def test_main_manual_non_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv('TOPOLOGIES_HOME', str(tmp_path))
    with patch('topology.init.run_sync', return_value=True):
        from topology.init import main
        import sys
        with patch.object(sys, 'argv', ['init.py', '--provider', 'manual', '--machines', 'pond 192.168.86.118']):
            main()
    data = read_toml(str(tmp_path / 'topology.toml'))
    assert data['network'] == 'manual'
    assert data['machines'][0]['hostname'] == 'pond'
    assert data['machines'][0]['local_ip'] == '192.168.86.118'


def test_main_exits_if_topology_exists_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv('TOPOLOGIES_HOME', str(tmp_path))
    (tmp_path / 'topology.toml').write_text('provider = "manual"\n')
    import sys
    with patch.object(sys, 'argv', ['init.py', '--provider', 'tailscale']):
        with pytest.raises(SystemExit) as exc:
            from topology.init import main
            main()
    assert exc.value.code == 0
    assert 'manual' in (tmp_path / 'topology.toml').read_text()


def test_main_force_overwrites_existing(tmp_path, monkeypatch):
    monkeypatch.setenv('TOPOLOGIES_HOME', str(tmp_path))
    (tmp_path / 'topology.toml').write_text('provider = "old"\n')
    with patch('topology.init.run_sync', return_value=True):
        import sys
        with patch.object(sys, 'argv', ['init.py', '--provider', 'manual', '--machines', 'pond 192.168.86.118', '--force']):
            from topology.init import main
            main()
    data = read_toml(str(tmp_path / 'topology.toml'))
    assert data['network'] == 'manual'
