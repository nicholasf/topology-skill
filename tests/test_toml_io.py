from topology.toml_io import read_toml, write_toml


def test_roundtrip_scalars_and_arrays(tmp_path):
    path = str(tmp_path / 'topology.toml')
    data = {
        'schema_version': 1,
        'provider': 'tailscale',
        'machines': [
            {'name': 'pond', 'hostname': 'pond', 'ssh': True, 'gpu': '—'},
            {'name': 'hut', 'hostname': 'hut', 'ssh': False},
        ],
    }
    write_toml(data, path)
    result = read_toml(path)

    assert result['schema_version'] == 1
    assert result['provider'] == 'tailscale'
    assert result['machines'][0] == {'name': 'pond', 'hostname': 'pond', 'ssh': True, 'gpu': '—'}
    assert result['machines'][1] == {'name': 'hut', 'hostname': 'hut', 'ssh': False}


def test_roundtrip_numbers_and_lists(tmp_path):
    path = str(tmp_path / 'topology.toml')
    data = {
        'live_state': [
            {'hostname': 'pond', 'port': 11434, 'models': ['qwen3.8-27b:latest', 'qwen2.5-coder:14b']},
        ],
        'benchmarks': [
            {'hostname': 'pond', 'ttft_ms': 1083.5, 'tok_s': 85.2, 'runs': 8},
        ],
    }
    write_toml(data, path)
    result = read_toml(path)

    assert result['live_state'][0]['port'] == 11434
    assert result['live_state'][0]['models'] == ['qwen3.8-27b:latest', 'qwen2.5-coder:14b']
    assert result['benchmarks'][0]['ttft_ms'] == 1083.5
    assert result['benchmarks'][0]['runs'] == 8


def test_write_escapes_quotes_and_backslashes(tmp_path):
    path = str(tmp_path / 'topology.toml')
    write_toml({'machines': [{'name': 'x', 'note': 'say "hi" \\ bye'}]}, path)
    result = read_toml(path)
    assert result['machines'][0]['note'] == 'say "hi" \\ bye'


def test_write_escapes_newlines_tabs_and_control_characters(tmp_path):
    path = str(tmp_path / 'topology.toml')
    note = 'line one\nline two\r\n\ttabbed\x1b[0m\x7f'
    write_toml({'note': note}, path)
    assert read_toml(path)['note'] == note


def test_empty_array_writes_nothing_for_that_key(tmp_path):
    path = str(tmp_path / 'topology.toml')
    write_toml({'schema_version': 1, 'machines': []}, path)
    result = read_toml(path)
    assert result['schema_version'] == 1
    assert result.get('machines', []) == []


def test_missing_keys_are_absent_not_placeholder(tmp_path):
    path = str(tmp_path / 'topology.toml')
    write_toml({'machines': [{'name': 'pond'}]}, path)
    result = read_toml(path)
    assert result['machines'][0] == {'name': 'pond'}
    assert 'gpu' not in result['machines'][0]
