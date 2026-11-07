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
    model_terms,
    parse_argv,
    read_prompt,
)

TOPOLOGY = {
    'machines': [
        {'name': 'pond', 'hostname': 'pond', 'os': 'linux', 'ssh': True},
        {'name': 'hut', 'hostname': 'hut', 'os': 'macOS'},
    ],
    'model_state': [
        {'hostname': 'pond', 'backend': 'ollama', 'models': ['qwen3-coder:30b'], 'status': 'up'},
        {'hostname': 'hut', 'backend': 'llama-server', 'models': [], 'status': 'down'},
    ],
}

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


def test_model_terms_include_base_name_but_not_generic_parts():
    assert model_terms('qwen3-coder:30b') == {'qwen3-coder:30b', 'qwen3-coder'}
    assert model_terms('foo-7b.gguf') == {'foo-7b.gguf', 'foo-7b'}
    assert model_terms('abc:latest') == {'abc:latest'}


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


def test_match_prompt_reports_model_location():
    text = '\n'.join(match_prompt('which box has qwen3-coder?', TOPOLOGY, PLAYBOOKS))
    assert 'Model qwen3-coder:30b is on pond (ollama, up' in text


def test_match_prompt_is_empty_for_unrelated_prompt():
    assert match_prompt('refactor the parser in sync.py', TOPOLOGY, PLAYBOOKS) == []


def test_match_prompt_falls_back_to_keywords():
    lines = match_prompt('show me the topology', TOPOLOGY, PLAYBOOKS)
    assert lines and 'mentions the topology' in lines[-1]


def test_match_prompt_skips_slash_commands_and_missing_topology():
    assert match_prompt('/topology show pond', TOPOLOGY, PLAYBOOKS) == []
    assert match_prompt('is pond up?', {}, PLAYBOOKS) == []


# ── summary ──────────────────────────────────────────────────────────────────

def test_build_summary_lists_machines_running_models_and_playbooks():
    text = build_summary(TOPOLOGY, PLAYBOOKS)
    assert 'Machines: pond (ssh), hut' in text
    assert 'pond ollama: qwen3-coder:30b' in text
    assert 'hut llama-server' not in text
    assert 'start-pond-qwen: "start qwen on pond"' in text
    assert 'topology.cli' in text


def test_build_summary_is_empty_without_topology():
    assert build_summary({}, PLAYBOOKS) == ''


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
