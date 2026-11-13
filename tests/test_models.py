import tomllib

import pytest

from topology.models import (
    GGUF,
    LLM,
    SCHEMA_VERSION,
    Agent,
    Benchmark,
    FailureReport,
    FileResource,
    LLMProvider,
    Machine,
    Playbook,
    Server,
    Task,
    Topology,
    upgrade_v1,
)
from topology.toml_io import read_toml, write_toml

TOPOLOGY = {
    'schema_version': 2,
    'network': 'tailscale',
    'last_refreshed': '2026-08-21T10-02-21',
    'machines': [
        {'name': 'pond', 'hostname': 'pond', 'tailscale_ip': '100.0.0.1', 'local_ip': '192.168.1.2',
         'os': 'linux', 'ssh': True, 'ssh_user': 'me', 'gpu': 'gpu', 'vram': '24GB (CUDA)',
         'last_verified': '2026-08-31', 'goose_acp_url': 'ws://pond:3284'},
        {'name': 'hut', 'hostname': 'hut', 'local_ip': '192.168.1.3'},
    ],
    'llm_providers': [
        {'hostname': 'pond', 'name': 'ollama', 'status': 'up', 'port': 11434,
         'context_window': 32768, 'last_seen': '2026-11-10T01-00-00'},
        {'hostname': 'hut', 'name': 'llama-server', 'status': 'down', 'port': 9337},
    ],
    'llms': [
        {'hostname': 'pond', 'provider': 'ollama', 'name': 'qwen2.5-coder:7b'},
        {'hostname': 'pond', 'provider': 'ollama', 'name': 'qwen2.5-coder:14b'},
    ],
    'agents': [
        {'hostname': 'pond', 'name': 'goose', 'status': 'down', 'endpoint': 'ws://pond:3284',
         'process': 'not found', 'reasoning_buffer': 12000},
    ],
    'ggufs': [
        {'hostname': 'pond', 'name': 'qwen.gguf', 'path': '/home/me/.local/share/gguf/qwen.gguf',
         'last_seen': '2026-11-10T01-00-00'},
    ],
    'benchmarks': [
        {'hostname': 'pond', 'llm': 'qwen2.5-coder:7b', 'timestamp': '2026-06-11T19-02-45',
         'ttft_ms': 2617.4, 'tok_s': 12.7, 'runs': 3},
    ],
}

PLAYBOOK = {
    'name': 'pull-model-on-dtiv',
    'aliases': ['pull model on dtiv'],
    'description': 'Pull a model.',
    'tasks': [
        {'name': 'pull model', 'hosts': 'dtiv', 'command': 'ollama pull "${MODEL}"'},
        {'name': 'restart', 'hosts': 'dtiv', 'command': 'systemctl restart ollama', 'oversight': True},
        {'ref': 'benchmark-model-on-dtiv'},
    ],
}


# ── Topology ─────────────────────────────────────────────────────────────────

def test_topology_round_trips():
    topology = Topology.from_dict(TOPOLOGY)
    assert isinstance(topology.machines[0], Machine)
    assert isinstance(topology.llm_providers[0], LLMProvider)
    assert isinstance(topology.llms[0], LLM)
    assert isinstance(topology.agents[0], Agent)
    assert isinstance(topology.ggufs[0], GGUF)
    assert isinstance(topology.benchmarks[0], Benchmark)
    assert topology.to_dict() == TOPOLOGY


def test_topology_survives_toml_write_and_read(tmp_path):
    path = tmp_path / 'topology.toml'
    write_toml(Topology.from_dict(TOPOLOGY).to_dict(), str(path))
    assert Topology.from_dict(read_toml(str(path))).to_dict() == TOPOLOGY


def test_new_topology_is_current_schema_version():
    assert Topology().schema_version == SCHEMA_VERSION
    assert Topology().to_dict()['schema_version'] == SCHEMA_VERSION


def test_provider_serving_no_llms_is_kept():
    topology = Topology.from_dict(TOPOLOGY)
    assert [p.name for p in topology.llm_providers_on('hut')] == ['llama-server']
    assert topology.llms_on('hut') == []


def test_servers_and_file_resources_are_separate_families():
    assert issubclass(LLMProvider, Server) and issubclass(Agent, Server)
    assert issubclass(GGUF, FileResource)
    assert not issubclass(GGUF, Server)


def test_lookups_follow_hostname_and_provider():
    topology = Topology.from_dict(TOPOLOGY)
    assert topology.machine('pond').ssh_user == 'me'
    assert topology.machine('nowhere') is None
    assert [p.name for p in topology.llm_providers_on('pond')] == ['ollama']
    assert [l.name for l in topology.llms_on('pond', 'ollama')] == ['qwen2.5-coder:7b', 'qwen2.5-coder:14b']
    assert len(topology.llms_on('pond')) == 2
    assert [a.name for a in topology.agents_on('pond')] == ['goose']
    assert [g.name for g in topology.ggufs_on('pond')] == ['qwen.gguf']
    assert [type(s) for s in topology.servers_on('pond')] == [LLMProvider, Agent]
    llm = topology.llms[0]
    assert topology.provider_of(llm).context_window == 32768
    assert [b.tok_s for b in topology.benchmarks_for(llm)] == [12.7]


def test_rows_for_unknown_hosts_are_kept():
    data = {**TOPOLOGY, 'llms': [*TOPOLOGY['llms'], {'hostname': 'ghost', 'provider': 'ollama', 'name': 'x'}]}
    topology = Topology.from_dict(data)
    assert topology.machine('ghost') is None
    assert topology.to_dict() == data


def test_unknown_keys_are_kept_in_extra():
    machine = Machine.from_dict({'name': 'a', 'hostname': 'a', 'hermes_gateway': 'http://a:8642'})
    assert machine.extra == {'hermes_gateway': 'http://a:8642'}
    assert machine.to_dict() == {'name': 'a', 'hostname': 'a', 'hermes_gateway': 'http://a:8642'}


def test_missing_required_field_raises():
    with pytest.raises(ValueError, match='LLM is missing provider'):
        LLM.from_dict({'hostname': 'pond', 'name': 'x'})


# ── upgrade_v1 ───────────────────────────────────────────────────────────────

V1 = {
    'schema_version': 1,
    'provider': 'tailscale',
    'machines': [{'name': 'pond', 'hostname': 'pond'}],
    'model_state': [
        {'hostname': 'pond', 'backend': 'ollama', 'port': 11434, 'models': ['a', 'b'],
         'status': 'up', 'context_window': 32768, 'last_seen': 't'},
        {'hostname': 'pond', 'backend': 'llama-server', 'port': 9337, 'models': [], 'status': 'down'},
        {'hostname': 'pond', 'backend': 'ggufs', 'models': ['c.gguf'], 'status': 'installed'},
    ],
    'agent_state': [
        {'hostname': 'pond', 'agent': 'hermes', 'endpoint': 'e', 'status': 'up', 'process': 'running',
         'reasoning_buffer': 12000},
    ],
    'benchmarks': [
        {'hostname': 'pond', 'model': 'a', 'timestamp': 't', 'ttft_ms': 1.0, 'tok_s': 2.0, 'runs': 3},
    ],
}


def test_upgrade_v1_splits_providers_and_llms():
    data = upgrade_v1(V1)
    assert data['schema_version'] == 2
    assert 'model_state' not in data and 'agent_state' not in data
    assert data['llm_providers'] == [
        {'hostname': 'pond', 'name': 'ollama', 'port': 11434, 'status': 'up',
         'context_window': 32768, 'last_seen': 't'},
        {'hostname': 'pond', 'name': 'llama-server', 'port': 9337, 'status': 'down'},
    ]
    assert data['llms'] == [
        {'hostname': 'pond', 'provider': 'ollama', 'name': 'a'},
        {'hostname': 'pond', 'provider': 'ollama', 'name': 'b'},
    ]


def test_upgrade_v1_turns_ggufs_row_into_files():
    assert upgrade_v1(V1)['ggufs'] == [
        {'hostname': 'pond', 'name': 'c.gguf', 'path': '~/.local/share/gguf/c.gguf'},
    ]


def test_upgrade_v1_renames_provider_to_network():
    data = upgrade_v1(V1)
    assert data['network'] == 'tailscale'
    assert 'provider' not in data


def test_upgrade_v1_renames_agent_and_benchmark_keys():
    data = upgrade_v1(V1)
    assert data['agents'] == [{'hostname': 'pond', 'name': 'hermes', 'endpoint': 'e', 'status': 'up',
                               'process': 'running', 'reasoning_buffer': 12000}]
    assert data['benchmarks'][0]['llm'] == 'a'
    assert 'model' not in data['benchmarks'][0]


def test_upgrade_v1_result_loads_and_round_trips():
    data = upgrade_v1(V1)
    assert Topology.from_dict(data).to_dict() == data


def test_upgrade_v1_leaves_v2_alone_and_does_not_mutate_input():
    snapshot = repr(V1)
    upgrade_v1(V1)
    assert repr(V1) == snapshot
    assert upgrade_v1(TOPOLOGY) is TOPOLOGY


# ── Playbooks ────────────────────────────────────────────────────────────────

def test_playbook_round_trips_with_command_and_ref_tasks():
    playbook = Playbook.from_dict(PLAYBOOK)
    assert all(isinstance(t, Task) for t in playbook.tasks)
    assert playbook.tasks[2].ref == 'benchmark-model-on-dtiv'
    assert playbook.tasks[0].oversight is None
    assert playbook.to_dict() == PLAYBOOK


def test_task_usage_defaults_and_is_never_written():
    task = Task.from_dict(PLAYBOOK['tasks'][0])
    assert task.last_run is None
    assert task.frequency == 0
    task.last_run = '2026-11-10T02:00:00Z'
    task.frequency = 4
    assert task.to_dict() == PLAYBOOK['tasks'][0]


def test_playbook_source_is_runtime_only():
    playbook = Playbook.from_dict(PLAYBOOK)
    playbook.source = '/tmp/topology-playbook-dtiv.toml'
    assert 'source' not in playbook.to_dict()


def test_playbook_parses_from_toml_text():
    text = '''
[[playbook]]
name = "p"
aliases = ["run p"]

  [[playbook.tasks]]
  name = "step"
  hosts = "localhost"
  command = "echo hi"
'''
    playbook = Playbook.from_dict(tomllib.loads(text)['playbook'][0])
    assert playbook.tasks[0].hosts == 'localhost'
    assert playbook.description is None


# ── Failure reports ──────────────────────────────────────────────────────────

def test_failure_report_round_trips():
    report = {'playbook': 'p', 'task': 't', 'hosts': 'pond', 'command': 'false', 'exit_code': 1,
              'timestamp': '2026-11-10T02-00-00', 'stdout': '', 'stderr': 'boom'}
    assert FailureReport.from_dict(report).to_dict() == report
