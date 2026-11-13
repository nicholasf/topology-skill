import os
import tempfile

from topology.benchmark_llm import record_result
from topology.toml_io import read_toml, write_toml


def make_topology_file(data):
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False)
    f.close()
    write_toml(data, f.name)
    return f.name


# -- record_result --

def test_record_creates_benchmarks_when_absent():
    path = make_topology_file({'machines': [{'name': 'gollum', 'hostname': 'gollum'}]})
    try:
        record_result(path, 'gollum', 'qwen3', 300.0, 45.2, 3)
        result = read_toml(path)
        assert len(result['benchmarks']) == 1
        assert result['benchmarks'][0]['hostname'] == 'gollum'
        assert result['benchmarks'][0]['tok_s'] == 45.2
    finally:
        os.unlink(path)


def test_record_appends_new_row_without_touching_existing():
    existing = {
        'hostname': 'gollum', 'llm': 'other-model', 'timestamp': '2026-01-01T00-00-00',
        'ttft_ms': 200.0, 'tok_s': 60.0, 'runs': 3,
    }
    path = make_topology_file({'schema_version': 2, 'benchmarks': [existing]})
    try:
        record_result(path, 'gollum', 'new-model', 310.0, 42.1, 3)
        rows = read_toml(path)['benchmarks']
        models = {r['llm'] for r in rows}
        assert models == {'other-model', 'new-model'}
    finally:
        os.unlink(path)


def test_record_replaces_existing_row_for_same_hostname_and_model():
    old = {
        'hostname': 'gollum', 'llm': 'qwen3', 'timestamp': '2026-01-01T00-00-00',
        'ttft_ms': 200.0, 'tok_s': 60.0, 'runs': 3,
    }
    path = make_topology_file({'schema_version': 2, 'benchmarks': [old]})
    try:
        record_result(path, 'gollum', 'qwen3', 320.0, 48.5, 5)
        rows = read_toml(path)['benchmarks']
        assert len(rows) == 1
        assert rows[0]['tok_s'] == 48.5
        assert rows[0]['runs'] == 5
    finally:
        os.unlink(path)


def test_record_rows_sorted_by_hostname_then_model():
    path = make_topology_file({})
    try:
        record_result(path, 'zzz-host', 'model-a', 100.0, 50.0, 1)
        record_result(path, 'aaa-host', 'model-b', 200.0, 40.0, 1)
        hostnames = [r['hostname'] for r in read_toml(path)['benchmarks']]
        assert hostnames == sorted(hostnames)
    finally:
        os.unlink(path)


def test_record_upgrades_v1_benchmarks():
    old = {'hostname': 'gollum', 'model': 'other', 'timestamp': 't', 'ttft_ms': 1.0, 'tok_s': 2.0, 'runs': 1}
    path = make_topology_file({'schema_version': 1, 'benchmarks': [old]})
    try:
        record_result(path, 'gollum', 'qwen3', 300.0, 45.2, 3)
        data = read_toml(path)
        assert data['schema_version'] == 2
        assert {r['llm'] for r in data['benchmarks']} == {'other', 'qwen3'}
        assert all('model' not in r for r in data['benchmarks'])
    finally:
        os.unlink(path)
