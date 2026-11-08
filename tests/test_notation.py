import io
from unittest.mock import patch

import pytest

from topology.hooks import main
from topology.notation import (
    build_expanded_form,
    expand,
    expand_prompt,
    find_notations,
    read_notation_key,
    read_skill_name,
)

TOPOLOGY = {
    'machines': [
        {'name': 'pond', 'hostname': 'pond', 'os': 'linux', 'ssh': True},
        {'name': 'hut', 'hostname': 'hut', 'os': 'macOS'},
    ],
    'model_state': [
        {'hostname': 'pond', 'backend': 'ollama', 'models': ['qwen3-coder:30b'], 'status': 'up'},
    ],
}

PLAYBOOKS = {
    'start-pond-qwen': {
        'name': 'start-pond-qwen',
        'aliases': ['start qwen on pond', 'wake pond up'],
        'description': 'Start qwen on pond',
        'tasks': [{'hosts': 'pond', 'command': 'ollama run ${model:-qwen}'}],
        'source': 'x.toml',
    },
}


@pytest.fixture(autouse=True)
def notation_log(tmp_path, monkeypatch):
    path = tmp_path / 'notation.log'
    monkeypatch.setenv('TOPOLOGY_NOTATION_LOG', str(path))
    monkeypatch.delenv('TOPOLOGY_NOTATION_DEBUG', raising=False)
    return path


# ── notation key ────────────────────────────────────────────────────────────────

def test_read_notation_key_from_skill_md_front_matter(tmp_path):
    skill = tmp_path / 'SKILL.md'
    skill.write_text('---\nname: x\nmetadata:\n  notation-key: "^abc"\n---\nbody\n')
    assert read_notation_key(skill) == '^abc'


def test_read_notation_key_defaults_when_missing(tmp_path):
    assert read_notation_key(tmp_path / 'nope.md') == '^top'


def test_read_skill_name_from_front_matter(tmp_path):
    skill = tmp_path / 'SKILL.md'
    skill.write_text('---\nname: abc\n---\nbody\n')
    assert read_skill_name(skill) == 'abc'
    assert read_skill_name() == 'topology'


def test_repo_skill_md_declares_top():
    assert read_notation_key() == '^top'


# ── finding notations ────────────────────────────────────────────────────────

def test_find_notations_with_and_without_payload():
    assert find_notations('is ^top:pond up? and ^top', '^top') == [('^top:pond', 'pond'), ('^top', '')]


def test_find_notations_strips_trailing_punctuation_and_decodes():
    assert find_notations('run ^top:wake+pond+up.', '^top') == [('^top:wake+pond+up', 'wake pond up')]
    assert find_notations('^top:a%2Bb', '^top') == [('^top:a%2Bb', 'a+b')]


def test_find_notations_keeps_colons_inside_payload():
    assert find_notations('^top:qwen3-coder:30b', '^top') == [('^top:qwen3-coder:30b', 'qwen3-coder:30b')]


def test_find_notations_ignores_lookalikes_and_duplicates():
    assert find_notations('grep x^top:pond, ^topology, ^start', '^top') == []
    assert find_notations('^top:pond ^top:pond', '^top') == [('^top:pond', 'pond')]


# ── expanding payloads ───────────────────────────────────────────────────────

def test_expand_host():
    tag, kind, text = expand('pond', TOPOLOGY, PLAYBOOKS)
    assert (tag, kind) == ('expanded', 'host')
    assert 'Host pond: linux, ssh.' in text
    assert 'ollama (up at last discover): qwen3-coder:30b.' in text
    assert 'Playbooks targeting it: start-pond-qwen.' in text


def test_expand_model_by_base_name():
    tag, kind, text = expand('qwen3-coder', TOPOLOGY, PLAYBOOKS)
    assert (tag, kind) == ('expanded', 'model')
    assert 'Model qwen3-coder:30b is on pond' in text


def test_expand_playbook_by_name_and_alias():
    for payload in ('start-pond-qwen', 'Wake pond up'):
        tag, kind, text = expand(payload, TOPOLOGY, PLAYBOOKS)
        assert (tag, kind) == ('expanded', 'playbook')
        assert 'Playbook start-pond-qwen.' in text
        assert 'run "start qwen on pond" --var model=...' in text


def test_expand_bare_notation_key_gives_summary():
    tag, kind, text = expand('', TOPOLOGY, PLAYBOOKS)
    assert (tag, kind) == ('expanded', 'summary')
    assert 'Machines: pond (ssh), hut' in text


def test_expand_unknown_handle_suggests():
    tag, kind, text = expand('pnd', TOPOLOGY, PLAYBOOKS)
    assert (tag, kind) == ('error', 'unknown')
    assert "Unknown handle 'pnd'" in text
    assert 'pond' in text


def test_expand_without_topology_is_error():
    assert expand('pond', {}, PLAYBOOKS)[0] == 'error'


# ── expanded form ────────────────────────────────────────────────────────────

def test_build_expanded_form_shape_and_escaping():
    form = build_expanded_form('^top:a', 'expanded', 'x < y & z\nline two', 'topology')
    assert form == (
        '<skill-notation skill="topology">\n'
        '  <notation>^top:a</notation>\n'
        '  <expanded>\n'
        '    x &lt; y &amp; z\n'
        '    line two\n'
        '  </expanded>\n'
        '</skill-notation>'
    )


def test_expand_prompt_builds_one_form_per_notation_and_logs(notation_log):
    out = expand_prompt('compare ^top:pond and ^top:pnd', TOPOLOGY, PLAYBOOKS, key='^top')
    assert out.count('<skill-notation skill="topology">') == 2
    assert '<notation>^top:pond</notation>' in out
    assert '<error>' in out

    logged = notation_log.read_text()
    assert '2 notation(s) for ^top' in logged
    assert "notation found: ^top:pond  payload='pond'" in logged
    assert 'resolved: ^top:pond -> expanded (host)' in logged
    assert 'resolved: ^top:pnd -> error (unknown)' in logged
    assert 'expanded form:\n<skill-notation skill="topology">' in logged


def test_expand_prompt_is_silent_without_notations(notation_log):
    assert expand_prompt('is pond up?', TOPOLOGY, PLAYBOOKS, key='^top') == ''
    assert not notation_log.exists()


def test_debug_env_also_logs_to_stderr(monkeypatch, capsys):
    monkeypatch.setenv('TOPOLOGY_NOTATION_DEBUG', '1')
    expand_prompt('^top:hut', TOPOLOGY, PLAYBOOKS, key='^top')
    assert 'notation found: ^top:hut' in capsys.readouterr().err


# ── hook integration ─────────────────────────────────────────────────────────

def run_prompt_hook(prompt, capsys):
    with patch('topology.hooks.load_topology', return_value=TOPOLOGY), \
         patch('topology.hooks.load_playbooks_quietly', return_value=PLAYBOOKS), \
         patch('sys.stdin', io.StringIO(prompt)):
        main(['prompt'])
    return capsys.readouterr().out


def test_prompt_hook_prefers_notations_over_plain_hints(capsys):
    out = run_prompt_hook('is ^top:pond up? also start qwen on pond', capsys)
    assert '<notation>^top:pond</notation>' in out
    assert 'matches alias' not in out


def test_prompt_hook_falls_back_to_plain_hints(capsys):
    out = run_prompt_hook('is pond up?', capsys)
    assert 'Host pond is in the topology' in out
    assert '<skill-notation' not in out


# ── completion (pi autocomplete) ─────────────────────────────────────────────

from topology.notation import build_completion, completion_items, encode_payload  # noqa: E402


def test_encode_payload_uses_plus_for_spaces_and_keeps_colons():
    assert encode_payload('wake pond up') == 'wake+pond+up'
    assert encode_payload('qwen3-coder:30b') == 'qwen3-coder:30b'


def test_completion_items_cover_hosts_models_playbooks_and_aliases():
    values = {i['value']: i['description'] for i in completion_items(TOPOLOGY, PLAYBOOKS)}
    assert values['pond'] == 'host: linux, ssh'
    assert values['qwen3-coder:30b'] == 'model on pond (ollama, up)'
    assert values['start-pond-qwen'] == 'playbook: Start qwen on pond'
    assert values['wake+pond+up'] == 'alias of playbook start-pond-qwen'


def test_completion_values_round_trip_through_find_notations_and_expand():
    for item in completion_items(TOPOLOGY, PLAYBOOKS):
        [(_, payload)] = find_notations(f'^top:{item["value"]}', '^top')
        assert expand(payload, TOPOLOGY, PLAYBOOKS)[0] == 'expanded', item


def test_build_completion_includes_key_and_skill():
    completion = build_completion(TOPOLOGY, PLAYBOOKS)
    assert completion['key'] == '^top'
    assert completion['skill'] == 'topology'
    assert completion['handles']


def test_notation_complete_cli_prints_json(capsys):
    import json
    from topology.notation import main as notation_main
    with patch('topology.notation.load_topology', return_value=TOPOLOGY), \
         patch('topology.notation.load_playbooks_quietly', return_value=PLAYBOOKS):
        notation_main(['complete'])
    assert json.loads(capsys.readouterr().out)['key'] == '^top'
