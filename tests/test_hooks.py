import io
import json
from unittest.mock import patch

import pytest

from topology.hooks import (
    build_summary,
    contains_term,
    format_output,
    main,
    match_prompt,
    llm_terms,
    parse_argv,
    read_prompt,
)
from topology.models import Topology

TOPOLOGY = Topology.from_dict({
    'machines': [
        {'name': 'pond', 'hostname': 'pond', 'os': 'linux', 'ssh': True},
        {'name': 'hut', 'hostname': 'hut', 'os': 'macOS'},
    ],
    'llm_providers': [
        {'hostname': 'pond', 'name': 'ollama', 'status': 'up'},
        {'hostname': 'hut', 'name': 'llama-server', 'status': 'down'},
    ],
    'llms': [
        {'hostname': 'pond', 'provider': 'ollama', 'name': 'qwen3-coder:30b'},
    ],    'ggufs': [
        {'hostname': 'pond', 'name': 'qwen-14b-q4.gguf', 'path': '/models/qwen-14b-q4.gguf'},
    ],
})

PLAYBOOKS = {
    'start-pond-qwen': {
        'name': 'start-pond-qwen',
        'aliases': ['start qwen on pond'],
        'description': '',
        'tasks': [{'hosts': 'pond', 'command': 'ollama run qwen'}],
        'source': 'x.toml',
    },
}


# ── matching primitives ──────────────────────────────────────────────────────

def test_contains_term_matches_whole_words_case_insensitively():
    assert contains_term('Is POND up?', 'pond')
    assert not contains_term('respond quickly', 'pond')
    assert not contains_term('pond-side', 'pond')


def test_llm_terms_include_base_name_but_not_generic_parts():
    assert llm_terms('qwen3-coder:30b') == {'qwen3-coder:30b', 'qwen3-coder'}
    assert llm_terms('foo-7b.gguf') == {'foo-7b.gguf', 'foo-7b'}
    assert llm_terms('abc:latest') == {'abc:latest'}


# ── prompt matching ──────────────────────────────────────────────────────────

def test_match_prompt_reports_playbook_alias_with_run_command():
    lines = match_prompt('please Start  qwen on pond now', TOPOLOGY, PLAYBOOKS)
    text = '\n'.join(lines)
    assert 'Playbook start-pond-qwen matches alias "start qwen on pond"' in text
    assert 'run "start qwen on pond"' in text


def test_match_prompt_reports_host_and_playbooks_targeting_it():
    text = '\n'.join(match_prompt('is pond online?', TOPOLOGY, PLAYBOOKS))
    assert 'Host pond is in the topology (linux, ssh)' in text
    assert 'start-pond-qwen' in text


def test_match_prompt_reports_llm_location():
    text = '\n'.join(match_prompt('which box has qwen3-coder?', TOPOLOGY, PLAYBOOKS))
    assert 'LLM qwen3-coder:30b is on pond (ollama, up' in text


def test_match_prompt_is_empty_for_unrelated_prompt():
    assert match_prompt('refactor the parser in sync.py', TOPOLOGY, PLAYBOOKS) == []


def test_match_prompt_ignores_bare_topology_and_playbook_words():
    assert match_prompt('explain the topology skill and its playbooks', TOPOLOGY, PLAYBOOKS) == []


def test_match_prompt_skips_slash_commands_and_missing_topology():
    assert match_prompt('/topology show pond', TOPOLOGY, PLAYBOOKS) == []
    assert match_prompt('is pond up?', None, PLAYBOOKS) == []


# ── summary ──────────────────────────────────────────────────────────────────

def test_build_summary_lists_machines_running_llms_and_playbooks():
    text = build_summary(TOPOLOGY, PLAYBOOKS)
    assert 'Machines: pond (ssh), hut' in text
    assert 'pond ollama: qwen3-coder:30b' in text
    assert 'hut llama-server' not in text
    assert 'start-pond-qwen: "start qwen on pond"' in text
    assert 'topology.cli' in text


def test_build_summary_is_empty_without_topology():
    assert build_summary(None, PLAYBOOKS) == ''


# ── I/O formats ──────────────────────────────────────────────────────────────

def test_read_prompt_parses_claude_json_and_passes_text_through():
    assert read_prompt('{"prompt": "hi", "session_id": "x"}', 'claude') == 'hi'
    assert read_prompt('hi', 'text') == 'hi'


def test_format_output_wraps_claude_additional_context():
    out = json.loads(format_output('hint', 'prompt', 'claude'))
    assert out == {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'hint'}}
    assert format_output('hint', 'session-start', 'text') == 'hint'
    assert format_output('', 'prompt', 'claude') == ''


def test_parse_argv_accepts_events_and_format():
    assert parse_argv(['prompt']) == ('prompt', 'text')
    assert parse_argv(['session-start', '--format', 'claude']) == ('session-start', 'claude')
    with pytest.raises(ValueError):
        parse_argv(['bogus'])
    with pytest.raises(ValueError):
        parse_argv(['prompt', '--format', 'xml'])


# ── main ─────────────────────────────────────────────────────────────────────

def test_main_prints_hint_for_matching_prompt(capsys):
    with patch('topology.hooks.load_topology', return_value=TOPOLOGY), \
         patch('topology.hooks.load_playbooks_quietly', return_value=PLAYBOOKS), \
         patch('sys.stdin', io.StringIO('is pond up?')):
        main(['prompt'])
    assert 'Host pond' in capsys.readouterr().out


def test_main_swallows_errors_and_prints_nothing(capsys):
    with patch('topology.hooks.load_topology', side_effect=RuntimeError('boom')), \
         patch('sys.stdin', io.StringIO('is pond up?')):
        main(['prompt'])
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'boom' in captured.err


def test_load_topology_reads_a_v1_file_as_llms(tmp_path, monkeypatch):
    from topology import hooks
    from topology.toml_io import write_toml
    path = tmp_path / 'topology.toml'
    write_toml({
        'schema_version': 1,
        'machines': [{'name': 'pond', 'hostname': 'pond'}],
        'model_state': [{'hostname': 'pond', 'backend': 'ollama', 'models': ['qwen3-coder:30b'], 'status': 'up'}],
    }, str(path))
    monkeypatch.setattr(hooks, 'get_topology_path', lambda: str(path))
    topology = hooks.load_topology()
    assert [l.name for l in topology.llms] == ['qwen3-coder:30b']
    assert 'pond ollama: qwen3-coder:30b' in build_summary(topology, {})


def test_load_topology_is_none_without_a_file(tmp_path, monkeypatch):
    from topology import hooks
    monkeypatch.setattr(hooks, 'get_topology_path', lambda: str(tmp_path / 'missing.toml'))
    assert hooks.load_topology() is None


def test_prompt_mentioning_gguf_file_gets_a_file_hint():
    hints = match_prompt('load qwen-14b-q4 into llama-server', TOPOLOGY, {})
    assert 'GGUF qwen-14b-q4.gguf is on pond (on disk at /models/qwen-14b-q4.gguf).' in hints
