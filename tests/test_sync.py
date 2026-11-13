import os

from topology.sync import get_topology_path, merge, read_network


def discovered(hostname, tailscale_ip='100.1.2.3', os_name='Linux', online=True, is_self=False):
    return {'hostname': hostname, 'tailscale_ip': tailscale_ip, 'os': os_name, 'online': online, 'is_self': is_self}


# -- get_topology_path --

def test_get_topology_path_uses_topologies_home(monkeypatch):
    monkeypatch.setenv('TOPOLOGIES_HOME', '/my/topologies')
    assert get_topology_path() == '/my/topologies/topology.toml'


def test_get_topology_path_defaults_when_unset(monkeypatch):
    monkeypatch.delenv('TOPOLOGIES_HOME', raising=False)
    assert get_topology_path() == os.path.expanduser('~/.agents/skills/topology.toml')


# -- read_network --

def test_read_network_detects_manual():
    assert read_network({'network': 'manual'}) == 'manual'


def test_read_network_detects_tailscale():
    assert read_network({'network': 'tailscale'}) == 'tailscale'


def test_read_network_defaults_to_tailscale_when_absent():
    assert read_network({}) == 'tailscale'


def test_read_network_is_case_insensitive():
    assert read_network({'network': 'Manual'}) == 'manual'


# -- merge --

def test_merge_new_machine_seeds_name_and_hostname_only():
    merged, changes = merge([], [discovered('newhost')])
    assert len(merged) == 1
    assert merged[0] == {'name': 'newhost', 'hostname': 'newhost', 'tailscale_ip': '100.1.2.3', 'os': 'Linux'}
    assert any('new machine' in c for c in changes)


def test_merge_changed_ip_updates_ip_preserves_manual_keys():
    existing = [{'name': 'myhost', 'hostname': 'myhost', 'tailscale_ip': '100.1.2.3', 'role': 'server', 'ssh': True}]
    merged, changes = merge(existing, [discovered('myhost', tailscale_ip='100.9.9.9')])
    assert merged[0]['tailscale_ip'] == '100.9.9.9'
    assert merged[0]['role'] == 'server'
    assert merged[0]['ssh'] is True
    assert any('100.1.2.3' in c and '100.9.9.9' in c for c in changes)


def test_merge_absent_machine_marked_offline():
    existing = [{'name': 'oldhost', 'hostname': 'oldhost', 'tailscale_ip': '100.1.2.3'}]
    merged, changes = merge(existing, [])
    assert merged[0]['tailscale_ip'] == '(offline)'
    assert any('offline' in c for c in changes)


def test_merge_no_changes_returns_empty_changes_list():
    existing = [{'name': 'myhost', 'hostname': 'myhost', 'tailscale_ip': '100.1.2.3', 'os': 'Linux'}]
    merged, changes = merge(existing, [discovered('myhost', tailscale_ip='100.1.2.3')])
    assert changes == []
    assert merged[0]['tailscale_ip'] == '100.1.2.3'


def test_merge_manual_ip_key():
    existing = [{'name': 'pond', 'hostname': 'pond', 'local_ip': '192.168.86.100'}]
    merged, changes = merge(existing, [discovered('pond', tailscale_ip='192.168.86.118')], ip_key='local_ip')
    assert merged[0]['local_ip'] == '192.168.86.118'
    assert any('local_ip' in c for c in changes)


def test_merge_manual_preserves_extra_keys():
    existing = [{'name': 'pond', 'hostname': 'pond', 'local_ip': '192.168.86.118', 'role': 'LLM Node', 'gpu': 'RTX 4090'}]
    merged, changes = merge(existing, [discovered('pond', tailscale_ip='192.168.86.118')], ip_key='local_ip')
    assert merged[0]['role'] == 'LLM Node'
    assert merged[0]['gpu'] == 'RTX 4090'
    assert changes == []
