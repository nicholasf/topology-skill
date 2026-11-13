import json
from unittest.mock import MagicMock, patch

from topology.discover import (
    build_agents,
    build_backends,
    build_llms,
    probe_llama_context_window,
    probe_ollama_context_window,
    read_existing_agent_reasoning_buffers,
)


# ── probe_llama_context_window ────────────────────────────────────────────────

def test_probe_llama_context_window_returns_n_ctx():
    with patch('topology.discover.http_json', return_value={'n_ctx': 65536}):
        assert probe_llama_context_window('pond') == 65536


def test_probe_llama_context_window_missing_key_returns_none():
    with patch('topology.discover.http_json', return_value={'other_key': 'value'}):
        assert probe_llama_context_window('pond') is None


def test_probe_llama_context_window_server_down_returns_none():
    with patch('topology.discover.http_json', return_value=None):
        assert probe_llama_context_window('pond') is None


# ── probe_ollama_context_window ───────────────────────────────────────────────

def _mock_urlopen(response_data: dict):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def test_probe_ollama_context_window_returns_context_length():
    mock_resp = _mock_urlopen({'model_info': {'llama.context_length': 131072}})
    with patch('urllib.request.urlopen', return_value=mock_resp):
        assert probe_ollama_context_window('gollum', 'qwen3-coder:30b') == 131072


def test_probe_ollama_context_window_matches_any_architecture_prefix():
    mock_resp = _mock_urlopen({'model_info': {'qwen2.context_length': 32768}})
    with patch('urllib.request.urlopen', return_value=mock_resp):
        assert probe_ollama_context_window('gollum', 'qwen2.5-coder:14b') == 32768


def test_probe_ollama_context_window_missing_model_info_returns_none():
    mock_resp = _mock_urlopen({})
    with patch('urllib.request.urlopen', return_value=mock_resp):
        assert probe_ollama_context_window('gollum', 'qwen3-coder:30b') is None


def test_probe_ollama_context_window_missing_context_length_key_returns_none():
    mock_resp = _mock_urlopen({'modelinfo': {'other': 'data'}})
    with patch('urllib.request.urlopen', return_value=mock_resp):
        assert probe_ollama_context_window('gollum', 'qwen3-coder:30b') is None


def test_probe_ollama_context_window_exception_returns_none():
    with patch('urllib.request.urlopen', side_effect=Exception('connection refused')):
        assert probe_ollama_context_window('gollum', 'qwen3-coder:30b') is None


# ── build_backends / build_llms ──────────────────────────────────────────────

SAMPLE_DISCOVERIES = {
    'pond': {
        'models': {
            'llama_server': {'up': True, 'models': ['qwen3-coder-30b.gguf'], 'context_window': 65536},
            'ollama': {'up': False, 'models': ['stale'], 'context_window': None},
        },
        'ggufs': None,
    }
}


def test_build_backends_includes_context_window_for_up_backend():
    backends = build_backends(SAMPLE_DISCOVERIES)
    llama = next(b for b in backends if b.name == 'llama-server')
    assert llama.context_window == 65536
    assert llama.status == 'up'
    assert llama.port == 9337
    assert llama.last_seen


def test_build_backends_keeps_down_backend_without_context_window():
    ollama = next(b for b in build_backends(SAMPLE_DISCOVERIES) if b.name == 'ollama')
    assert ollama.status == 'down'
    assert 'context_window' not in ollama.to_dict()
    assert 'last_seen' not in ollama.to_dict()


def test_build_backends_and_llms_empty_discoveries():
    assert build_backends({}) == []
    assert build_llms({}) == []


def test_build_llms_one_row_per_llm_on_up_backends_only():
    llms = build_llms(SAMPLE_DISCOVERIES)
    assert [l.to_dict() for l in llms] == [
        {'hostname': 'pond', 'backend': 'llama-server', 'name': 'qwen3-coder-30b.gguf'},
    ]


def test_ggufs_backend_and_llms_only_when_ggufs_not_none():
    assert not any(b.name == 'ggufs' for b in build_backends(SAMPLE_DISCOVERIES))
    discoveries = {'pond': {'models': SAMPLE_DISCOVERIES['pond']['models'], 'ggufs': ['a.gguf', 'b.gguf']}}
    ggufs = next(b for b in build_backends(discoveries) if b.name == 'ggufs')
    assert ggufs.status == 'installed'
    assert ggufs.port is None
    assert [l.name for l in build_llms(discoveries) if l.backend == 'ggufs'] == ['a.gguf', 'b.gguf']


# ── build_agents ──────────────────────────────────────────────────────────────

SAMPLE_AGENT_ROWS = [
    {
        'hostname': 'pond',
        'name': 'hermes',
        'endpoint': 'http://pond:8642',
        'status': 'up',
        'process': 'running',
        'last_seen': '2026-06-08T09-00-00',
    }
]


def test_build_agents_no_preserved_buffer_omits_key():
    agents = build_agents(SAMPLE_AGENT_ROWS, preserved_buffers={})
    assert 'reasoning_buffer' not in agents[0].to_dict()


def test_build_agents_preserves_existing_reasoning_buffer():
    agents = build_agents(SAMPLE_AGENT_ROWS, preserved_buffers={('pond', 'hermes'): 12000})
    assert agents[0].reasoning_buffer == 12000
    assert agents[0].to_dict() == {**SAMPLE_AGENT_ROWS[0], 'reasoning_buffer': 12000}


def test_build_agents_no_rows_returns_empty_list():
    assert build_agents([]) == []


# ── read_existing_agent_reasoning_buffers ─────────────────────────────────────

def test_read_existing_buffers_finds_values():
    agents = [
        {'hostname': 'pond', 'name': 'hermes', 'reasoning_buffer': 12000},
        {'hostname': 'pond', 'name': 'goose', 'status': 'down'},
    ]
    assert read_existing_agent_reasoning_buffers(agents) == {('pond', 'hermes'): 12000}


def test_read_existing_buffers_empty_when_no_agents():
    assert read_existing_agent_reasoning_buffers([]) == {}


def test_read_existing_buffers_skips_rows_without_reasoning_buffer():
    agents = [{'hostname': 'pond', 'name': 'hermes', 'status': 'up'}]
    assert read_existing_agent_reasoning_buffers(agents) == {}


# ── main: upgrading a schema_version 1 file ──────────────────────────────────

def test_main_upgrades_v1_file_and_keeps_reasoning_buffer(tmp_path, monkeypatch):
    from topology import discover
    from topology.toml_io import read_toml, write_toml
    path = tmp_path / 'topology.toml'
    write_toml({
        'schema_version': 1,
        'machines': [{'name': 'pond', 'hostname': 'pond'}],
        'model_state': [{'hostname': 'pond', 'backend': 'ollama', 'port': 11434, 'models': ['old'], 'status': 'up'}],
        'agent_state': [{'hostname': 'pond', 'agent': 'goose', 'status': 'up', 'reasoning_buffer': 8000}],
    }, str(path))
    monkeypatch.setattr(discover, 'get_topology_path', lambda: str(path))
    monkeypatch.setattr(discover, 'probe_models', lambda host: {
        'llama_server': {'up': False, 'models': [], 'context_window': None},
        'ollama': {'up': True, 'models': ['qwen3:4b'], 'context_window': 32768},
    })
    monkeypatch.setattr(discover, 'probe_agents', lambda host, user, row: [
        {'hostname': 'pond', 'name': 'goose', 'endpoint': 'ws://pond:3284', 'status': 'down', 'process': '(no SSH)'},
    ])

    discover.main()

    data = read_toml(str(path))
    assert data['schema_version'] == 2
    assert 'model_state' not in data and 'agent_state' not in data
    assert [b['name'] for b in data['backends']] == ['llama-server', 'ollama']
    assert data['llms'] == [{'hostname': 'pond', 'backend': 'ollama', 'name': 'qwen3:4b'}]
    assert data['agents'][0]['reasoning_buffer'] == 8000
